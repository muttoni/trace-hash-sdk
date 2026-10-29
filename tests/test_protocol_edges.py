import json
import uuid

import httpx
import pytest
from trace_hash.client import TraceClient
from trace_hash.reference import ReferenceRegistry


def test_summary_is_union_not_sum(fp, text):
    registry = ReferenceRegistry({"lab": ("lab", "lab"), "buyer": ("buyer", "lab")})
    rows = fp(text)
    with TraceClient("https://trace.invalid", "lab", transport=registry.transport()) as c:
        c.submit(rows, dataset_id=rows[0]["dataset_id"], revision=1, usage="declared_used_for_training")
    a, b = fp(text)[0], fp(text)[0]
    with TraceClient("https://trace.invalid", "buyer", transport=registry.transport()) as c:
        result = c.check([a, b])
    assert sum(r["other_labs_by_usage"]["declared_used_for_training"] for r in result["results"]) == 2
    assert result["summary"]["distinct_other_labs"] == 1


def test_partial_index_and_private_response_projection(fp):
    rows = fp("hello")
    snapshot = str(uuid.uuid4())

    def handler(request):
        assert "source_path" not in json.loads(request.content)["records"][0]
        return httpx.Response(
            200,
            json={
                "snapshot_id": snapshot,
                "coverage_watermark": snapshot,
                "complete": False,
                "organization": "PRIVATE LAB",
                "results": [
                    {
                        "item_id": rows[0]["item_id"],
                        "class": "no_match",
                        "other_labs_by_usage": {},
                        "fingerprint": "PRIVATE FINGERPRINT",
                        "source_path": "PRIVATE PATH",
                    }
                ],
            },
        )

    with TraceClient("https://trace.invalid", "token", transport=httpx.MockTransport(handler)) as c:
        result = c.check(rows)
    assert result["results"][0]["class"] == "indeterminate"
    assert "PRIVATE" not in json.dumps(result)


def test_retries_have_identical_idempotency_keys(monkeypatch, fp):
    rows = fp("hello")
    calls = []
    monkeypatch.setattr("trace_hash.client.time.sleep", lambda _: None)

    def handler(request):
        calls.append((request.url.path, request.headers["Idempotency-Key"], request.content))
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"state": "durable"})

    with TraceClient("https://trace.invalid", "token", transport=httpx.MockTransport(handler)) as c:
        c.submit(rows, dataset_id=rows[0]["dataset_id"], revision=1, usage="under_evaluation")
    assert calls[0] == calls[1]


def test_partial_upload_not_searchable(fp, text):
    registry = ReferenceRegistry({"lab": ("lab", "lab"), "buyer": ("buyer", "lab")})
    rows = fp(text)
    with TraceClient("https://trace.invalid", "lab", transport=registry.transport()) as c:
        c._request(
            "PUT",
            f"v1/datasets/{rows[0]['dataset_id']}/revisions/1/batches/0",
            {"records": rows, "usage_assertion": "declared_used_for_training"},
        )
    with TraceClient("https://trace.invalid", "buyer", transport=registry.transport()) as c:
        result = c.check(rows)
    assert result["summary"]["distinct_other_labs"] == 0


def test_check_iterator_bounded(fp):
    row = fp("hello")[0]
    count = 0

    def endless():
        nonlocal count
        while True:
            count += 1
            yield row

    with TraceClient("https://trace.invalid", "token") as c:
        with pytest.raises(ValueError):
            c.check(endless())
    assert count == 257
