"""CLI contract for the disposable retrieval load profile."""

from pathlib import Path
import subprocess
import sys


def test_retrieval_load_profile_help_exposes_scale_and_latency_budget():
    script = Path(__file__).resolve().parents[2] / "scripts" / "retrieval_load_profile.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--chunks" in result.stdout
    assert "--max-p95-ms" in result.stdout
