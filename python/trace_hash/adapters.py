"""Bounded container expansion. No archive extraction or remote URL dereferencing."""

from __future__ import annotations

import base64
import csv
import gzip
import io
import json
import stat
import tarfile
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from . import _native
from .model import LimitExceeded, child_id, envelope
from .text import text_feature

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".html", ".htm", ".xml"}
CODE_SUFFIXES = {
    ".py",
    ".rs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".java",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".sh",
    ".sql",
    ".css",
    ".swift",
}


def detect(path, hint=None):
    with open(path, "rb") as f:
        head = f.read(8192)
    suffix = Path(hint or path).suffix.lower()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PAR1"):
        return "parquet"
    if head.startswith((b"ARROW1", b"\xff\xff\xff\xff")):
        return "arrow"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        with zipfile.ZipFile(path) as z:
            return "docx" if "word/document.xml" in z.namelist() else "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head[257:262] == b"ustar" or suffix == ".tar":
        return "tar"
    if head.startswith(b"WARC/"):
        return "warc"
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"II*\x00", b"MM\x00*")) or head[8:12] == b"WEBP":
        return "image"
    if head.startswith((b"fLaC", b"ID3")) or head[8:12] == b"WAVE":
        return "audio"
    if head.startswith(b"OggS") or (len(head) > 1 and head[0] == 255 and head[1] & 224 == 224):
        return "audio"
    if head[4:8] == b"ftyp" or head.startswith(b"\x1aE\xdf\xa3"):
        return "audio" if suffix in (".m4a", ".aac") else "video"
    if suffix in (".jsonl", ".ndjson"):
        return "jsonl"
    if suffix == ".csv":
        return "csv"
    if suffix in CODE_SUFFIXES:
        return "code"
    if suffix in (".html", ".htm") or head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if suffix in TEXT_SUFFIXES:
        return "text"
    try:
        head.decode("utf-8")
        if b"\x00" not in head and (head or suffix in TEXT_SUFFIXES):
            return "text"
    except UnicodeDecodeError:
        pass
    return "binary"


class Expansion:
    def __init__(self, config):
        self.config = config
        self.bytes = 0
        self.count = 0

    def consume(self, size):
        self.bytes += size
        if self.bytes > self.config.limits.max_expanded_bytes:
            raise LimitExceeded("expansion_limit")

    def item(self):
        self.count += 1
        if self.count > self.config.limits.max_records:
            raise LimitExceeded("record_limit")

    def read(self, stream):
        data = stream.read(self.config.limits.max_record_bytes + 1)
        self.consume(len(data))
        if len(data) > self.config.limits.max_record_bytes:
            raise LimitExceeded("record_size_limit")
        return data


