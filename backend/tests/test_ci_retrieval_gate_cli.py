"""CLI contract for the deterministic CI retrieval release gate."""

from pathlib import Path
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
