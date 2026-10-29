# Architecture and operating limits

`trace-core` implements BLAKE3, text normalization/MinHash/Winnowing and incremental spectral landmarks. PyO3 exposes these primitives without independent Python reimplementations; expensive text/audio work releases the GIL. Python coordinates input adapters, decoder processes, interchange, checkpoints and HTTP. Image PDQ uses the upstream native implementation.

## Data path

Local/S3 input → bounded worker process → exact hash → decode/normalize → content profiles → validated envelopes → local manifest → explicit Trace client.

Local files are read once for the full exact digest and read again by decoders. This is deliberate in the reference implementation: adapters need different seek/stream behavior. Stream/S3 input additionally spools to private temporary storage and is reread locally. No claim of universal single-pass decoding is made. Sharing these reads, columnar upload payloads and avoiding JSON serialization costs are optimization work to assess with the pilot.

Persistent worker pools bound in-flight sources to twice the worker count and do not eagerly materialize a file inventory. Each source result is bounded, although logical rows within one source are currently accumulated before publication so a changed file cannot leave valid-looking child records behind. For huge containers, raise limits consciously or split source shards; reaching the default record cap produces a partial result.

## Default bounds

| Resource | Default |
|---|---:|
| Input bytes per source | 1 TiB |
| Decoded text/record bytes | 8 MiB |
| Stream/S3 spool | 1 GiB |
| Total archive/document expansion | 1 GiB |
| Logical records per container budget | 10,000 |
| Nested containers | 4 |
| Pixels per image/frame/page | 40 million |
| PDF pages | 1,000 |
| Audio/video duration | 1 hour |
| Anchors/landmarks/frames | 500,000 |
| Container manifest budget | 128 MiB |
| Worker peak resident memory | 4 GiB |
| Worker wall time per source | 600 seconds |

Limits are configurable within the public schema's fixed upper bounds. An OS SIGALRM with the default termination action enforces wall time even if a native decoder holds Python's GIL. Linux also applies an address-space resource limit; a resident-memory watchdog runs on both platforms. macOS does not provide equivalent hard address-space enforcement here, so a container/host memory limit remains necessary for strict containment. The scheduler fails explicitly on a terminated pool, retaining already committed work for a resume. It does not claim complete processing of unattempted sources after termination.

This is **process isolation and resource control, not an OS security sandbox**. Decoder vulnerabilities that achieve arbitrary code execution are outside its containment. Run the reference container without network access and with read-only input mounts for hostile local corpora. S3 access requires network credentials and should use separately scoped read-only roles and egress restrictions. Never point this tool at untrusted active documents in a high-privilege host process.

## Recovery and source identity

SQLite WAL checkpoints atomically commit each source's opaque ID and all its rows. Checkpoint files/locks and output manifests use mode 0600. Checkpoints deliberately contain local source locations, so they must not be uploaded. A file lock prevents concurrent writers from sharing a checkpoint. Output manifests replace a temporary file atomically after a completed run.

Local cache verification recomputes BLAKE3 over all bytes; equal sizes/mtimes are insufficient. Device, inode, size, mtime and ctime are checked before and after source processing. This detects ordinary concurrent changes, not a malicious source capable of defeating OS metadata and snapshot semantics. Prefer immutable datasets or filesystem snapshots.

S3 uses the default AWS credential chain and HTTPS. GET pins a non-null VersionId or uses If-Match plus a post-read metadata check. ETags are only concurrency validators, never substituted for a content digest. S3 scan inputs are individual object URIs; callers can stream object inventories into the SDK. Native bucket-prefix discovery and a distributed job coordinator are not included.

Shard assignment is BLAKE3(source URI) modulo shard count. All machines must use identical source URIs and a shared explicit dataset UUID, with separate checkpoint/output paths per shard. Content fingerprints do not depend on that placement. Cross-node coordination, straggler scheduling, merging and snapshot-consistent dataset totals belong in the lab's orchestration layer.

## Measurements

The benchmark records source bytes, wall time, worker and parent CPU time, peak worker RSS, generated manifest bytes and decoded audio duration. Total CPU includes both worker processing and parent scheduling/validation. Source bytes represent corpus size, not physical I/O volume: re-reads and cache verification cause additional I/O. S3 request/retry counters use SDK response metadata and become unavailable on unmeasured transport/worker failure. Decoder read amplification is not measured. Cache-hit runs are not valid cold-throughput measurements. Peak worker RSS is not total fleet memory; the media-hours metric currently measures decoded audio streams rather than sampled silent-video duration.

Cost estimates require caller-supplied total hourly compute cost and omit unspecified storage/egress/request charges. Larger-scale projections assume unchanged workload and available storage bandwidth; they are not measurements of petabyte operation.
