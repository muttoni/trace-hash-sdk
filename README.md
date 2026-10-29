# Trace Hashing SDK

Local, multimodal training-data fingerprints: a Rust core, Python SDK and `trace-hash` CLI.

**Status: experimental reference implementation, not a calibrated exclusivity service.** Exact byte matches use BLAKE3-256. All fuzzy profiles are experimental and their matches are returned as **uncertain**, with a proposed match class. They cannot contribute to production lab counts. No terabyte pilot, three-million-negative accuracy study or multi-node scaling run has been completed by this repository.

## Install

Python 3.11+ and a Rust toolchain are required to build from source. Linux and macOS are the supported platforms.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install 'maturin>=1.8,<2'
maturin develop --release --extras all,dev
trace-hash --help
```

The CLI is installed with the Python package and uses its compiled Rust extension. It is not a statically linked single executable. Optional extras are `media`, `data`, and `s3`; exact/text hashing does not require them. A missing optional dependency becomes an explicit coverage status.

## Local use

```sh
trace-hash scan ./dataset --output fingerprints.jsonl --workers 4
trace-hash validate fingerprints.jsonl
trace-hash scan s3://my-bucket/dataset.jsonl --config examples/record-config.json --output objects.arrow
trace-hash compare first.jsonl second.jsonl
```

`scan` never contacts Trace. Local source paths are retained only in the **private local SQLite checkpoint**, created beside the output. Keep this checkpoint confidential. Repeating the command resumes decoding work; local cache verification rereads the original bytes rather than trusting modification times. S3 objects are downloaded again and use immutable version IDs or conditional reads for consistency, never ETags as content digests.

Exit codes: `0` completed; `2` command/validation failure; `3` scan completed with partial, unsupported or failed sources. Read the JSON summary and each record's coverage; exact hashing may succeed even where fuzzy processing is unsupported.

For JSONL, CSV, Parquet and Arrow, map training fields explicitly. The mapping participates in the fingerprint profile. Text values must be strings; media fields accept bytes (Arrow/Parquet) or explicitly mapped base64 strings (JSONL). URLs are never fetched.

```python
from trace_hash import fingerprint_file, fingerprint_record, compare

if __name__ == "__main__":  # required by the spawn-based decoder workers
    first = fingerprint_file("first.txt")[0]
    second = fingerprint_file("second.txt")[0]
    print(compare(first, second))
    rows = fingerprint_record({"text": "training example"}, fields={"/text": "text"})
```

`fingerprint_stream` accepts a binary stream. `iter_fingerprints` provides bounded streaming over a collection with a reused worker pool. File calls return a bounded list containing the file and its logical children.

## Explicit Trace integration

```sh
export TRACE_TOKEN='your-token'
trace-hash submit fingerprints.jsonl --url https://your-trace-service.example \
  --dataset-id DATASET_UUID --revision 1 --usage declared_used_for_training
trace-hash check fingerprints.jsonl --url https://your-trace-service.example
trace-hash status --url https://your-trace-service.example --dataset-id DATASET_UUID --revision 1
trace-hash withdraw --url https://your-trace-service.example --dataset-id DATASET_UUID --revision 1
```

The URL is intentionally not preconfigured. The repository defines a proposed integration contract and reference implementation; it does not claim compatibility with an existing deployed Trace API. Checking never registers the querying buyer as a data user. Credentials are supplied through the environment, not command-line token arguments.

## Boundaries

- No detected overlap applies only to the checked registry, profiles and snapshot; it does not establish global exclusivity.
- A submission or contributor assertion does not prove training use, ownership or licensing rights.
- Fingerprints can disclose membership and similarity information. Keeping content local is not a secrecy guarantee.
- OCR, semantic paraphrases, translation matching and proof of training are outside this implementation.

See [the standard](docs/STANDARD.md), [architecture and limits](docs/ARCHITECTURE.md), [Trace protocol](docs/TRACE_API.md), [validation and release gates](docs/VALIDATION.md), and [third-party notices](NOTICE).
