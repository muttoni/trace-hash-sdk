from __future__ import annotations

import json
from importlib.resources import files

import jsonschema

MAX_LINE = 128 << 20
_validator = None


def validate_record(record: dict) -> None:
    global _validator
    if _validator is None:
        schema = json.loads(files("trace_hash").joinpath("manifest.schema.json").read_text())
        _validator = jsonschema.Draft202012Validator(schema)
    _validator.validate(record)
    if record["status"] == "failed" and record["exact_digest"] is not None:
        raise ValueError("failed records cannot retain usable exact digests")
    if record["coverage"] == "complete" and record["status"] in ("failed", "partial", "unsupported"):
        raise ValueError("incomplete processing cannot claim complete coverage")


def encode(record: dict) -> bytes:
    validate_record(record)
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def read_manifest(path):
    if str(path).endswith((".arrow", ".ipc")):
        import pyarrow.ipc as ipc

        with ipc.open_stream(str(path)) as reader:
            if reader.schema.names != ["record"]:
                raise ValueError("invalid Arrow manifest schema")
            for batch in reader:
                for value in batch.column(0).to_pylist():
                    if len(value) > MAX_LINE:
                        raise ValueError("manifest record limit")
                    record = json.loads(value)
                    validate_record(record)
                    yield record
    else:
        with open(path, "rb") as stream:
            while line := stream.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE:
                    raise ValueError("manifest record limit")
                record = json.loads(line)
                validate_record(record)
                yield record


def write_manifest(records, path):
    """Atomic replacement; Arrow carries the exact canonical JSON envelope per row."""
    import os
    import tempfile
    from pathlib import Path

    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            if str(path).endswith((".arrow", ".ipc")):
                import pyarrow as pa
                import pyarrow.ipc as ipc

                schema = pa.schema([("record", pa.large_binary())], metadata={b"trace.schema": b"1.0"})
                with ipc.new_stream(out, schema, options=ipc.IpcWriteOptions(compression="zstd")) as writer:
                    batch = []
                    size = 0
                    for record in records:
                        value = encode(record).rstrip(b"\n")
                        batch.append(value)
                        size += len(value)
                        if len(batch) >= 256 or size >= 8 << 20:
                            writer.write_batch(
                                pa.record_batch([pa.array(batch, type=pa.large_binary())], schema=schema)
                            )
                            batch, size = [], 0
                    if batch:
                        writer.write_batch(
                            pa.record_batch([pa.array(batch, type=pa.large_binary())], schema=schema)
                        )
            else:
                for record in records:
                    out.write(encode(record))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
