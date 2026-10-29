import json
from pathlib import Path

from trace_hash import _native


def test_published_text_vectors():
    vectors = json.loads((Path(__file__).parent / "vectors/text-v0.json").read_text())
    for vector in vectors:
        assert (
            json.loads(_native.fingerprint_text(vector["text"], vector["code"], 500000)) == vector["expected"]
        )


def test_packaged_schema_identical():
    root = Path(__file__).resolve().parents[1]
    assert (root / "schemas/manifest.schema.json").read_bytes() == (
        root / "python/trace_hash/manifest.schema.json"
    ).read_bytes()
