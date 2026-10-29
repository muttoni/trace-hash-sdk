"""Durable, resumable local scheduling. Checkpoints contain LOCAL source metadata."""

from __future__ import annotations

import concurrent.futures
import fcntl
import importlib.metadata
import json
import multiprocessing
import os
import resource
import sqlite3
import time
from collections import Counter, deque
from pathlib import Path

from . import _native
from .manifest import encode, write_manifest
from .model import VERSION, Config, config_dict, opaque_id
from .sdk import IO_STATS, _failure, _stat, _worker


def provenance():
    versions = {}
    for name in ("Pillow", "numpy", "pdqhash", "av", "pyarrow", "pypdfium2", "warcio"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"sdk": VERSION, "dependencies": versions}


def discover(inputs, excludes=()):
    excluded = {str(Path(x).absolute()) for x in excludes}

    def visit(path):
        if str(path.absolute()) in excluded:
            return
        if path.is_dir() and not path.is_symlink():
            with os.scandir(path) as entries:
                for entry in entries:
                    yield from visit(Path(entry.path))
        else:
            yield str(path.absolute())

    for value in inputs:
        if str(value).startswith("s3://"):
            yield str(value)
        else:
            yield from visit(Path(value))


def _timed_worker(*args):
    start, usage = time.perf_counter(), resource.getrusage(resource.RUSAGE_SELF)
    io = {"storage_requests": 0, "retries": 0}
    token = IO_STATS.set(io)
    try:
        rows = _worker(*args)
    finally:
        IO_STATS.reset(token)
    end = resource.getrusage(resource.RUSAGE_SELF)
    rss = end.ru_maxrss * (1 if os.uname().sysname == "Darwin" else 1024)
    return rows, {
        **io,
        "wall_seconds": time.perf_counter() - start,
        "cpu_seconds": end.ru_utime + end.ru_stime - usage.ru_utime - usage.ru_stime,
        "peak_rss_bytes": rss,
    }


