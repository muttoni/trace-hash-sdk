# Security boundaries

Do not include source data, confidential fingerprints, credentials or local
checkpoint databases in public bug reports. Report minimal synthetic reproductions.

The SDK does not transmit content to Trace. Fingerprints still disclose similarity,
membership and structural information. Aggregate counts can support inference.
The public algorithms are not encryption and do not prove ownership or training.

Native decoders run in resource-limited processes, not a complete OS sandbox.
Use a separate low-privilege environment for hostile corpora. With local input,
the reference container can run with `--network=none`, `--read-only`, bounded
`--memory`/`--cpus`, a bounded `/tmp` tmpfs and read-only input mounts. S3 jobs
require separately restricted egress and read-only object-store credentials.

The in-memory reference registry is a test fixture, with no persistent storage,
encryption-at-rest, public listener or production authorization infrastructure.
Do not deploy it as a service. Production operator requirements are documented
in docs/TRACE_API.md. A production security contact must be established before
the project's first public release; none is fabricated here.
