"""CLI contract for release metric budget comparisons."""

from pathlib import Path
import hashlib
import json
import subprocess
import sys


def test_release_budget_gate_help_describes_baseline_and_candidate_inputs():
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_release_budget.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "--baseline" in result.stdout
    assert "--candidate" in result.stdout


def test_release_budget_gate_requires_an_exact_expiring_approval_to_override_failure(tmp_path):
    """A recorded exception may cover the measured regression only when it is
    bound to these exact artifacts and has not expired."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_release_budget.py"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps({
        "ann_recall_at_10": 0.97,
        "citation_entailment_precision": 0.96,
        "retrieval_p95_ms": 80.0,
        "query_provider_cost_usd": 0.004,
        "formation_queue_p95_ms": 1000.0,
    }))
    candidate.write_text(json.dumps({
        "ann_recall_at_10": 0.90,
        "citation_entailment_precision": 0.93,
        "retrieval_p95_ms": 120.0,
        "query_provider_cost_usd": 0.006,
        "formation_queue_p95_ms": 1400.0,
    }))
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({
        "approved_by": "release-owner@example.com",
        "approved_at": "2026-08-10T12:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "reason": "Staging compute migration; rollback has been rehearsed.",
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }))

    result = subprocess.run(
        [sys.executable, str(script), "--baseline", str(baseline),
         "--candidate", str(candidate), "--approval", str(approval)],
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0
    assert "recorded release exception accepted" in result.stdout


def test_release_budget_gate_rejects_an_approval_for_a_different_candidate(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_release_budget.py"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps({
        "ann_recall_at_10": 0.97,
        "citation_entailment_precision": 0.96,
        "retrieval_p95_ms": 80.0,
        "query_provider_cost_usd": 0.004,
        "formation_queue_p95_ms": 1000.0,
    }))
    candidate.write_text(json.dumps({
        "ann_recall_at_10": 0.90,
        "citation_entailment_precision": 0.93,
        "retrieval_p95_ms": 120.0,
        "query_provider_cost_usd": 0.006,
        "formation_queue_p95_ms": 1400.0,
    }))
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({
        "approved_by": "release-owner@example.com",
        "approved_at": "2026-08-10T12:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "reason": "Staging compute migration; rollback has been rehearsed.",
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }))
    candidate.write_text(candidate.read_text().replace("120.0", "121.0"))

    result = subprocess.run(
        [sys.executable, str(script), "--baseline", str(baseline),
         "--candidate", str(candidate), "--approval", str(approval)],
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 1
    assert "candidate_sha256 does not match" in result.stderr


def test_model_or_prompt_release_requires_canary_and_rollback_metadata(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_release_budget.py"
    metrics = json.dumps({
        "ann_recall_at_10": 0.97,
        "citation_entailment_precision": 0.96,
        "retrieval_p95_ms": 80.0,
        "query_provider_cost_usd": 0.004,
        "formation_queue_p95_ms": 1000.0,
    })
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(metrics)
    candidate.write_text(metrics)

    result = subprocess.run(
        [sys.executable, str(script), "--baseline", str(baseline),
         "--candidate", str(candidate), "--change-kind", "prompt"],
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 2
    assert "--rollout-metadata is required" in result.stderr


def test_prompt_release_accepts_a_passing_canary_with_candidate_bound_rollback(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_release_budget.py"
    metrics = json.dumps({
        "ann_recall_at_10": 0.97,
        "citation_entailment_precision": 0.96,
        "retrieval_p95_ms": 80.0,
        "query_provider_cost_usd": 0.004,
        "formation_queue_p95_ms": 1000.0,
    })
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(metrics)
    candidate.write_text(metrics)
    rollout = tmp_path / "rollout.json"
    rollout.write_text(json.dumps({
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "canary": {"scope": "staging workspace canary", "result": "passed"},
        "rollback": {"strategy": "deploy previous prompt version", "target": "prompt:2026-08-01"},
    }))

    result = subprocess.run(
        [sys.executable, str(script), "--baseline", str(baseline),
         "--candidate", str(candidate), "--change-kind", "prompt",
         "--rollout-metadata", str(rollout)],
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0
    assert "canary passed" in result.stdout
