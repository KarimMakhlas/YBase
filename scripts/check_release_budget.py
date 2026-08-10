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
import json
import sys
from pathlib import Path
from typing import Dict, List


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
    failures = check(baseline, candidate, args)
    for key in REQUIRED:
        print(f"{key}: baseline={baseline[key]:.6g} candidate={candidate[key]:.6g}")
    if failures:
        for failure in failures:
            print(f"BUDGET FAIL: {failure}", file=sys.stderr)
        return 1
    print("release budgets passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
