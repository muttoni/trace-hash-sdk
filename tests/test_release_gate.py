import importlib.util
from pathlib import Path


def test_release_gate_fails_without_real_evidence():
    path = Path(__file__).resolve().parents[1] / "scripts/release_gate.py"
    spec = importlib.util.spec_from_file_location("release_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.evaluate({"exact_vectors_passed": True})
    assert not result["stable_release_allowed"]
    assert not result["checks"]["terabyte_pilot"]
    assert not result["checks"]["quality"]


def test_negative_query_bound():
    path = Path(__file__).resolve().parents[1] / "scripts/release_gate.py"
    spec = importlib.util.spec_from_file_location("release_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.evaluate({"negative_queries": {"count": 3000000, "false_overlap_reports": 0}})
    assert 0.00000099 < report["negative_query_95pct_upper_bound"] < 0.00000101
    assert not report["checks"]["negative_queries"]  # no representative corpus or index reference
