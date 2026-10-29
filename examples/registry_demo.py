"""Run a local protocol demonstration; no network requests or training content uploads."""
import io
import json

from trace_hash import TraceClient, fingerprint_stream
from trace_hash.reference import ReferenceRegistry


def main():
    registry = ReferenceRegistry({
        "lab-a-token": ("lab-a", "lab"),
        "lab-b-token": ("lab-b", "lab"),
        "buyer-token": ("buyer", "lab"),
    })
    content = " ".join(f"syntheticword{i}" for i in range(200)).encode()
    rows = fingerprint_stream(io.BytesIO(content), format_hint="example.txt")
    for token in ("lab-a-token", "lab-b-token"):
        with TraceClient("https://local-fixture.invalid", token, transport=registry.transport()) as client:
            client.submit(rows, dataset_id=rows[0]["dataset_id"], revision=1, usage="declared_used_for_training")
    with TraceClient("https://local-fixture.invalid", "buyer-token", transport=registry.transport()) as client:
        print(json.dumps(client.check(rows), indent=2))


if __name__ == "__main__":
    main()
