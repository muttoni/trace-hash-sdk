"""Reference pairwise matcher. Experimental fuzzy evidence is never reportable."""

from __future__ import annotations

from collections import defaultdict

from .model import BOUNDARIES, POLICY


def _result(kind, *, proposed=None, score=None, metric=None, profiles=None, segments=None, limitations=None):
    return {
        "class": kind,
        "proposed_class": proposed,
        "reportable": kind == "byte_exact",
        "score": score,
        "score_kind": metric,
        "profiles": profiles or [],
        "query_segments": segments or [],
        "policy_version": POLICY,
        "limitations": limitations or [],
        "boundaries": BOUNDARIES,
    }


def _distance(a, b):
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _aligned(query, target, *, tolerance=0, budget=100000):
    index = defaultdict(list)
    limited = False
    for a in target:
        if len(index[a["hash"]]) < 64:
            index[a["hash"]].append(a["position"])
        else:
            limited = True
    votes = defaultdict(list)
    work = 0
    for a in query:
        for position in index.get(a["hash"], []):
            work += 1
            if work > budget:
                return None, True
            delta = position - a["position"]
            for d in range(delta - tolerance, delta + tolerance + 1):
                votes[d].append((a["position"], a["hash"]))
    if not votes:
        return None, limited
    best = max(votes.values(), key=lambda values: len(set(values)))
    unique = set(best)
    return {
        "count": len(unique),
        "distinct": len({h for _, h in unique}),
        "start": min(p for p, _ in unique),
        "end": max(p for p, _ in unique),
    }, limited


def _text(a, b):
    if a["low_information"] or b["low_information"]:
        return None, "low_information"
    if a["canonical_digest"] == b["canonical_digest"]:
        return ("canonical_exact", 1.0, "canonical_digest", []), None
    score = sum(x == y for x, y in zip(a["minhash"], b["minhash"])) / 128
    if score >= 0.90:
        return ("near_duplicate", score, "minhash_agreement", []), None
    alignment, limited = _aligned(a["anchors"], b["anchors"])
    if alignment and alignment["distinct"] >= 5 and alignment["end"] - alignment["start"] >= 50:
        return (
            "partial_overlap",
            float(alignment["count"]),
            "aligned_anchor_count",
            [{"unit": "token", "start": alignment["start"], "end": alignment["end"] + 5}],
        ), None
    return None, "candidate_limit" if limited else "feature_limit" if a["truncated"] or b[
        "truncated"
    ] else None


def _audio(a, b):
    if a["low_information"] or b["low_information"]:
        return None, "low_information"
    alignment, limited = _aligned(a["landmarks"], b["landmarks"], tolerance=1)
    if alignment:
        seconds = (alignment["end"] - alignment["start"]) * a["hop"] / a["sample_rate"]
        if alignment["distinct"] >= 25 and alignment["count"] >= 50 and seconds >= 10:
            return (
                "partial_overlap",
                float(alignment["count"]),
                "aligned_landmark_count",
                [
                    {
                        "unit": "seconds",
                        "start": alignment["start"] * a["hop"] / a["sample_rate"],
                        "end": (alignment["end"] * a["hop"] + 2048) / a["sample_rate"],
                    }
                ],
            ), None
    return None, "candidate_limit" if limited else "feature_limit" if a["truncated"] or b[
        "truncated"
    ] else None


def _video(a, b):
    if a["low_information"] or b["low_information"]:
        return None, "low_information"
    # 32 disjoint 8-bit bands: <=30 bit differences guarantees at least one equal band.
    buckets = defaultdict(set)
    for index, frame in enumerate(b["frames"]):
        if frame["quality"] < 50:
            continue
        for band in range(32):
            buckets[(band, frame["hash"][band * 2 : band * 2 + 2])].add(index)
    votes = defaultdict(set)
    work = 0
    for i, frame in enumerate(a["frames"]):
        if frame["quality"] < 50:
            continue
        candidates = set()
        for band in range(32):
            candidates.update(buckets.get((band, frame["hash"][band * 2 : band * 2 + 2]), ()))
        for j in candidates:
            work += 1
            if work > 100000:
                return None, "candidate_limit"
            target = b["frames"][j]
            if _distance(frame["hash"], target["hash"]) <= 30:
                offset = round((target["time_ms"] - frame["time_ms"]) / 1000)
                votes[offset].add(i)
    best = max(votes.values(), key=len, default=set())
    if len(best) >= 10:
        times = [a["frames"][i]["time_ms"] for i in best]
        if max(times) - min(times) >= 9000:
            return (
                "partial_overlap",
                float(len(best)),
                "aligned_frame_count",
                [{"unit": "seconds", "start": min(times) / 1000, "end": max(times) / 1000 + 1}],
            ), None
    return None, "sampled_video"


def compare(query: dict, target: dict) -> dict:
    from .manifest import validate_record

    validate_record(query)
    validate_record(target)
    if query["status"] == "failed" or target["status"] == "failed":
        return _result("indeterminate", limitations=["failed_input"])
    if (
        query["exact_digest"]
        and query["exact_digest"] == target["exact_digest"]
        and query["exact_basis"] == target["exact_basis"]
    ):
        if query["exact_basis"] == "raw_bytes":
            return _result("byte_exact", score=1.0, metric="blake3-256")
        return _result("uncertain", proposed="canonical_exact", limitations=["experimental_profile"])
    index = {f["profile"]: f for f in target["features"]}
    compatible, limitations, candidates = 0, set(), []
    for f in query["features"]:
        other = index.get(f["profile"])
        if not other or other["kind"] != f["kind"]:
            continue
        compatible += 1
        a, b, kind = f["payload"], other["payload"], f["kind"]
        evidence, limitation = None, None
        if kind == "text":
            evidence, limitation = _text(a, b)
        elif kind == "audio":
            evidence, limitation = _audio(a, b)
        elif kind == "video":
            evidence, limitation = _video(a, b)
        elif kind == "image":
            if a["low_information"] or b["low_information"]:
                limitation = "low_information"
            else:
                distance = _distance(a["hash"], b["hash"])
                if distance <= 30:
                    evidence = ("near_duplicate", float(distance), "pdq_hamming_distance", [])
        elif kind == "image_regions":
            matches = [
                (x, y)
                for x in a["regions"]
                for y in b["regions"]
                if x["quality"] >= 50 and y["quality"] >= 50 and _distance(x["hash"], y["hash"]) <= 20
            ]
            if len({x["hash"] for x, _ in matches}) >= 2:
                evidence = ("partial_overlap", float(len(matches)), "region_candidate_count", [])
            limitation = "regional_alignment_unvalidated"
        if limitation:
            limitations.add(limitation)
        if evidence:
            candidates.append((f["profile"], evidence))
    if candidates:
        rank = {"canonical_exact": 0, "near_duplicate": 1, "partial_overlap": 2}
        profile, (kind, score, metric, segments) = min(candidates, key=lambda x: rank[x[1][0]])
        return _result(
            "uncertain",
            proposed=kind,
            score=score,
            metric=metric,
            profiles=[profile],
            segments=segments,
            limitations=sorted(limitations | {"experimental_profile"}),
        )
    if not compatible:
        return _result("not_comparable", limitations=["incompatible_or_missing_profile"])
    if query["coverage"] != "complete" or target["coverage"] != "complete":
        limitations.add("incomplete_coverage")
    if limitations:
        return _result("indeterminate", limitations=sorted(limitations))
    return _result("no_match", limitations=["experimental_profile", "pairwise_only"])
