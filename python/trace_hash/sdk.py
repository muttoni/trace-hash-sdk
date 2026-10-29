from __future__ import annotations

import concurrent.futures
import io
import json
import multiprocessing
import os
import resource
import signal
import tempfile
import threading
import time
from contextvars import ContextVar
from pathlib import Path

from . import _native
from .adapters import Expansion, children, detect, mapped_record
from .model import Config, LimitExceeded, child_id, config_dict, config_from_dict, envelope, opaque_id
from .text import text_feature

CONTAINERS = {"jsonl", "csv", "parquet", "arrow", "warc", "zip", "tar", "gzip", "docx", "pdf"}
IO_STATS = ContextVar("trace_io_stats", default=None)


def _failure(dataset, item, issue, **kwargs):
    return envelope(dataset, item, status="failed", coverage="none", issues=[issue], **kwargs)


def _stat(path):
    s = os.stat(path)
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _process_path(
    path,
    dataset,
    item,
    config,
    expansion,
    depth=0,
    parent=None,
    forced=None,
    unit="file",
    position=None,
    hint=None,
):
    before = _stat(path)
    if before[2] > config.limits.max_input_bytes:
        return [_failure(dataset, item, "input_size_limit", parent_id=parent, unit=unit, position=position)]
    digest, size = _native.digest_file(str(path))
    root = envelope(dataset, item, parent_id=parent, unit=unit, position=position, size=size, digest=digest)
    rows = [root]
    try:
        kind = forced or detect(path, hint)
        root["modality"] = (
            "document"
            if kind in ("pdf", "docx")
            else "container"
            if kind in CONTAINERS
            else "text"
            if kind in ("text", "html")
            else kind
        )
        if kind in ("text", "code", "html"):
            with open(path, "rb") as source:
                data = source.read(config.limits.max_record_bytes + 1)
            if len(data) > config.limits.max_record_bytes:
                raise LimitExceeded("record_size_limit")
            root["features"] = [text_feature(data, config, code=kind == "code", html=kind == "html")]
            if root["features"][0]["payload"]["truncated"]:
                raise LimitExceeded("feature_limit")
        elif kind == "image":
            from .media import image_features

            root["features"] = image_features(path, config)
        elif kind in ("audio", "video"):
            from .media import audio_video_features

            root["features"], root["issues"] = audio_video_features(path, config, video=kind == "video")
            if root["issues"]:
                root.update(coverage="partial", status="partial")
        elif kind in CONTAINERS:
            if depth >= config.limits.max_depth:
                raise LimitExceeded("depth_limit")
            if kind in ("pdf", "docx"):
                root["issues"].append("document_extraction_experimental")
                if kind == "pdf":
                    root["issues"].append("ocr_not_performed")
            manifest_size = 0
            for data, name, index, forced_kind in children(path, kind, config, expansion, hint):
                cid = child_id(item, index)
                if forced_kind == "skip":
                    sub = [
                        envelope(
                            dataset,
                            cid,
                            parent_id=item,
                            unit="record",
                            position=index,
                            status="unsupported",
                            coverage="none",
                            issues=["non_regular_entry_skipped"],
                        )
                    ]
                elif forced_kind == "record":
                    try:
                        sub = mapped_record(data, dataset, cid, config, item, index)
                    except (KeyError, IndexError, TypeError, ValueError):
                        sub = [
                            _failure(
                                dataset, cid, "invalid_record", parent_id=item, unit="record", position=index
                            )
                        ]
                elif isinstance(data, Path):
                    sub = _process_path(
                        data,
                        dataset,
                        cid,
                        config,
                        expansion,
                        depth + 1,
                        item,
                        unit="record",
                        position=index,
                        hint=name,
                    )
                else:
                    if len(data) > config.limits.max_record_bytes:
                        raise LimitExceeded("record_size_limit")
                    with tempfile.NamedTemporaryFile() as tmp:
                        tmp.write(data)
                        tmp.flush()
                        sub = _process_path(
                            tmp.name,
                            dataset,
                            cid,
                            config,
                            expansion,
                            depth + 1,
                            item,
                            forced_kind,
                            "record",
                            index,
                            name,
                        )
                if kind == "pdf" or (kind == "docx" and index == 0):
                    # Generated text/rendered images are not original source bytes.
                    for row in sub:
                        row["exact_basis"] = "derived_content"
                manifest_size += sum(len(json.dumps(row)) for row in sub)
                if manifest_size > config.limits.max_manifest_bytes:
                    raise LimitExceeded("manifest_size_limit")
                rows.extend(sub)
            if any(row["coverage"] != "complete" for row in rows[1:]) or kind in ("pdf", "docx"):
                root.update(status="partial", coverage="partial")
                root["issues"].append(
                    "child_coverage_incomplete"
                    if kind not in ("pdf", "docx")
                    else "document_coverage_limited"
                )
        else:
            root.update(status="unsupported", coverage="none", issues=["fuzzy_unsupported"])
    except ImportError:
        root.update(status="unsupported", coverage="none")
        root["issues"].append("optional_dependency_missing")
    except LimitExceeded as exc:
        root.update(status="partial", coverage="partial")
        root["issues"].append(str(exc))
    except Exception:
        # Never put decoder messages (which can contain source text/paths) in upload records.
        root.update(status="partial", coverage="partial")
        root["issues"].append("decode_failed")
    if _stat(path) != before:
        return [_failure(dataset, item, "source_changed", parent_id=parent, unit=unit, position=position)]
    return rows


