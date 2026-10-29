"""Explicit, fingerprint-only HTTP integration. No service URL is assumed."""

from __future__ import annotations

import json
import math
import time
import uuid
from urllib.parse import quote, urlsplit

import httpx

from . import _native
from .manifest import encode
from .model import BOUNDARIES, POLICY

USAGE = {"offered_for_sale", "under_evaluation", "planned_for_training", "declared_used_for_training"}


class TraceError(Exception):
    pass


class TraceClient:
    def __init__(self, base_url, token, *, transport=None, retries=3):
        u = urlsplit(base_url)
        if u.scheme != "https" or not u.netloc or u.username or u.password or u.query or u.fragment:
            raise ValueError("Trace requires an HTTPS base URL without credentials or query parameters")
        if not token or retries < 0 or retries > 10:
            raise ValueError("token required; retries must be between 0 and 10")
        self.retries = retries
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            transport=transport,
            headers={"Authorization": "Bearer " + token},
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method, path, data=None):
        body = (
            json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if data is not None
            else b""
        )
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers["Idempotency-Key"] = _native.digest_bytes(
                method.encode() + b"\0" + path.encode() + b"\0" + body
            )
        for attempt in range(self.retries + 1):
            try:
                response = self.http.request(method, path, content=body, headers=headers)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise TraceError(
                        "Trace transport failed; no credentials or source metadata logged"
                    ) from None
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < self.retries:
                retry = response.headers.get("Retry-After", "")
                time.sleep(min(float(retry), 30) if retry.isdigit() else min(2**attempt, 8))
                continue
            if response.status_code not in range(200, 300):
                raise TraceError(f"Trace request failed with HTTP {response.status_code}")
            if response.status_code == 204:
                return {"withdrawn": True}
            try:
                return response.json()
            except ValueError:
                raise TraceError("Invalid Trace JSON response") from None
        raise TraceError("retry budget exhausted")

    def submit(self, records, *, dataset_id, revision, usage, batch_size=256):
        if usage not in USAGE or revision < 1 or batch_size < 1:
            raise ValueError("invalid usage, revision or batch size")
        dataset = quote(dataset_id, safe="")
        uuid.UUID(dataset_id)
        digest, count, batch, size = _native.Hasher(), 0, [], 0
        sequence = 0
        for record in records:
            if record["dataset_id"] != dataset_id:
                raise ValueError("mixed dataset submission")
            wire = encode(record)
            if len(wire) > 128 << 20:
                raise ValueError("individual upload record exceeds 128 MiB")
            if batch and (len(batch) >= batch_size or size + len(wire) > 8 << 20):
                self._request(
                    "PUT",
                    f"v1/datasets/{dataset}/revisions/{revision}/batches/{sequence}",
                    {"records": batch, "usage_assertion": usage},
                )
                sequence += 1
                batch, size = [], 0
            digest.update(wire)
            count += 1
            batch.append(record)
            size += len(wire)
        if batch:
            self._request(
                "PUT",
                f"v1/datasets/{dataset}/revisions/{revision}/batches/{sequence}",
                {"records": batch, "usage_assertion": usage},
            )
            sequence += 1
        result = self._request(
            "POST",
            f"v1/datasets/{dataset}/revisions/{revision}/commit",
            {
                "batch_count": sequence,
                "item_count": count,
                "manifest_digest": digest.hexdigest(),
                "usage_assertion": usage,
            },
        )
        if result.get("state") not in ("durable", "indexing", "searchable"):
            raise TraceError("Invalid ingestion state")
        return result

    def status(self, dataset_id, revision):
        uuid.UUID(dataset_id)
        return self._request("GET", f"v1/datasets/{quote(dataset_id, safe='')}/revisions/{int(revision)}")

    def withdraw(self, dataset_id, revision):
        uuid.UUID(dataset_id)
        return self._request("DELETE", f"v1/datasets/{quote(dataset_id, safe='')}/revisions/{int(revision)}")

    def check(self, records):
        batch, size = [], 0
        for record in records:
            size += len(encode(record))
            if len(batch) >= 256 or size > 128 << 20:
                raise ValueError("check batches are limited to 256 items / 128 MiB")
            batch.append(record)
        records = batch
        response = self._request("POST", "v1/checks", {"records": records})
        # Construct a strict projection; never relay untrusted identities or fingerprints.
        if not all(k in response for k in ("snapshot_id", "coverage_watermark", "complete", "results")):
            raise TraceError("Trace check missing registry coverage")
        if type(response["complete"]) is not bool:
            raise TraceError("Invalid registry completeness")
        for key in ("snapshot_id", "coverage_watermark"):
            try:
                uuid.UUID(response[key])
            except (ValueError, TypeError, AttributeError):
                raise TraceError("Invalid registry snapshot identifier") from None
        allowed_items = {r["item_id"] for r in records}
        query_profiles = {r["item_id"]: {f["profile"] for f in r["features"]} for r in records}
        safe = []
        for result in response["results"]:
            if result.get("item_id") not in allowed_items:
                raise TraceError("unexpected item in Trace report")
            counts = result.get("other_labs_by_usage", {})
            if set(counts) - USAGE or any(type(n) is not int or n < 0 for n in counts.values()):
                raise TraceError("invalid aggregate counts")
            kind = result.get("class")
            if kind not in ("byte_exact", "uncertain", "no_match", "not_comparable", "indeterminate"):
                raise TraceError("unvalidated fuzzy report class")
            if kind != "byte_exact" and any(counts.values()):
                raise TraceError("uncertain results cannot contribute lab counts")
            if not response["complete"] and kind == "no_match":
                kind = "indeterminate"
            profiles = result.get("profiles", [])
            if not isinstance(profiles, list) or not set(profiles) <= query_profiles[result["item_id"]]:
                raise TraceError("Unexpected report profile")
            segments = []
            for segment in result.get("query_segments", []):
                if (
                    segment.get("unit") not in ("token", "seconds")
                    or any(
                        type(segment.get(k)) not in (int, float) or not math.isfinite(segment[k])
                        for k in ("start", "end")
                    )
                    or not 0 <= segment["start"] < segment["end"]
                ):
                    raise TraceError("Invalid query-side segment")
                segments.append({k: segment[k] for k in ("unit", "start", "end")})
            if len(segments) > 1024:
                raise TraceError("Too many query segments")
            policy = result.get("policy_version", POLICY)
            if policy != POLICY:
                raise TraceError("Unsupported matching policy")
            allowed_limits = {
                "candidate_limit",
                "experimental_profile",
                "incomplete_coverage",
                "sampled_video",
                "feature_limit",
                "low_information",
            }
            limits = result.get("limitations", [])
            if not isinstance(limits, list) or not set(limits) <= allowed_limits:
                raise TraceError("Unknown report limitation")
            safe.append(
                {
                    "item_id": result["item_id"],
                    "class": kind,
                    "other_labs_by_usage": counts,
                    "profiles": profiles,
                    "query_segments": segments,
                    "policy_version": policy,
                    "limitations": limits,
                }
            )
        if {r["item_id"] for r in safe} != allowed_items or len(safe) != len(allowed_items):
            raise TraceError("incomplete or duplicate check results")
        summary = response.get("summary")
        safe_summary = None
        if summary is not None:
            count, counts = summary.get("distinct_other_labs"), summary.get("other_labs_by_usage", {})
            if (
                type(count) is not int
                or count < 0
                or set(counts) - USAGE
                or any(type(n) is not int or n < 0 or n > count for n in counts.values())
            ):
                raise TraceError("Invalid distinct-lab summary")
            if not any(r["class"] == "byte_exact" for r in safe) and count:
                raise TraceError("Uncertain matches cannot contribute summary counts")
            safe_summary = {"distinct_other_labs": count, "other_labs_by_usage": counts}
        return {
            "snapshot_id": response["snapshot_id"],
            "coverage_watermark": response["coverage_watermark"],
            "complete": response["complete"],
            "results": safe,
            "summary": safe_summary,
            "boundaries": BOUNDARIES,
        }
