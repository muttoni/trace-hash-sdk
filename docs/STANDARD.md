# Trace fingerprint standard, draft 0

Normative language describes this implementation. **All fuzzy profiles remain experimental.** A stable v1 profile requires the release gates in VALIDATION.md. The implementation version is independent of the profile and matching-policy versions.

## Exact and canonical identity

`exact_algorithm=blake3-256` hashes every original byte, including metadata and container packaging. Bytes are read in bounded 1 MiB blocks. Input-size limits or source changes produce a failed record without a digest. Fuzzy decoder failure can coexist with a valid full-byte digest and partial coverage.

`exact_basis=raw_bytes` supports `byte_exact`. Logical mapped records instead use `canonical_record`: UTF-8 JSON with lexicographically sorted keys, compact separators, no ASCII escaping, no NaN/Infinity, and an object containing the explicit `mapping` and selected `values`. Their equality is **canonical**, not byte-exact original-file equality. Canonical record matches remain uncertain in v0.

Extracted document text and rendered pages use `derived_content`; equal generated payload bytes must not be reported as equality of original source files. Their equality also remains uncertain in v0.

IDs are opaque UUIDs, independent of fingerprint content. Child IDs are UUIDv5 using parent UUID and decimal child index. IDs may change when repacking; fingerprint payloads must not. Metadata affecting only placement never enters the content hash. Upload envelopes never carry paths, filenames, source URLs, field names, content, thumbnails, transcripts, or credentials. Structural metadata such as lengths and positions is intentionally visible to Trace.

## Text profile

IDs: `trace.text.nfc-minhash128-winnow32.v0` and `trace.code.nfc-minhash128-winnow32.v0`.

1. Decode strict UTF-8, consuming an optional UTF-8 BOM. Convert CRLF/CR to LF and apply Unicode NFC with `unicode-normalization=0.1.24`.
2. Prose collapses Unicode whitespace to single ASCII spaces and trims boundaries. Code retains whitespace. Case, identifiers, punctuation, and numeric values are not canonicalized away.
3. Canonical digest is BLAKE3-256 over normalized UTF-8. HTML extracts visible text with the supplied parser, excluding script/style/noscript; no external resources are fetched.
4. Prose shingles use Unicode words from `unicode-segmentation=1.12.0`; code shingles use individual Unicode scalar values. Shingle width is five units. Code and prose cannot share profiles.
5. Hash each shingle with BLAKE3 derive-key context `trace.text.shingle.v0`. Feed each UTF-8 unit preceded by its unsigned 64-bit little-endian byte length. Interpret the first eight digest bytes as little-endian `u64`.
6. Compute 128 minima of `splitmix64(shingle_hash XOR splitmix64(seed))`, seeds 0 through 127. Arithmetic wraps modulo 2^64; constants and reference implementation are in `crates/trace-core/src/lib.rs`. Encode values as 16 lowercase hex digits, numeric order preserved by fixed width.
7. Winnowing uses the **rightmost minimum** in each complete window of 32 consecutive shingles. Emit a `(hash, position)` anchor once per selected position, in source order. Positions are zero-based token/scalar offsets. Full-window guarantees begin at 36 units; this is not the calibrated minimum reportable excerpt length.
8. Fewer than 36 units or 16 distinct shingles is low-information. Anchor caps set `truncated=true`; the complete canonical digest and MinHash still cover the entire admitted text.

Mapped fields append `+mapping-` and the first 24 hex characters of BLAKE3 over sorted compact mapping JSON, NUL, and the field's UTF-8 JSON pointer. This prevents incompatible field selection from silently comparing. Only the mapping digest is uploaded. JSONL/CSV/Arrow/Parquet using the same explicit mapping produce equivalent record fingerprints.

## Images

`trace.image.pdq256-white-exif.v0`: use upstream `pdqhash=0.2.8`, not a substitute algorithm. Apply EXIF orientation, composite RGBA on opaque white, use RGB channel order, retain encoded RGB without implicit ICC conversion. PDQ's 256 bits are packed big-endian within bytes and lowercase hex encoded. Preserve the upstream quality score. Quality below 50 is low-information.

