"""In-memory protocol fixture, NOT a production registry or public HTTP server.

Uses exact and feature indexes. Identity comes from an injected token directory.
All state disappears when the fixture is destroyed. This is for integration tests.
"""

import json
import uuid
from collections import defaultdict

import httpx

from . import _native
from .client import USAGE
from .manifest import encode, validate_record
from .matching import compare
from .model import POLICY


def _keys(row):
    for f in row["features"]:
        p, value = f["profile"], f["payload"]
        if f["kind"] == "text":
            yield (p, "canonical", value["canonical_digest"])
            for i in range(0, len(value["minhash"]), 8):
                yield (p, str(i), _native.digest_bytes("".join(value["minhash"][i : i + 8]).encode()))
            for anchor in value["anchors"]:
                yield (p, "anchor", anchor["hash"])
        elif f["kind"] == "audio":
            for anchor in value["landmarks"]:
                yield (p, "landmark", anchor["hash"])
        elif f["kind"] in ("image", "video"):
            hashes = [value["hash"]] if f["kind"] == "image" else [frame["hash"] for frame in value["frames"]]
            for h in hashes:
                for band in range(32):
                    yield (p, str(band), h[band * 2 : band * 2 + 2])


class ReferenceRegistry:
    def __init__(self, identities, *, candidate_limit=1000, query_budget=10000):
        """identities maps tokens to (opaque organization ID, 'lab' or 'vendor')."""
        if any(role not in ("lab", "vendor") for _, role in identities.values()):
            raise ValueError("invalid organization role")
        self.identities = identities
        self.candidate_limit = candidate_limit
        self.query_budget = query_budget
        self.queries = defaultdict(int)
        self.staged, self.revisions, self.active = {}, {}, {}
        self.exact, self.features = defaultdict(set), defaultdict(set)
        self.rows = {}
        self.snapshot = str(uuid.uuid4())

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _reindex(self):
        self.rows, self.exact, self.features = {}, defaultdict(set), defaultdict(set)
        for (org, dataset), revision in self.active.items():
            data = self.revisions[(org, dataset, revision)]
            if data["withdrawn"]:
                continue
            for row in data["rows"]:
                rid = (org, dataset, revision, row["item_id"])
                self.rows[rid] = row
                if row["exact_digest"]:
                    self.exact[(row["exact_basis"], row["exact_digest"])].add(rid)
                for key in _keys(row):
                    self.features[key].add(rid)
        self.snapshot = str(uuid.uuid4())

    def _check(self, records, org):
        result, unions, all_labs = [], {u: set() for u in USAGE}, set()
        for row in records:
            validate_record(row)
            usage_labs = {u: set() for u in USAGE}
            exact = self.exact.get((row["exact_basis"], row["exact_digest"]), set())
            kind, candidate = "indeterminate", None
            for rid in exact:
                other = rid[0]
                if other == org:
                    continue
                if row["exact_basis"] != "raw_bytes":
                    kind = "uncertain"
                    continue
                kind = "byte_exact"
                record = self.revisions[rid[:3]]
                if record["role"] == "lab":
                    usage_labs[record["usage"]].add(other)
            limitations = []
            if kind not in ("byte_exact", "uncertain"):
                candidates = set()
                limited = False
                for key in _keys(row):
                    ids = self.features.get(key, set())
                    # Bound retrieval BEFORE copying a hot bucket.
                    if len(ids) > self.candidate_limit:
                        limited = True
                        break
                    candidates.update(rid for rid in ids if rid[0] != org)
                    if len(candidates) > self.candidate_limit:
                        limited = True
                        break
                if limited:
                    limitations.append("candidate_limit")
                else:
                    for rid in sorted(candidates):
                        evidence = compare(row, self.rows[rid])
                        if evidence["class"] == "uncertain":
                            kind, candidate = "uncertain", evidence
                            break
                    # v0 has no calibrated fuzzy negative coverage; cannot assert corpus uniqueness.
                    if kind == "indeterminate":
                        limitations.append("experimental_profile")
            for usage, labs in usage_labs.items():
                unions[usage].update(labs)
                all_labs.update(labs)
            result.append(
                {
                    "item_id": row["item_id"],
                    "class": kind,
                    "other_labs_by_usage": {u: len(v) for u, v in usage_labs.items()},
                    "profiles": [f["profile"] for f in row["features"]],
                    "query_segments": candidate["query_segments"] if candidate else [],
                    "limitations": limitations,
                    "policy_version": POLICY,
                }
            )
        return {
            "snapshot_id": self.snapshot,
            "coverage_watermark": self.snapshot,
            "complete": True,
            "results": result,
            "summary": {
                "distinct_other_labs": len(all_labs),
                "other_labs_by_usage": {u: len(v) for u, v in unions.items()},
            },
            "comparison_scope": "byte-exact reportable; fuzzy profiles experimental",
        }

    def handle(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.identities:
            return httpx.Response(401, json={"error": "unauthorized"})
        org, role = self.identities[token]
        try:
            parts = request.url.path.strip("/").split("/")
            body = json.loads(request.content) if request.content else {}
            if parts == ["v1", "checks"] and request.method == "POST":
                records = body["records"]
                self.queries[org] += len(records)
                if self.queries[org] > self.query_budget:
                    return httpx.Response(429, json={"error": "query_budget"})
                return httpx.Response(200, json=self._check(records, org))
            if len(parts) < 5 or parts[:2] != ["v1", "datasets"] or parts[3] != "revisions":
                return httpx.Response(404)
            dataset, revision = parts[2], int(parts[4])
            key = (org, dataset, revision)
            if revision < 1:
                return httpx.Response(400)
            if len(parts) == 7 and parts[5] == "batches" and request.method == "PUT":
                if key in self.revisions:
                    # A retry must carry the same previously staged batch.
                    previous = self.staged.get(key, {}).get(int(parts[6]))
                    return httpx.Response(200 if previous == body else 409, json={"state": "durable"})
                if body["usage_assertion"] not in USAGE:
                    return httpx.Response(400)
                for row in body["records"]:
                    validate_record(row)
                    if row["dataset_id"] != dataset:
                        return httpx.Response(400)
                batches = self.staged.setdefault(key, {})
                index = int(parts[6])
                if index in batches and batches[index] != body:
                    return httpx.Response(409)
                batches[index] = body
                return httpx.Response(202, json={"state": "durable"})
            if len(parts) == 6 and parts[5] == "commit" and request.method == "POST":
                if key in self.revisions:
                    if self.revisions[key]["commit"] != body:
                        return httpx.Response(409)
                    return httpx.Response(200, json={"state": "searchable", "snapshot_id": self.snapshot})
                if revision <= self.active.get((org, dataset), 0):
                    return httpx.Response(409)
                batches = self.staged.get(key, {})
                if set(batches) != set(range(body["batch_count"])):
                    return httpx.Response(409)
                rows = [r for i in range(body["batch_count"]) for r in batches[i]["records"]]
                digest = _native.Hasher()
                for row in rows:
                    digest.update(encode(row))
                if (
                    len({r["item_id"] for r in rows}) != len(rows)
                    or len(rows) != body["item_count"]
                    or digest.hexdigest() != body["manifest_digest"]
                ):
                    return httpx.Response(409)
                if any(b["usage_assertion"] != body["usage_assertion"] for b in batches.values()):
                    return httpx.Response(409)
                parents = {r["item_id"]: r["parent_id"] for r in rows}
                for row in rows:
                    current, seen = row["item_id"], set()
                    while current is not None:
                        if current in seen or current not in parents or len(seen) > 64:
                            return httpx.Response(409)
                        seen.add(current)
                        current = parents[current]
                self.revisions[key] = {
                    "rows": rows,
                    "role": role,
                    "usage": body["usage_assertion"],
                    "withdrawn": False,
                    "commit": body,
                }
                self.active[(org, dataset)] = revision
                self._reindex()
                return httpx.Response(200, json={"state": "searchable", "snapshot_id": self.snapshot})
            if len(parts) == 5 and key in self.revisions:
                if request.method == "DELETE":
                    self.revisions[key]["withdrawn"] = True
                    self._reindex()
                    return httpx.Response(204)
                if request.method == "GET":
                    return httpx.Response(
                        200,
                        json={
                            "state": "withdrawn" if self.revisions[key]["withdrawn"] else "searchable",
                            "snapshot_id": self.snapshot,
                        },
                    )
            return httpx.Response(404)
        except Exception:
            return httpx.Response(400, json={"error": "invalid_request"})
