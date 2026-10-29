import os
import subprocess

import pytest
from trace_hash.manifest import read_manifest, validate_record, write_manifest
from trace_hash.scan import scan


def test_manifest_roundtrip_and_privacy(tmp_path, fp, text):
    rows = fp(text)
    for suffix in ("jsonl", "arrow"):
        path = tmp_path / ("manifest." + suffix)
        write_manifest(rows, path)
        assert list(read_manifest(path)) == rows
    rows[0]["source_path"] = "/secrets/data"
    with pytest.raises(Exception):
        validate_record(rows[0])


def test_failed_cannot_have_digest(fp):
    row = fp("abc")[0]
    row["status"], row["coverage"] = "failed", "none"
    with pytest.raises(ValueError):
        validate_record(row)


def test_scan_checkpoint_changed_content_and_deletion(tmp_path, text):
    directory = tmp_path / "inputs"
    directory.mkdir()
    a, b = directory / "a.txt", directory / "b.txt"
    a.write_text(text)
    b.write_text(text + " b")
    output = tmp_path / "manifest.jsonl"
    first = scan([directory], output, workers=2)
    records = list(read_manifest(output))
    second = scan([directory], output, workers=1)
    assert first["dataset_id"] == second["dataset_id"]
    assert second["counts"]["cache_hits"] == 2
    assert list(read_manifest(output)) == records
    old = a.stat()
    # Same size and restored mtime must still invalidate the cache.
    a.write_text(text.replace("word100", "WORD100"))
    os.utime(a, ns=(old.st_atime_ns, old.st_mtime_ns))
    b.unlink()
    third = scan([directory], output)
    assert third["counts"].get("cache_hits", 0) == 0
    assert third["counts"]["discovered"] == 1
    assert len(list(read_manifest(output))) == 1


def test_symlinks_not_followed(tmp_path, text):
    a = tmp_path / "a.txt"
    a.write_text(text)
    link = tmp_path / "link.txt"
    link.symlink_to(a)
    out = tmp_path / "out.jsonl"
    summary = scan([link], out)
    assert summary["counts"]["unsupported"] == 1
    assert list(read_manifest(out))[0]["exact_digest"] is None


def test_cli_validate_and_cycle(tmp_path, fp, text):
    rows = fp(text)
    path = tmp_path / "manifest.jsonl"
    write_manifest(rows, path)
    result = subprocess.run([".venv/bin/trace-hash", "validate", str(path)], capture_output=True)
    assert result.returncode == 0
    rows[0]["parent_id"] = rows[0]["item_id"]
    write_manifest(rows, path)
    result = subprocess.run([".venv/bin/trace-hash", "validate", str(path)], capture_output=True)
    assert result.returncode == 2


def test_scan_never_overwrites_input(tmp_path):
    path = tmp_path / "original.txt"
    path.write_text("valuable source data")
    with pytest.raises(ValueError):
        scan([path], path)
    assert path.read_text() == "valuable source data"


def test_atomic_writer_does_not_follow_predictable_temp_symlink(tmp_path, fp):
    source = tmp_path / "source.txt"
    source.write_text("preserve me")
    output = tmp_path / "manifest.jsonl"
    (tmp_path / "manifest.jsonl.partial").symlink_to(source)
    write_manifest(fp("hello"), output)
    assert source.read_text() == "preserve me"
