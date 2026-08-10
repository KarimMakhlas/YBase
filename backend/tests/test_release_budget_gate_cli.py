"""CLI contract for release metric budget comparisons."""

from pathlib import Path
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
