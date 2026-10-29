# Proposed Trace protocol v1

This is a proposed integration contract, not a claim about an existing deployed endpoint. `TraceClient` implements it. `ReferenceRegistry` is an in-memory `httpx.MockTransport` fixture for protocol and report tests, **not a production service**. It is intentionally not exposed as a network server and does not implement persistent encryption, distributed indexing or operator controls.

## Identity and authorization

HTTPS is required, TLS verification stays enabled, redirects are not followed, and environment proxy configuration is not implicitly inherited. Tokens authenticate to an organization and its lab/vendor role. No organization identifier in an upload body is authoritative. Server authorization must scope every dataset operation to the authenticated organization; vendor queries must not mutate buyer usage.

Usage assertions: `offered_for_sale`, `under_evaluation`, `planned_for_training`, `declared_used_for_training`. These are assertions, never cryptographic proof of training, ownership or licensing.

## Upload and revision lifecycle

`PUT /v1/datasets/{uuid}/revisions/{positive_integer}/batches/{zero_based_sequence}`

Body: `{ "records": [manifest envelopes], "usage_assertion": "..." }`.

The client targets batches of 256 records or 8 MiB, whichever comes first; a single larger record may be sent alone up to 128 MiB. Servers must bound parsing and storage before acknowledgement. An envelope is validated against the public schema before sending. Unknown fields are rejected instead of silently uploaded.

Every mutation includes `Idempotency-Key = BLAKE3(method + NUL + relative_path + NUL + canonical_JSON_body)`. Keys must be scoped to the authenticated organization. Repeating an identical batch is safe; different contents at the same revision/sequence are a conflict. Re-running the client replays batches; server idempotency makes this resumable without double-registration.

`POST /v1/datasets/{uuid}/revisions/{revision}/commit`

Body: `batch_count`, `item_count`, `manifest_digest`, `usage_assertion`. The manifest digest covers the concatenation of canonical, newline-terminated JSONL records in submission order. Commit requires all sequences present, matching digest/counts, unique item IDs, valid parent relationships, consistent usage and organization authorization. The dataset becomes visible atomically; partially staged revisions must never count.

Return `state` separately as `durable`, `indexing` or `searchable`. A durable acknowledgement is not evidence that overlap checks include the data. `GET /v1/datasets/{uuid}/revisions/{revision}` returns indexing state; `DELETE` withdraws the revision. Revisions advance monotonically and replace the previous active revision without reviving an older revision after withdrawal.

The fixture transitions directly to searchable and exercises counting semantics. Production must add durable queues, index watermarks, tombstones, access audit logs and authenticated rate limits. Client retry policy handles transport errors, 429, 502, 503 and 504 with bounded backoff; 4xx validation/authentication errors are not retried. Error bodies are not echoed into logs.

## Read-only checks

`POST /v1/checks` with `{ "records": [...] }`. Maximum 256 items / 128 MiB per request, with the CLI targeting 8 MiB batches. The request does not register ownership or training use.

Response:

```json
{
  "snapshot_id": "opaque UUID",
  "coverage_watermark": "opaque UUID",
  "complete": true,
  "results": [{
    "item_id": "query item UUID",
    "class": "byte_exact",
    "other_labs_by_usage": {"declared_used_for_training": 2},
    "profiles": [],
    "query_segments": [],
    "policy_version": "trace.conservative.v0",
    "limitations": []
  }],
  "summary": {
    "distinct_other_labs": 2,
    "other_labs_by_usage": {"declared_used_for_training": 2}
  }
}
```

`complete` describes the index snapshot's availability, not globally complete data or validated fuzzy coverage. Every result also describes its own comparison limitations. The v0 fixture returns indeterminate rather than an unqualified negative for uncalibrated fuzzy searches. A future production negative must name the exact checked profiles and coverage. Incomplete index coverage can never produce an unqualified `no_match` in the client.

Count distinct **other organizations**, not uploads, records, files, memberships or fuzzy graph components. Exclude the querying organization and all vendors from lab counts. Usage categories remain separate. Summary counts are unions across items; summing per-item counts is incorrect. The CLI emits independent batch reports and intentionally does not claim their summed counts are dataset-wide distinct counts. Snapshot-consistent multi-batch dataset reports require a server-side check job, not client-side aggregation of anonymous counts.

The client projects a strict allowlist of result fields, verifies query item IDs, counts, known policy/profile identifiers and numerical query-side segments, and rejects positive counts for uncertain matches. It does not relay identities, fingerprints, filenames or arbitrary server error fields.

## Production server obligations outside this SDK

- Encrypt stored fingerprints, source-association records, backups and transport; apply retention and access policies.
- Enforce tenant isolation, enrollment, contributor roles, query quotas, abuse monitoring and administrator audit logs.
- Use exact indexes and profile-specific candidate retrieval, followed by direct fingerprint verification; no transitive inference.
- Expose candidate truncation, hot buckets, incompatible profiles and indexing lag as limitations, never negative evidence.
- Validate completeness and statistical quality of matching before accepting a new reportable profile.
- Apply withdrawal to every active index before acknowledging searchable deletion. Specify backup expiry and legal retention separately; the SDK cannot promise deletion from operator backups.
- Assess count-based membership inference and fingerprint inversion. Signing submissions proves attribution, not possession or training use.

No production backend URL, persistent database, cloud infrastructure or external publication is created by this repository.
