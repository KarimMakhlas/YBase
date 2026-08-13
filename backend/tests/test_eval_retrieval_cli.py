"""CLI contract for the ANN recall evaluator."""

from pathlib import Path
import subprocess
import sys


def test_eval_retrieval_help_describes_required_workspace_and_recall_gate():
    script = Path(__file__).resolve().parents[2] / "scripts" / "eval_retrieval.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--workspace" in result.stdout
    assert "--min-recall" in result.stdout
    assert "--feedback-regressions" in result.stdout
