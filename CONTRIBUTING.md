# Contributing

Build the package in a virtual environment, run the Rust and Python test suites,
and include synthetic regression fixtures for changes in behavior. Never commit
source datasets, local checkpoints, credentials or private fingerprints.

Fingerprint output is a public interface. Changes to normalization, numeric
behavior, decoder compatibility, algorithms, seeds, segmentation or serialization
require a new profile ID and new immutable vectors. Matching-policy changes use
a separate policy version. Do not update existing vectors merely to make tests
pass: explain the intended compatibility break.

A proposal to promote an experimental profile must include a profile specification,
held-out quality evidence for every advertised transformation, candidate-retrieval
measurements, throughput and fingerprint/index density, privacy analysis and the
release evidence in docs/VALIDATION.md. Registry counts must never include
uncertain similarities or infer matches through transitive clusters.

The project currently has no public release or configured security contact.
Establish maintainer ownership, the reporting channel and artifact-signing identity
before publishing. The included workflows build and attest artifacts; they do not
publish to PyPI or configure a production Trace service.