def _stream(stream, dataset, item, config, hint=None):
    hasher, size, stored = _native.Hasher(), 0, 0
    with tempfile.NamedTemporaryFile() as tmp:
        while block := stream.read(1 << 20):
            size += len(block)
            if size > config.limits.max_input_bytes:
                return [_failure(dataset, item, "input_size_limit")]
            hasher.update(block)
            if stored + len(block) <= config.limits.max_spool_bytes:
                tmp.write(block)
                stored += len(block)
        if stored != size:
            return [
                envelope(
                    dataset,
                    item,
                    size=size,
                    digest=hasher.hexdigest(),
                    status="partial",
                    coverage="partial",
                    issues=["spool_size_limit"],
                )
            ]
        tmp.flush()
        return _process_path(tmp.name, dataset, item, config, Expansion(config), hint=hint)


def _s3(source, dataset, item, config):
    from urllib.parse import urlsplit

    import boto3
    from botocore.config import Config as S3Config

    u = urlsplit(source)
    if u.scheme != "s3" or not u.netloc or not u.path.lstrip("/") or u.query or u.fragment:
        raise ValueError("invalid S3 source")
    client = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        config=S3Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=10, read_timeout=60),
    )
    args = {"Bucket": u.netloc, "Key": u.path.lstrip("/")}

    def request(method, **kwargs):
        stats = IO_STATS.get()
        try:
            response = getattr(client, method)(**kwargs)
        except Exception:
            if stats is not None:
                stats.update(storage_requests=None, retries=None)
            raise
        if stats is not None and stats["storage_requests"] is not None:
            retries = response.get("ResponseMetadata", {}).get("RetryAttempts", 0)
            stats["storage_requests"] += 1 + retries
            stats["retries"] += retries
        return response

    head = request("head_object", **args)
    if head["ContentLength"] > config.limits.max_input_bytes:
        return [_failure(dataset, item, "input_size_limit")]
    version = head.get("VersionId")
    pin = {"VersionId": version} if version and version != "null" else {"IfMatch": head["ETag"]}
    response = request("get_object", **args, **pin)
    try:
        rows = _stream(response["Body"], dataset, item, config, hint=u.path)
    finally:
        response["Body"].close()
    if not version or version == "null":
        after = request("head_object", **args)
        if any(after.get(k) != head.get(k) for k in ("ETag", "ContentLength", "LastModified")):
            return [_failure(dataset, item, "source_changed")]
    return rows


def _worker(source, dataset, item, config_data):
    config = config_from_dict(config_data)
    done = threading.Event()
    # OS-default SIGALRM terminates even a native decoder that holds Python's GIL.
    previous_alarm = signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, config.limits.timeout_seconds)
    previous_as = None
    if os.uname().sysname == "Linux":
        previous_as = resource.getrlimit(resource.RLIMIT_AS)
        hard = previous_as[1]
        bound = (
            config.limits.memory_bytes
            if hard == resource.RLIM_INFINITY
            else min(config.limits.memory_bytes, hard)
        )
        resource.setrlimit(resource.RLIMIT_AS, (bound, hard))

    def guard():
        start = time.monotonic()
        while not done.wait(0.1):
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if os.uname().sysname != "Darwin":
                rss *= 1024
            if time.monotonic() - start > config.limits.timeout_seconds or rss > config.limits.memory_bytes:
                os._exit(124)

    threading.Thread(target=guard, daemon=True).start()
    try:
        if str(source).startswith("s3://"):
            return _s3(source, dataset, item, config)
        if os.path.islink(source):
            return [
                envelope(dataset, item, status="unsupported", coverage="none", issues=["symlink_skipped"])
            ]
        return _process_path(source, dataset, item, config, Expansion(config))
    except Exception:
        return [_failure(dataset, item, "source_read_failed")]
    finally:
        done.set()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        if previous_as is not None:
            resource.setrlimit(resource.RLIMIT_AS, previous_as)