def scan(
    inputs, output, *, checkpoint=None, config=None, dataset_id=None, workers=1, shard_index=0, shard_count=1
):
    if workers < 1 or shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid worker or shard configuration")
    config = config or Config()
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = Path(checkpoint or str(output) + ".local.sqlite").absolute()
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    if output.resolve() == checkpoint.resolve() or output.is_symlink() or checkpoint.is_symlink():
        raise ValueError("output and checkpoint must be distinct, non-symlink files")

    # Inputs are an iterable: reject collisions lazily without loading an inventory.
    def checked_inputs():
        for value in inputs:
            if not str(value).startswith("s3://") and Path(value).resolve() in (
                output.resolve(),
                checkpoint.resolve(),
            ):
                raise ValueError("output/checkpoint must not overwrite an explicit input")
            yield value

    key = _native.digest_bytes(
        json.dumps({"config": config_dict(config), "build": provenance()}, sort_keys=True).encode()
    )
    start, counts = time.perf_counter(), Counter()
    parent_start = resource.getrusage(resource.RUSAGE_SELF)
    run = opaque_id()
    totals = {
        "input_bytes": 0,
        "fingerprint_bytes": 0,
        "cpu_seconds": 0.0,
        "peak_worker_rss_bytes": 0,
        "storage_requests": 0,
        "retries": 0,
        "media_seconds": 0.0,
    }
    lock_path = str(checkpoint) + ".lock"
    with open(lock_path, "a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = os.open(checkpoint, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with sqlite3.connect(checkpoint) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(source TEXT PRIMARY KEY, item TEXT NOT NULL, config TEXT, digest TEXT, rows TEXT, run TEXT)"
            )
            saved = db.execute("SELECT value FROM meta WHERE key='dataset'").fetchone()
            if saved and dataset_id and saved[0] != dataset_id:
                raise ValueError("checkpoint belongs to a different dataset")
            dataset = saved[0] if saved else dataset_id or opaque_id()
            db.execute("INSERT OR REPLACE INTO meta VALUES('dataset', ?)", (dataset,))
            db.commit()
            exclusions = [
                output,
                str(output) + ".partial",
                checkpoint,
                str(checkpoint) + "-wal",
                str(checkpoint) + "-shm",
                lock_path,
            ]
            sources = discover(checked_inputs(), exclusions)
            pending = deque()
            ctx = multiprocessing.get_context("spawn")

            def commit(source, item, rows, metrics):
                wire = b"".join(encode(row) for row in rows)
                root = rows[0]
                db.execute(
                    "UPDATE jobs SET config=?, digest=?, rows=?, run=? WHERE source=?",
                    (key, root["exact_digest"], wire.decode(), run, source),
                )
                db.commit()
                counts[root["status"]] += 1
                counts["logical_items"] += len(rows)
                totals["input_bytes"] += root["byte_length"]
                totals["fingerprint_bytes"] += len(wire)
                totals["cpu_seconds"] += metrics.get("cpu_seconds", 0)
                for metric in ("storage_requests", "retries"):
                    value = metrics.get(metric, None if root["status"] == "failed" else 0)
                    if value is None or totals[metric] is None:
                        totals[metric] = None
                    else:
                        totals[metric] += value
                totals["peak_worker_rss_bytes"] = max(
                    totals["peak_worker_rss_bytes"], metrics.get("peak_rss_bytes", 0)
                )
                totals["media_seconds"] += sum(
                    f["payload"]["samples"] / 11025
                    for r in rows
                    for f in r["features"]
                    if f["kind"] == "audio"
                )

            with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                for source in sources:
                    shard = int(_native.digest_bytes(source.encode())[:16], 16) % shard_count
                    if shard != shard_index:
                        continue
                    existing = db.execute(
                        "SELECT item, config, digest, rows, run FROM jobs WHERE source=?", (source,)
                    ).fetchone()
                    if existing and existing[4] == run:
                        continue
                    counts["discovered"] += 1
                    item = existing[0] if existing else opaque_id()
                    db.execute("INSERT OR IGNORE INTO jobs(source,item) VALUES (?,?)", (source, item))
                    db.execute("UPDATE jobs SET run=? WHERE source=?", (run, source))
                    db.commit()
                    # Never trust mtime or ETag as a content digest. Local cache hits re-read all bytes.
                    if (
                        existing
                        and existing[1] == key
                        and existing[2]
                        and not source.startswith("s3://")
                        and not os.path.islink(source)
                    ):
                        try:
                            before = _stat(source)
                            digest, _ = _native.digest_file(source)
                            if digest == existing[2] and before == _stat(source):
                                cached = [json.loads(line) for line in existing[3].splitlines()]
                                if all(r["status"] != "failed" for r in cached):
                                    commit(source, item, cached, {})
                                    counts["cache_hits"] += 1
                                    continue
                        except OSError:
                            pass
                    pending.append(
                        (source, item, pool.submit(_timed_worker, source, dataset, item, config_dict(config)))
                    )
                    if len(pending) >= workers * 2:
                        source, item, future = pending.popleft()
                        try:
                            rows, metrics = future.result()
                        except concurrent.futures.process.BrokenProcessPool:
                            commit(source, item, [_failure(dataset, item, "worker_terminated")], {})
                            raise RuntimeError(
                                "worker terminated; completed results are checkpointed; resume the scan"
                            ) from None
                        commit(source, item, rows, metrics)
                while pending:
                    source, item, future = pending.popleft()
                    try:
                        rows, metrics = future.result()
                    except concurrent.futures.process.BrokenProcessPool:
                        commit(source, item, [_failure(dataset, item, "worker_terminated")], {})
                        raise RuntimeError("worker terminated; resume from checkpoint") from None
                    commit(source, item, rows, metrics)

            def records():
                for row in db.execute("SELECT rows FROM jobs WHERE run=? ORDER BY item", (run,)):
                    if row[0]:
                        for line in row[0].splitlines():
                            yield json.loads(line)

            write_manifest(records(), output)
    elapsed = time.perf_counter() - start
    parent_end = resource.getrusage(resource.RUSAGE_SELF)
    totals["worker_cpu_seconds"] = totals["cpu_seconds"]
    totals["parent_cpu_seconds"] = (
        parent_end.ru_utime + parent_end.ru_stime - parent_start.ru_utime - parent_start.ru_stime
    )
    totals["cpu_seconds"] += totals["parent_cpu_seconds"]
    return {
        "dataset_id": dataset,
        "counts": dict(counts),
        "metrics": {
            **totals,
            "elapsed_seconds": elapsed,
            "input_gb_per_second": totals["input_bytes"] / 1e9 / elapsed,
            "items_per_second": counts["discovered"] / elapsed,
            "media_hours_per_compute_hour": totals["media_seconds"] / totals["cpu_seconds"]
            if totals["cpu_seconds"]
            else None,
        },
        "provenance": provenance(),
        "coverage_note": "Counts include every discovered source in this shard; cache hits re-read source bytes.",
    }
