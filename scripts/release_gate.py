"""Fail-closed stable-release gate. Supply reviewed evidence, never guessed metrics."""

import argparse
import json
import math
from pathlib import Path


def evaluate(evidence):
    checks = {}
    checks["exact_vectors"] = evidence.get("exact_vectors_passed") is True
    checks["cross_platform"] = set(evidence.get("verified_platforms", [])) >= {
        "linux-x86_64",
        "linux-aarch64",
        "macos-x86_64",
        "macos-aarch64",
    }
    groups = evidence.get("quality_groups", [])
    checks["modality_coverage"] = {g.get("modality") for g in groups} >= {"text", "image", "audio", "video"}
    checks["quality"] = bool(groups) and all(
        g.get("held_out") is True
        and 0.98 <= g.get("near_duplicate_recall", 0) <= 1
        and 0.95 <= g.get("excerpt_recall", 0) <= 1
        and 0.999 <= g.get("precision", 0) <= 1
        and bool(g.get("corpus_reference"))
        for g in groups
    )
    negative = evidence.get("negative_queries", {})
    n = negative.get("count", 0)
    checks["negative_queries"] = (
        n >= 3_000_000
        and negative.get("false_overlap_reports") == 0
        and negative.get("representative_held_out") is True
        and bool(negative.get("index_reference"))
    )
    pilot = evidence.get("pilot", {})
    checks["terabyte_pilot"] = (
        pilot.get("bytes", 0) >= 1e12
        and pilot.get("representative_multimodal") is True
        and pilot.get("complete") is True
        and bool(pilot.get("report_reference"))
    )
    checks["scaling"] = all(
        evidence.get(k, {}).get("maximum", 0) >= 8 and evidence.get(k, {}).get("efficiency", 0) >= 0.8
        for k in ("worker_scaling", "node_scaling")
    )
    checks["security_review"] = evidence.get("privacy_and_dependency_review_passed") is True
    return {
        "stable_release_allowed": all(checks.values()),
        "checks": checks,
        "negative_query_95pct_upper_bound": -math.expm1(math.log(0.05) / n)
        if n > 0 and negative.get("false_overlap_reports") == 0
        else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.evidence.read_text()))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["stable_release_allowed"] else 1)
