from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .client import TraceClient
from .manifest import read_manifest
from .matching import compare
from .model import BOUNDARIES, VERSION, Config, Limits
from .scan import scan


def _config(args):
    values = {}
    if getattr(args, "config", None):
        values = json.loads(Path(args.config).read_text())
    return Config(
        limits=Limits(**values.get("limits", {})),
        fields=values.get("fields", {}),
        s3_endpoint=values.get("s3_endpoint"),
    )


def _dump(value, path=None):
    data = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if path:
        Path(path).write_text(data)
    else:
        print(data, end="")


def _single(path, item_id):
    selected = (
        [r for r in read_manifest(path) if r["item_id"] == item_id]
        if item_id
        else [r for r in read_manifest(path) if r["parent_id"] is None]
    )
    if len(selected) != 1:
        raise ValueError("comparison requires one root item or an explicit item ID")
    return selected[0]


def parser():
    root = argparse.ArgumentParser(
        prog="trace-hash", description="Local fingerprints. Fuzzy profiles are experimental."
    )
    root.add_argument("--version", action="version", version=VERSION)
    subs = root.add_subparsers(dest="command", required=True)
    s = subs.add_parser("scan", help="hash locally; never contacts Trace")
    s.add_argument("inputs", nargs="+")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--checkpoint")
    s.add_argument("--dataset-id")
    s.add_argument("--workers", type=int, default=1)
    s.add_argument("--shard-index", type=int, default=0)
    s.add_argument("--shard-count", type=int, default=1)
    s.add_argument("--config")
    v = subs.add_parser("validate", help="validate manifest schema and graph integrity")
    v.add_argument("manifest")
    c = subs.add_parser("compare", help="pairwise reference comparison")
    c.add_argument("query")
    c.add_argument("target")
    c.add_argument("--query-item")
    c.add_argument("--target-item")
    for name in ("submit", "check", "status", "withdraw"):
        p = subs.add_parser(name)
        p.add_argument("--url", required=True)
        p.add_argument("--token-env", default="TRACE_TOKEN")
        if name in ("submit", "check"):
            p.add_argument("manifest")
        if name != "check":
            p.add_argument("--dataset-id", required=True)
            p.add_argument("--revision", type=int, required=True)
        if name == "submit":
            p.add_argument(
                "--usage",
                required=True,
                choices=[
                    "offered_for_sale",
                    "under_evaluation",
                    "planned_for_training",
                    "declared_used_for_training",
                ],
            )
    b = subs.add_parser("benchmark", help="measure a supplied workload; never claims unrun scale gates")
    b.add_argument("inputs", nargs="+")
    b.add_argument("--workers", type=int, nargs="+", default=[1])
    b.add_argument("--config")
    b.add_argument("--output", required=True)
    b.add_argument("--hourly-cost", type=float)
    b.add_argument("--projection-bytes", type=int)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "scan":
            summary = scan(
                args.inputs,
                args.output,
                checkpoint=args.checkpoint,
                config=_config(args),
                dataset_id=args.dataset_id,
                workers=args.workers,
                shard_index=args.shard_index,
                shard_count=args.shard_count,
            )
            _dump(summary)
            return 3 if any(summary["counts"].get(k) for k in ("failed", "partial", "unsupported")) else 0
        if args.command == "validate":
            # Disk-backed graph checks avoid holding a petabyte-corpus inventory in RAM.
            import sqlite3

            with tempfile.TemporaryDirectory() as temp:
                with sqlite3.connect(str(Path(temp) / "validation.sqlite")) as db:
                    db.execute("CREATE TABLE items(id TEXT PRIMARY KEY, parent TEXT, dataset TEXT)")
                    count = 0
                    dataset = None
                    for record in read_manifest(args.manifest):
                        if dataset is not None and dataset != record["dataset_id"]:
                            raise ValueError("mixed dataset manifest")
                        dataset = record["dataset_id"]
                        db.execute(
                            "INSERT INTO items VALUES(?,?,?)",
                            (record["item_id"], record["parent_id"], dataset),
                        )
                        count += 1
                    missing = db.execute(
                        "SELECT COUNT(*) FROM items a LEFT JOIN items b ON a.parent=b.id WHERE a.parent IS NOT NULL AND b.id IS NULL"
                    ).fetchone()[0]
                    if missing:
                        raise ValueError("missing parent records")
                    # A bounded recursive walk detects self/cyclic links in the supported depth envelope.
                    bad = db.execute(
                        "WITH RECURSIVE walk(id,parent,depth) AS (SELECT id,parent,0 FROM items UNION ALL SELECT w.id,i.parent,w.depth+1 FROM walk w JOIN items i ON w.parent=i.id WHERE w.depth<64) SELECT COUNT(*) FROM walk WHERE parent=id OR (depth=64 AND parent IS NOT NULL)"
                    ).fetchone()[0]
                    if bad:
                        raise ValueError("cyclic or excessively deep manifest graph")
            _dump({"valid": True, "items": count})
        elif args.command == "compare":
            _dump(compare(_single(args.query, args.query_item), _single(args.target, args.target_item)))
        elif args.command == "benchmark":
            if args.hourly_cost is not None and args.hourly_cost < 0:
                raise ValueError("hourly cost must be nonnegative")
            if args.projection_bytes is not None and args.projection_bytes <= 0:
                raise ValueError("projection bytes must be positive")
            measurements = []
            with tempfile.TemporaryDirectory() as temp:
                for workers in args.workers:
                    result = scan(
                        args.inputs,
                        Path(temp) / f"run-{workers}.jsonl",
                        config=_config(args),
                        workers=workers,
                    )
                    m = result["metrics"]
                    m["estimated_cost_per_tb"] = (
                        args.hourly_cost * m["elapsed_seconds"] / 3600 * 1e12 / m["input_bytes"]
                        if args.hourly_cost is not None and m["input_bytes"]
                        else None
                    )
                    result["workers"] = workers
                    measurements.append(result)
            baseline = next((r for r in measurements if r["workers"] == 1), None)
            for r in measurements:
                r["parallel_efficiency"] = (
                    baseline["metrics"]["elapsed_seconds"] / r["metrics"]["elapsed_seconds"] / r["workers"]
                    if baseline and baseline["metrics"]["input_bytes"] == r["metrics"]["input_bytes"]
                    else None
                )
            best = max(measurements, key=lambda r: r["metrics"]["input_gb_per_second"])
            projection = None
            if args.projection_bytes and best["metrics"]["input_gb_per_second"]:
                projection = {
                    "projected_input_bytes": args.projection_bytes,
                    "projected_seconds": args.projection_bytes
                    / (best["metrics"]["input_gb_per_second"] * 1e9),
                    "assumptions": "Same hardware, worker count, data mix, storage throughput, and fingerprint density; no demonstrated fleet-scale result.",
                }
            _dump(
                {
                    "measurements": measurements,
                    "projection": projection,
                    "release_gates": {
                        "one_tb_measured": all(r["metrics"]["input_bytes"] >= 1e12 for r in measurements),
                        "representative_multimodal_pilot": False,
                        "eight_node_scaling": False,
                        "accuracy_calibration": False,
                    },
                    "boundaries": BOUNDARIES,
                },
                args.output,
            )
        else:
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError("authentication token environment variable is not set")
            with TraceClient(args.url, token) as client:
                if args.command == "submit":
                    result = client.submit(
                        read_manifest(args.manifest),
                        dataset_id=args.dataset_id,
                        revision=args.revision,
                        usage=args.usage,
                    )
                elif args.command == "status":
                    result = client.status(args.dataset_id, args.revision)
                elif args.command == "withdraw":
                    result = client.withdraw(args.dataset_id, args.revision)
                else:
                    # Stream independent query batches. Dataset-level lab unions must be calculated server-side.
                    batch, size = [], 0
                    from .manifest import encode

                    for record in read_manifest(args.manifest):
                        n = len(encode(record))
                        if batch and (len(batch) == 256 or size + n > 8 << 20):
                            print(json.dumps(client.check(batch)))
                            batch, size = [], 0
                        batch.append(record)
                        size += n
                    if batch:
                        print(json.dumps(client.check(batch)))
                    return 0
                _dump(result)
        return 0
    except Exception as exc:
        # Do not echo arbitrary decoder/server errors or credentials.
        print(
            f"trace-hash: operation failed ({type(exc).__name__}); local checkpoints are retained",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
