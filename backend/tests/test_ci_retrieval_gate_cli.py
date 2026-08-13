"""CLI contract for the deterministic CI retrieval release gate."""

from pathlib import Path
import importlib.util
import math
import subprocess
import sys


def test_ci_retrieval_gate_help_describes_recall_budget():
    script = Path(__file__).resolve().parents[2] / "scripts" / "ci_retrieval_gate.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--min-recall" in result.stdout
    assert "--documents" in result.stdout


def test_ci_gate_vectors_have_an_unambiguous_exact_top_k_boundary():
    """The gate measures ANN recall, not which equal-score hash embedding
    happens to land on the fifth-neighbor boundary."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "ci_retrieval_gate.py"
    spec = importlib.util.spec_from_file_location("ci_retrieval_gate", script)
    gate = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(gate)

    vectors = [gate.deterministic_gate_vector(index) for index in range(16)]
    assert all(math.isclose(sum(value * value for value in vector), 1.0) for vector in vectors)
    for index, vector in enumerate(vectors):
        scores = sorted(
            (sum(left * right for left, right in zip(vector, other)), other_index)
            for other_index, other in enumerate(vectors)
            if other_index != index
        )
        assert scores[9][0] != scores[10][0]
