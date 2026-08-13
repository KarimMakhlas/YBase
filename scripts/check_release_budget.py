#!/usr/bin/env python3
"""Fail a release when measured memory-system budgets regress.

Each JSON input is a compact artifact from a fixed staging evaluation suite:

{
  "ann_recall_at_10": 0.97,
  "citation_entailment_precision": 0.96,
  "retrieval_p95_ms": 84.2,
  "query_provider_cost_usd": 0.0041,
  "formation_queue_p95_ms": 1200
}
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


REQUIRED = (
    "ann_recall_at_10",
    "citation_entailment_precision",
    "retrieval_p95_ms",
    "query_provider_cost_usd",
    "formation_queue_p95_ms",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare fixed release quality and budget metrics.")
    parser.add_argument("--baseline", required=True, type=Path, help="accepted baseline metrics JSON")
    parser.add_argument("--candidate", required=True, type=Path, help="candidate metrics JSON")
    parser.add_argument("--min-ann-recall", type=float, default=0.95)
    parser.add_argument("--max-quality-loss", type=float, default=0.02)
    parser.add_argument("--max-relative-increase", type=float, default=0.20)
    parser.add_argument(
        "--approval", type=Path,
        help="recorded, expiry-bounded exception JSON for these exact artifacts",
    )
    parser.add_argument(
        "--change-kind",
        choices=("other", "embedding", "index", "model", "prompt", "retrieval"),
        default="other",
        help="release class; model and prompt changes require canary metadata",
    )
    parser.add_argument(
        "--rollout-metadata", type=Path,
        help="canary and rollback record required for model/prompt releases",
    )
    args = parser.parse_args()
    if not 0 <= args.min_ann_recall <= 1:
        parser.error("--min-ann-recall must be between 0 and 1")
    if not 0 <= args.max_quality_loss <= 1:
        parser.error("--max-quality-loss must be between 0 and 1")
    if args.max_relative_increase < 0:
        parser.error("--max-relative-increase must be non-negative")
    return args


def _load(path: Path) -> Dict[str, float]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    missing = [key for key in REQUIRED if key not in data]
    if missing:
        raise ValueError(f"{path} is missing required metrics: {', '.join(missing)}")
    try:
        return {key: float(data[key]) for key in REQUIRED}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} contains a non-numeric metric") from exc


def _artifact_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"cannot hash {path}: {exc}") from exc


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"approval {field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"approval {field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"approval {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_approval(approval_path: Path, baseline_path: Path, candidate_path: Path) -> Dict[str, str]:
    """Validate a narrowly-scoped, independently reviewable release exception.

    An exception is deliberately bound to artifact bytes and expires. It cannot
    become a reusable switch for later candidate results.
    """
    try:
        data = json.loads(approval_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read approval {approval_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("approval must be a JSON object")
    for field in ("approved_by", "reason", "baseline_sha256", "candidate_sha256"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"approval {field} is required")
    approved_at = _parse_timestamp(data.get("approved_at"), "approved_at")
    expires_at = _parse_timestamp(data.get("expires_at"), "expires_at")
    if expires_at <= approved_at:
        raise ValueError("approval expires_at must be after approved_at")
    if expires_at <= datetime.now(timezone.utc):
        raise ValueError("approval has expired")
    if data["baseline_sha256"] != _artifact_hash(baseline_path):
        raise ValueError("approval baseline_sha256 does not match --baseline")
    if data["candidate_sha256"] != _artifact_hash(candidate_path):
        raise ValueError("approval candidate_sha256 does not match --candidate")
    return {field: data[field].strip() for field in ("approved_by", "reason")}


def validate_rollout_metadata(rollout_path: Path, candidate_path: Path) -> Dict[str, str]:
    """Require model/prompt canaries to retain their concrete rollback path."""
    try:
        data = json.loads(rollout_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read rollout metadata {rollout_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("rollout metadata must be a JSON object")
    canary = data.get("canary")
    rollback = data.get("rollback")
    if not isinstance(canary, dict):
        raise ValueError("rollout metadata canary object is required")
    if canary.get("result") != "passed":
        raise ValueError("rollout canary result must be 'passed'")
    if not isinstance(canary.get("scope"), str) or not canary["scope"].strip():
        raise ValueError("rollout canary scope is required")
    if not isinstance(rollback, dict):
        raise ValueError("rollout metadata rollback object is required")
    for field in ("strategy", "target"):
        if not isinstance(rollback.get(field), str) or not rollback[field].strip():
            raise ValueError(f"rollout rollback {field} is required")
    if data.get("candidate_sha256") != _artifact_hash(candidate_path):
        raise ValueError("rollout candidate_sha256 does not match --candidate")
    return {
        "canary_scope": canary["scope"].strip(),
        "rollback_strategy": rollback["strategy"].strip(),
    }


def check(baseline: Dict[str, float], candidate: Dict[str, float], args: argparse.Namespace) -> List[str]:
    """Return deterministic budget failures; kept pure for CI/release tooling."""
    failures = []
    if candidate["ann_recall_at_10"] < args.min_ann_recall:
        failures.append(
            f"ANN recall@10 {candidate['ann_recall_at_10']:.3f} is below {args.min_ann_recall:.3f}"
        )
    for key, label in (
        ("ann_recall_at_10", "ANN recall@10"),
        ("citation_entailment_precision", "citation-entailment precision"),
    ):
        loss = baseline[key] - candidate[key]
        if loss > args.max_quality_loss:
            failures.append(
                f"{label} lost {loss:.3f}, over {args.max_quality_loss:.3f} budget"
            )
    for key, label in (
        ("retrieval_p95_ms", "retrieval p95"),
        ("query_provider_cost_usd", "per-query provider cost"),
        ("formation_queue_p95_ms", "formation queue p95"),
    ):
        if baseline[key] <= 0:
            failures.append(f"baseline {label} must be positive for a relative budget")
            continue
        increase = candidate[key] / baseline[key] - 1
        if increase > args.max_relative_increase:
            failures.append(
                f"{label} increased {increase:.1%}, over {args.max_relative_increase:.1%} budget"
            )
    return failures


def main() -> int:
    args = parse_args()
    try:
        baseline = _load(args.baseline)
        candidate = _load(args.candidate)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.change_kind in {"model", "prompt"}:
        if args.rollout_metadata is None:
            print(
                "--rollout-metadata is required for model and prompt releases",
                file=sys.stderr,
            )
            return 2
        try:
            rollout = validate_rollout_metadata(args.rollout_metadata, args.candidate)
        except ValueError as exc:
            print(f"ROLLOUT METADATA REJECTED: {exc}", file=sys.stderr)
            return 2
        print(
            f"canary passed scope={rollout['canary_scope']} "
            f"rollback={rollout['rollback_strategy']}"
        )
    failures = check(baseline, candidate, args)
    for key in REQUIRED:
        print(f"{key}: baseline={baseline[key]:.6g} candidate={candidate[key]:.6g}")
    if failures:
        if args.approval:
            try:
                approval = validate_approval(args.approval, args.baseline, args.candidate)
            except ValueError as exc:
                print(f"BUDGET EXCEPTION REJECTED: {exc}", file=sys.stderr)
            else:
                print(
                    "recorded release exception accepted "
                    f"approved_by={approval['approved_by']} reason={approval['reason']}"
                )
                return 0
        for failure in failures:
            print(f"BUDGET FAIL: {failure}", file=sys.stderr)
        return 1
    print("release budgets passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