`trace.image.pdq-regions10-white-exif.v0` adds ten regions: half-size and three-quarter-size windows at four corners and the center, with integer-truncated pixel bounds. These are **candidate regional fingerprints**, not a verified geometric crop matcher. General crop robustness is not established, and regional evidence stays uncertain. Animated/multi-page images report a coverage limitation rather than silently using only their first frame.

## Audio

`trace.audio.landmarks-11025-2048-512.v0`: PyAV/FFmpeg decodes one audio stream to mono float32 at 11,025 Hz. The Rust engine retains state across caller blocks, computes a 2,048-point Hann-windowed FFT with hop 512, and finds spectral maxima in bins 20 through 999. Retain the three strongest local maxima above power 0.01, tie-breaking by lower bin. Pair the two strongest peaks at offsets 3, 7 and 15 frames. Landmark encoding is `(anchor_bin/4)<<24 | (target_bin/4)<<8 | frame_delta`, with integer division and 16-digit lowercase hex. Positions are anchor-frame offsets. Values are indexed keys, not cryptographic secrets.

At least 100 landmarks are required to avoid the low-information flag. Reference matching requires at least 25 distinct aligned landmark keys, 50 aligned observations and ten seconds of span, allowing one-frame offset tolerance. These are experimental operating points, not achieved precision/recall claims. Pitch/time stretching, cross-language speech and semantic equivalence are not supported claims.

## Video

`trace.video.pdq-1fps.v0`: decode the first video stream; fingerprint the first decoded frame at or after each next whole-second boundary with PDQ. Preserve frame timestamps in milliseconds and separately fingerprint the first audio track. The full container still receives an exact digest. Other streams, temporal gaps, short clips between sampled frames and edits outside the sample coverage are not guaranteed. Video records explicitly have partial/sampled coverage. Reference comparison uses PDQ Hamming candidates and temporal offset consensus, never mere shared-frame counts without alignment.

## Decoder compatibility

Media profiles append a 16-hex BLAKE3 suffix derived from a sorted JSON dictionary of relevant installed package versions (and FFmpeg library versions for PyAV). Different decoder builds are conservatively not comparable. Float/codec behavior has not passed the cross-platform gate. `requirements-lock.txt`, Cargo.lock and the reference container establish the tested baseline; wider interchange requires conformance evidence, not assumed tolerance.

## Documents and containers

PDF uses PDFium text extraction plus a rendered image per page (scale 1), covering scanned and vector material without OCR. DOCX extracts body paragraphs and media entries; headers, footers, ordering of floating objects and other layout details are not fully covered. Both explicitly report limited extraction coverage. No file or URL relationships are followed.

Archives are iterated, never extracted to caller-specified paths. ZIP encryption, corrupt members, expansion/depth/size limits and malformed records are explicit failures or partial coverage. TAR links/devices are skipped; no link targets are followed. WARC uses response/resource/conversion payloads without retrieving original URLs. Gzip decoding spools to a bounded private temporary file before parsing its child container. WebDataset TAR members are fingerprinted as independent content units; automatic multimodal sample grouping is not claimed.

## Matching and manifests

Policy `trace.conservative.v0` can report only `byte_exact`. All other positive evidence becomes `uncertain` with a `proposed_class`, native metric name, score and query-side segments. A score is not a probability. Incompatibility returns `not_comparable`; saturation, low-information or incomplete coverage returns `indeterminate`. No transitive matching is performed.

Schema: `schemas/manifest.schema.json`. JSONL uses one validated envelope per line. Arrow IPC uses zstd compression and one `large_binary` column named `record` containing the same canonical JSON envelope; schema metadata is `trace.schema=1.0`. This simple first interchange representation is lossless, but does not yet offer typed column projection of individual feature arrays.

Test vectors live under `tests/vectors/`. Any change affecting output must introduce a new profile ID; accepting incompatible records under an old ID is forbidden.
