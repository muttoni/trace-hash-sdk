import json

import httpx
import pytest
from trace_hash.client import TraceClient, TraceError
from trace_hash.reference import ReferenceRegistry


def client(registry, token):
    return TraceClient("https://trace.invalid", token, transport=registry.transport(), retries=0)


def submit(c, rows, revision=1, usage="declared_used_for_training"):
    return c.submit(rows, dataset_id=rows[0]["dataset_id"], revision=revision, usage=usage)


def test_org_counts_self_exclusion_withdrawal_and_idempotency(fp, text):
    rows = fp(text)
    registry = ReferenceRegistry({"a": ("org-a", "lab"), "b": ("org-b", "lab"), "v": ("vendor", "vendor")})
    a, b, v = client(registry, "a"), client(registry, "b"), client(registry, "v")
    assert submit(a, rows)["state"] == "searchable"
    submit(a, rows)
    submit(v, rows, usage="offered_for_sale")
    checked = b.check(rows)
    assert checked["results"][0]["other_labs_by_usage"]["declared_used_for_training"] == 1
    assert a.check(rows)["results"][0]["other_labs_by_usage"]["declared_used_for_training"] == 0
    submit(b, rows)
    assert a.check(rows)["results"][0]["other_labs_by_usage"]["declared_used_for_training"] == 1
    b.withdraw(rows[0]["dataset_id"], 1)
    assert a.check(rows)["results"][0]["other_labs_by_usage"]["declared_used_for_training"] == 0
    assert "org-a" not in json.dumps(checked) and "vendor" not in json.dumps(checked)
    assert text not in json.dumps(checked)


def test_checks_do_not_register_and_fuzzy_not_counted(fp, text):
    registry = ReferenceRegistry({"a": ("org-a", "lab"), "b": ("org-b", "lab")})
    a, b = client(registry, "a"), client(registry, "b")
    rows = fp(text)
    submit(a, rows)
    query = fp(text.replace("word200", "edited200"))
    result = b.check(query)
    assert result["results"][0]["class"] == "uncertain"
    assert all(n == 0 for n in result["results"][0]["other_labs_by_usage"].values())
    assert len(registry.revisions) == 1


def test_revision_replaces_old_counts(fp, text):
    registry = ReferenceRegistry({"a": ("org-a", "lab"), "b": ("org-b", "lab")})
    a, b = client(registry, "a"), client(registry, "b")
    rows = fp(text)
    submit(a, rows)
    submit(a, rows, revision=2, usage="planned_for_training")
    counts = b.check(rows)["results"][0]["other_labs_by_usage"]
    assert counts["declared_used_for_training"] == 0 and counts["planned_for_training"] == 1


def test_https_only_and_no_redirect_following(fp):
    with pytest.raises(ValueError):
        TraceClient("http://example.test", "secret")
    with pytest.raises(ValueError):
        TraceClient("https://user:pass@example.test", "secret")
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.invalid"})

    c = TraceClient("https://trace.invalid", "secret", transport=httpx.MockTransport(redirect), retries=0)
    with pytest.raises(TraceError):
        c.check(fp("hello"))
    assert len(calls) == 1


def test_upload_sanitization(fp):
    rows = fp("private text")
    rows[0]["source_path"] = "/private"
    calls = []
    c = TraceClient(
        "https://trace.invalid", "secret", transport=httpx.MockTransport(lambda r: calls.append(r)), retries=0
    )
    with pytest.raises(Exception):
        submit(c, rows)
    assert not calls


def test_uncertain_counts_rejected(fp):
    rows = fp("hello")

    def bad(request):
        return httpx.Response(
            200,
            json={
                "snapshot_id": "snapshot",
                "coverage_watermark": "watermark",
                "complete": True,
                "results": [
                    {
                        "item_id": rows[0]["item_id"],
                        "class": "uncertain",
                        "other_labs_by_usage": {"declared_used_for_training": 1},
                    }
                ],
            },
        )

    c = TraceClient("https://trace.invalid", "secret", transport=httpx.MockTransport(bad))
    with pytest.raises(TraceError):
        c.check(rows)


def test_hot_buckets_are_indeterminate(fp, text):
    registry = ReferenceRegistry({"a": ("org-a", "lab"), "b": ("org-b", "lab")}, candidate_limit=1)
    a, b = client(registry, "a"), client(registry, "b")
    submit(a, fp(text))
    submit(a, fp(text + " suffix"))
    result = b.check(fp(text + " third suffix"))
    assert result["results"][0]["class"] == "indeterminate"


def test_query_quota(fp):
    registry = ReferenceRegistry({"a": ("org", "lab")}, query_budget=0)
    with pytest.raises(TraceError):
        client(registry, "a").check(fp("hello"))