def fingerprint_file(source, *, config=None, dataset_id=None, item_id=None):
    """Return a bounded list of upload-safe envelopes, including logical children.

    Native decoders run in a disposable process. Use iter_fingerprints for a reused
    worker pool when processing many files. Source paths only live on the caller.
    """
    config = config or Config()
    dataset, item = dataset_id or opaque_id(), item_id or opaque_id()
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        try:
            return pool.submit(_worker, str(source), dataset, item, config_dict(config)).result()
        except concurrent.futures.process.BrokenProcessPool:
            return [_failure(dataset, item, "worker_terminated")]


def fingerprint_stream(stream, *, config=None, dataset_id=None, item_id=None, format_hint=None):
    """Caller streams are spooled locally with a hard disk bound, then decoded in isolation."""
    config = config or Config()
    dataset, item = dataset_id or opaque_id(), item_id or opaque_id()
    h, size = _native.Hasher(), 0
    with tempfile.TemporaryDirectory() as tmpdir:
        # Only the suffix is used, never a caller-controlled directory.
        path = Path(tmpdir) / ("input" + "".join(Path(format_hint or "").suffixes))
        with open(path, "wb") as out:
            while block := stream.read(1 << 20):
                size += len(block)
                if size > config.limits.max_input_bytes:
                    return [_failure(dataset, item, "input_size_limit")]
                h.update(block)
                if size <= config.limits.max_spool_bytes:
                    out.write(block)
        if size > config.limits.max_spool_bytes:
            return [
                envelope(
                    dataset,
                    item,
                    size=size,
                    digest=h.hexdigest(),
                    status="partial",
                    coverage="partial",
                    issues=["spool_size_limit"],
                )
            ]
        return fingerprint_file(path, config=config, dataset_id=dataset, item_id=item)


def fingerprint_record(value, *, fields, config=None, dataset_id=None, item_id=None):
    from dataclasses import replace

    config = replace(config or Config(), fields=fields)
    # Serialize binary values as explicit base64 fields; containers use the same mapping.
    data = json.dumps(value, default=lambda v: base64_encode(v), ensure_ascii=False).encode() + b"\n"
    rows = fingerprint_stream(
        io.BytesIO(data), config=config, dataset_id=dataset_id, item_id=item_id, format_hint="record.jsonl"
    )
    if len(rows) <= 1:
        return [
            _failure(
                rows[0]["dataset_id"], rows[0]["item_id"], "invalid_record", unit="record", modality="record"
            )
        ]
    record_id, previous = rows[0]["item_id"], rows[1]["item_id"]
    result = rows[1:]
    result[0]["item_id"], result[0]["parent_id"] = record_id, None
    for row in result[1:]:
        if row["parent_id"] == previous:
            row["parent_id"] = record_id
    return result


def base64_encode(value):
    import base64

    if not isinstance(value, bytes):
        raise TypeError("unsupported record value")
    return base64.b64encode(value).decode()


def iter_fingerprints(sources, *, config=None, dataset_id=None, workers=1):
    """Bounded in-flight scheduling, with stable input order and no eager source list."""
    from collections import deque

    if workers < 1:
        raise ValueError("workers must be positive")
    config, dataset = config or Config(), dataset_id or opaque_id()
    iterator = iter(sources)
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        pending = deque()
        exhausted = False
        while pending or not exhausted:
            while len(pending) < workers * 2 and not exhausted:
                try:
                    source = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                item = opaque_id()
                pending.append((item, pool.submit(_worker, str(source), dataset, item, config_dict(config))))
            if pending:
                item, job = pending.popleft()
                try:
                    yield from job.result()
                except concurrent.futures.process.BrokenProcessPool:
                    yield _failure(dataset, item, "worker_terminated")
                    for item, _ in pending:
                        yield _failure(dataset, item, "worker_terminated")
                    for _ in iterator:
                        yield _failure(dataset, opaque_id(), "worker_terminated")
                    return