def pointer(value, path):
    for part in path[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def mapped_record(value, dataset, item, config, parent=None, position=None):
    if not config.fields:
        return [
            envelope(
                dataset,
                item,
                parent_id=parent,
                unit="record",
                modality="record",
                position=position,
                status="unsupported",
                coverage="none",
                issues=["field_mapping_required"],
            )
        ]
    mapping = json.dumps(config.fields, sort_keys=True, separators=(",", ":")).encode()
    selected, features, children = {}, [], []
    for index, (field, kind) in enumerate(sorted(config.fields.items())):
        field_value = pointer(value, field)
        binding = _native.digest_bytes(mapping + b"\0" + field.encode())[:24]
        if kind in ("text", "code"):
            if not isinstance(field_value, str):
                raise ValueError("mapped_field_type")
            data = field_value.encode()
            if len(data) > config.limits.max_record_bytes:
                raise LimitExceeded("record_size_limit")
            f = text_feature(data, config, code=kind == "code")
            f["profile"] += "+mapping-" + binding
            features.append(f)
            selected[field] = field_value
        else:
            if isinstance(field_value, str):
                data = base64.b64decode(field_value, validate=True)
            elif isinstance(field_value, bytes):
                data = field_value
            else:
                raise ValueError("mapped_field_type")
            if len(data) > config.limits.max_record_bytes:
                raise LimitExceeded("record_size_limit")
            selected[field] = {"blake3": _native.digest_bytes(data), "modality": kind}
            child = child_id(item, index)
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write(data)
                tmp.flush()
                from .sdk import _process_path

                rows = _process_path(
                    tmp.name,
                    dataset,
                    child,
                    config,
                    Expansion(config),
                    depth=1,
                    parent=item,
                    forced=kind,
                    unit="record",
                    position=index,
                )
                for row in rows:
                    for f in row["features"]:
                        f["profile"] += "+mapping-" + binding
                children.extend(rows)
    canonical = json.dumps(
        {"mapping": config.fields, "values": selected},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    root = envelope(
        dataset,
        item,
        parent_id=parent,
        unit="record",
        modality="record",
        position=position,
        size=len(canonical),
        digest=_native.digest_bytes(canonical),
        features=features,
        exact_basis="canonical_record",
    )
    if any(f["payload"].get("truncated") for f in features) or any(
        r["coverage"] != "complete" for r in children
    ):
        root.update(status="partial", coverage="partial", issues=["child_coverage_incomplete"])
    return [root, *children]


def children(path, kind, config, expansion, hint=None):
    """Yield (bytes or structured value, format hint, logical position, forced type)."""
    if kind in ("zip", "docx"):
        with zipfile.ZipFile(path) as archive:
            if kind == "docx":
                from defusedxml import ElementTree

                with archive.open("word/document.xml") as source:
                    xml = expansion.read(source)
                root = ElementTree.fromstring(xml)
                paragraphs = []
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                for p in root.iter(ns + "p"):
                    paragraphs.append("".join(t.text or "" for t in p.iter(ns + "t")))
                yield "\n".join(paragraphs).encode(), "document.txt", 0, "text"
            for index, info in enumerate(archive.infolist(), start=1):
                if info.is_dir() or (kind == "docx" and not info.filename.startswith("word/media/")):
                    continue
                expansion.item()
                if stat.S_ISLNK(info.external_attr >> 16):
                    yield b"", info.filename, index, "skip"
                    continue
                if info.flag_bits & 1:
                    raise ValueError("encrypted_archive")
                if info.file_size > config.limits.max_record_bytes:
                    raise LimitExceeded("record_size_limit")
                with archive.open(info) as source:
                    yield expansion.read(source), info.filename, index, None
    elif kind == "tar":
        with tarfile.open(path, mode="r|") as archive:
            for index, info in enumerate(archive):
                if not info.isfile():
                    if not info.isdir():
                        expansion.item()
                        yield b"", info.name, index, "skip"
                    continue  # never follow symlinks, hardlinks, devices or paths
                expansion.item()
                if info.size > config.limits.max_record_bytes:
                    raise LimitExceeded("record_size_limit")
                with archive.extractfile(info) as source:
                    yield expansion.read(source), info.name, index, None
    elif kind == "gzip":
        # Spool rather than hold the decompressed container in RAM. Budget applies to expanded bytes.
        with gzip.open(path) as source, tempfile.NamedTemporaryFile() as tmp:
            while block := source.read(1 << 20):
                expansion.consume(len(block))
                tmp.write(block)
            tmp.flush()
            yield Path(tmp.name), Path(hint or path).stem, 0, None
    elif kind == "jsonl":
        with open(path, "rb") as source:
            index = 0
            while line := source.readline(config.limits.max_record_bytes + 1):
                expansion.consume(len(line))
                if len(line) > config.limits.max_record_bytes:
                    raise LimitExceeded("record_size_limit")
                if not line.strip():
                    continue
                expansion.item()
                yield json.loads(line), None, index, "record"
                index += 1
    elif kind == "csv":
        csv.field_size_limit(config.limits.max_record_bytes)
        with open(path, newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames and len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("duplicate_csv_headers")
            for index, row in enumerate(reader):
                expansion.item()
                size = sum(len(str(v).encode()) for v in row.values())
                if size > config.limits.max_record_bytes:
                    raise LimitExceeded("record_size_limit")
                expansion.consume(size)
                yield row, None, index, "record"
    elif kind in ("parquet", "arrow"):
        import pyarrow as pa
        import pyarrow.ipc as ipc
        import pyarrow.parquet as pq

        with pa.OSFile(str(path), "rb") as source:
            if kind == "parquet":
                batches = pq.ParquetFile(source).iter_batches(batch_size=64)
            else:
                try:
                    reader = ipc.open_file(source)
                    batches = (reader.get_batch(i) for i in range(reader.num_record_batches))
                except pa.ArrowInvalid:
                    source.seek(0)
                    batches = ipc.open_stream(source)
            index = 0
            for batch in batches:
                expansion.consume(batch.nbytes)
                for row in batch.to_pylist():
                    expansion.item()
                    yield row, None, index, "record"
                    index += 1
    elif kind == "warc":
        from warcio.archiveiterator import ArchiveIterator

        with open(path, "rb") as source:
            for index, record in enumerate(ArchiveIterator(source)):
                if record.rec_type not in ("response", "resource", "conversion"):
                    continue
                expansion.item()
                data = expansion.read(record.content_stream())
                yield data, "record.html" if b"<html" in data[:8192].lower() else "record.txt", index, None
    elif kind == "pdf":
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(path) as document:
            if len(document) > config.limits.max_pages:
                raise LimitExceeded("page_limit")
            for index in range(len(document)):
                expansion.item()
                with closing(document[index]) as page:
                    with closing(page.get_textpage()) as textpage:
                        text = textpage.get_text_range()
                    if text.strip():
                        raw = text.encode()
                        expansion.consume(len(raw))
                        yield raw, "page.txt", index * 2, "text"
                    width, height = page.get_size()
                    if width * height > config.limits.max_pixels:
                        raise LimitExceeded("pixel_limit")
                    # Render every page, covering scans, embedded images and vector figures alike.
                    bitmap = page.render(scale=1)
                    try:
                        out = io.BytesIO()
                        bitmap.to_pil().save(out, format="PNG")
                        data = out.getvalue()
                        expansion.consume(len(data))
                        yield data, "page.png", index * 2 + 1, "image"
                    finally:
                        bitmap.close()
