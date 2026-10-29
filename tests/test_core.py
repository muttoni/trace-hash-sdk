import io
import json

from trace_hash import (
    Config,
    Limits,
    _native,
    compare,
    fingerprint_file,
    fingerprint_stream,
    iter_fingerprints,
)


def test_official_vectors():
    assert _native.digest_bytes(b"") == "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    assert _native.digest_bytes(b"abc") == "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"


def test_exact_and_excerpts(fp, text):
    a = fp(text)[0]
    assert compare(a, fp(text, "renamed.txt")[0])["class"] == "byte_exact"
    altered = fp(text.replace("word200", "edited200"))[0]
    result = compare(a, altered)
    assert result["class"] == "uncertain" and result["proposed_class"] == "near_duplicate"
    assert not result["reportable"]
    crop = fp(" ".join(text.split()[200:450]))[0]
    result = compare(crop, a)
    assert result["proposed_class"] == "partial_overlap"
    assert result["query_segments"][0]["end"] <= 250


def test_code_whitespace_and_normalization():
    def f(s, code=False):
        return json.loads(_native.fingerprint_text(s, code, 10000))

    assert f("cafe\u0301\n hi")["canonical_digest"] == f("café  hi")["canonical_digest"]
    assert f("if x:\n  y()", True)["canonical_digest"] != f("if x:\ny()", True)["canonical_digest"]
    assert f("x = 'a'", True)["canonical_digest"] != f("x = 'A'", True)["canonical_digest"]


def test_html_and_low_information(fp, text):
    a = fp("<html><script>SECRET</script><p>" + text + "</p></html>", "a.html")[0]
    b = fp(text)[0]
    assert compare(a, b)["proposed_class"] == "canonical_exact"
    assert "SECRET" not in json.dumps(a)
    assert compare(fp("hello")[0], fp(" hello ")[0])["class"] == "indeterminate"


def test_incompatible_profiles(fp, text):
    a, b = fp(text)[0], fp(text + " end")[0]
    b["features"][0]["profile"] += ".different"
    assert compare(a, b)["class"] == "not_comparable"


def test_partial_record_limit_retains_full_digest(fp):
    data = b"x" * 2000
    row = fp(data, config=Config(limits=Limits(max_record_bytes=20)))[0]
    assert row["status"] == "partial"
    assert row["exact_digest"] == _native.digest_bytes(data)


def test_stream_and_workers(tmp_path, text):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text(text)
    b.write_text(text)
    one = fingerprint_file(a)[0]
    stream = fingerprint_stream(io.BytesIO(text.encode()), format_hint="x.txt")[0]
    assert one["features"] == stream["features"]
    two = list(iter_fingerprints([a, b], workers=2))
    assert two[0]["features"] == two[1]["features"] == one["features"]


def test_stream_spool_limit():
    data = b"abc" * 500
    rows = fingerprint_stream(io.BytesIO(data), config=Config(limits=Limits(max_spool_bytes=100)))
    assert rows[0]["issues"] == ["spool_size_limit"]
    assert rows[0]["exact_digest"] == _native.digest_bytes(data)


def test_invalid_limits():
    import pytest

    with pytest.raises(ValueError):
        Limits(max_depth=0)
    with pytest.raises(ValueError):
        Config(fields={"text": "text"})


def test_source_mutation_invalidates_all(monkeypatch, tmp_path, text):
    from trace_hash import sdk
    from trace_hash.adapters import Expansion
    from trace_hash.model import opaque_id

    path = tmp_path / "changing.txt"
    path.write_text(text)
    original = sdk.text_feature

    def changed(*args, **kwargs):
        path.write_text("changed during scan")
        return original(*args, **kwargs)

    monkeypatch.setattr(sdk, "text_feature", changed)
    rows = sdk._process_path(path, opaque_id(), opaque_id(), Config(), Expansion(Config()))
    assert len(rows) == 1 and rows[0]["exact_digest"] is None
    assert rows[0]["issues"] == ["source_changed"]
