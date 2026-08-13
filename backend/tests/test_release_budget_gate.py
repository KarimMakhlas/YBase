"""Budget policy regression coverage for the release evaluator."""

import importlib.util
from argparse import Namespace
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_release_budget.py"
_SPEC = importlib.util.spec_from_file_location("check_release_budget", _SCRIPT)
_GATE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_GATE)


def _args():
    return Namespace(min_ann_recall=0.95, max_quality_loss=0.02, max_relative_increase=0.20)


def _metrics(**over):
    base = {
        "ann_recall_at_10": 0.97,
        "citation_entailment_precision": 0.96,
        "retrieval_p95_ms": 80.0,
        "query_provider_cost_usd": 0.004,
        "formation_queue_p95_ms": 1000.0,
    }
    base.update(over)
    return base


def test_release_budget_gate_accepts_within_fixed_budgets():
    assert _GATE.check(_metrics(), _metrics(retrieval_p95_ms=96.0), _args()) == []


def test_release_budget_gate_rejects_quality_loss_and_cost_growth():
    failures = _GATE.check(
        _metrics(),
        _metrics(ann_recall_at_10=0.94, citation_entailment_precision=0.93,
                 query_provider_cost_usd=0.005),
        _args(),
    )

    assert any("ANN recall@10" in failure for failure in failures)
    assert any("citation-entailment precision" in failure for failure in failures)
    assert any("per-query provider cost" in failure for failure in failures)
