import gzip
import io
import json
import tarfile
import zipfile

from trace_hash import Config, Limits, fingerprint_record


def test_jsonl_mapping_and_repack(fp, text):
    config = Config(fields={"/text": "text"})
    rows = fp(json.dumps({"text": text, "private_path": "/secret/file"}) + "\n", "data.jsonl", config)
    assert len(rows) == 2
    assert rows[1]["exact_basis"] == "canonical_record"
    assert "/secret/file" not in json.dumps(rows)
    assert "word100" not in json.dumps(rows)
    assert rows[1]["parent_id"] == rows[0]["item_id"]
    changed = fp(json.dumps({"other": text}) + "\n", "other.jsonl", Config(fields={"/other": "text"}))
    assert rows[1]["features"][0]["profile"] != changed[1]["features"][0]["profile"]


def test_missing_and_bad_fields_explicit(fp):
    rows = fp('{"text":"hello"}\n', "data.jsonl")
    assert rows[1]["issues"] == ["field_mapping_required"]
    rows = fp('{"wrong":"hello"}\n', "data.jsonl", Config(fields={"/text": "text"}))
    assert rows[1]["status"] == "failed"
    assert rows[0]["coverage"] == "partial"


def test_parquet_arrow_csv_equivalence(fp, text):
    import pyarrow as pa
    import pyarrow.parquet as pq

    config = Config(fields={"/text": "text"})
    expected = fp(json.dumps({"text": text}), "a.jsonl", config)[1]
    table = pa.table({"text": [text]})
    out = io.BytesIO()
    pq.write_table(table, out)
    parquet = fp(out.getvalue(), "a.parquet", config)[1]
    out = io.BytesIO()
    with pa.ipc.new_stream(out, table.schema) as writer:
        writer.write_table(table)
    arrow = fp(out.getvalue(), "a.arrow", config)[1]
    csv = fp("text\n" + text + "\n", "a.csv", config)[1]
    assert expected["features"] == parquet["features"] == arrow["features"] == csv["features"]
    assert expected["exact_digest"] == parquet["exact_digest"] == csv["exact_digest"]


def test_archives_gzip_and_traversal(fp, text, tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("../../escape.txt", text)
    zipped = fp(out.getvalue(), "a.zip")
    assert len(zipped) == 2 and not (tmp_path.parent / "escape.txt").exists()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        entry = tarfile.TarInfo("different.txt")
        entry.size = len(text.encode())
        archive.addfile(entry, io.BytesIO(text.encode()))
    tarred = fp(out.getvalue(), "a.tar")
    assert zipped[1]["features"] == tarred[1]["features"]
    gz = fp(gzip.compress(text.encode()), "a.txt.gz")
    assert gz[1]["features"] == zipped[1]["features"]


def test_archive_budget(fp):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.txt", "x" * 100000)
    row = fp(out.getvalue(), "bomb.zip", Config(limits=Limits(max_record_bytes=100)))[0]
    assert row["issues"] == ["record_size_limit"]
    assert row["coverage"] == "partial"


def test_docx(fp, text):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    rows = fp(out.getvalue(), "a.docx")
    assert rows[0]["modality"] == "document"
    assert rows[1]["features"] == fp(text)[0]["features"]
    assert rows[0]["coverage"] == "partial"


def test_public_record_interface(text):
    rows = fingerprint_record({"text": text}, fields={"/text": "text"})
    assert len(rows) == 1 and rows[0]["unit"] == "record"
    assert rows[0]["parent_id"] is None
    bad = fingerprint_record({"wrong": text}, fields={"/text": "text"})
    assert bad and bad[0]["status"] == "failed"


def test_warc_resource(fp, text):
    from warcio.warcwriter import WARCWriter

    out = io.BytesIO()
    writer = WARCWriter(out, gzip=False)
    record = writer.create_warc_record(
        "https://private.example/source", "resource", payload=io.BytesIO(text.encode())
    )
    writer.write_record(record)
    rows = fp(out.getvalue(), "data.warc")
    assert len(rows) == 2 and rows[1]["features"] == fp(text)[0]["features"]
    assert "private.example" not in json.dumps(rows)


def test_tar_links_explicitly_skipped(fp):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "/secret/outside"
        archive.addfile(link)
    rows = fp(out.getvalue(), "links.tar")
    assert rows[0]["coverage"] == "partial"
    assert rows[1]["issues"] == ["non_regular_entry_skipped"]
