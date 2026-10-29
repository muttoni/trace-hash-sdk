# Validation and release gates

Unit/integration tests establish implementation behavior, not production false-positive rates. Synthetic fixtures are intentionally not presented as a held-out training-data benchmark.

## Run locally

```sh
cargo test --locked
cargo clippy --all-targets -- -D warnings
pytest -q
ruff check python tests scripts
trace-hash benchmark /path/to/representative/data --workers 1 2 4 8 --output benchmark.json
```

The benchmark consumes real supplied input, does not download datasets, and does not silently generate a terabyte of data. It measures full hashing/decoding/output, keeps actual measurements separate from projections, and marks unrun release gates false. `--hourly-cost` is the **total fleet hourly compute cost for that run**, not per-worker cost. `--projection-bytes` assumes the same workers/hardware/data mix; network, storage and index bottlenecks are not predicted.

The local `benchmarks/smoke.json` report, when present, is a small synthetic engineering measurement. It is not the one-terabyte acceptance pilot and does not demonstrate eight-node scaling.

## Required before stable fuzzy v1

| Gate | Required evidence |
|---|---|
| Exact correctness | Published vectors pass 100% |
| Determinism | Equal payloads on Linux/macOS x86-64 and ARM64 under the same profile |
| Near-duplicate recall | At least 98% in **each** advertised transformation category |
| Excerpt recall | At least 95% above each published minimum size |
| Precision | At least 99.9%, with disclosed prevalence and uncertainty |
| Negative queries | Zero false overlap decisions in at least 3,000,000 representative held-out queries against a declared index |
| Bulk pilot | At least 1 TB of representative multimodal input processed end to end |
| Worker/node scaling | 1→8 workers and 1→8 nodes; target at least 80% parallel efficiency on balanced, sufficiently provisioned workloads |
| Failure recovery | Interruption, retry, corrupted inputs, source mutation, throttling and duplicate submissions |
| Privacy | Inspect actual outbound payloads, logs and reports; adversarial API response tests |
| Release governance | Dependency/codec review, profile RFC, immutable vectors, signed artifacts, pilot lab acceptance |

For zero failures in n independent representative negative trials, the one-sided 95% binomial upper bound is `1 - 0.05**(1/n)`, approximately `3/n`. At three million trials this is about 10^-6 **per tested query at the declared index size**, not per pair and not a guarantee at trillion-item scale. Correlated/generated negatives do not automatically supply that statistical evidence. Report candidate-retrieval recall separately from final classification.

The supplied `scripts/release_gate.py` fails closed when required evidence is missing. It does not invent accuracy thresholds from synthetic smoke tests or promote profiles automatically. Review benchmark methodology and provenance before approving evidence files.

## Benchmark corpus requirements

Include independently labeled unrelated pairs and duplicates/excerpts across languages, scripts, source code, templated documents, repeated boilerplate, visually similar distinct images, speech/music/noise, repeated intros and title cards. Separate speakers reading the same words must not count as the same recording. Include hard negatives and common low-information content, not only random noise.

Hold out threshold-selection data from evaluation. Publish transformations, retained image area, excerpt lengths, codecs, resolutions, compression settings, sampling limits, distribution/prevalence, failure cases, licensed fixture provenance and frozen profile/decoder versions. Images with crops and short clips need their own gates; global PDQ or music-style landmark prior art is not sufficient validation.

## Scale model

Freeze a dated disclosed training reference, multiply its stated training volume by ten, then model original bytes, logical units, text tokens, media hours, fingerprint density, replication, index expansion and candidate rates separately. Public token counts cannot determine source petabytes. For example [Meta disclosed more than 30 trillion tokens for Llama 4](https://ai.meta.com/blog/llama-4-multimodal-intelligence/); that is a dated reference, not a claim about the largest or latest private corpus.

One billion items × 1,000 fingerprint bytes = 1 TB before indexing/replication. Include decoding and serialization cost, object-store request costs, cache validation reads, hot buckets, duplicate-skew and partial extraction in the model. Record uncertainty rather than asserting a universal GB/s promise.

## Current evidence boundary

This implementation provides executable profiles, conformance fixtures, bounded scanning, a protocol fixture, a benchmark command and CI configuration. Until independently measured: multimodal quality targets, cross-platform media equivalence, geometric crop matching, the terabyte pilot, eight-node operation and production registry controls remain **unverified**. The regional image matcher is a candidate experiment; it has no verified crop transformation envelope.
