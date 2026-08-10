"""CLI contract for concurrent preprocessing/fairness load profiling."""

from pathlib import Path
import subprocess
import sys


def test_worker_load_profile_help_exposes_workspace_and_concurrency_scale():
    script = Path(__file__).resolve().parents[2] / "scripts" / "worker_load_profile.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "--workspaces" in result.stdout
    assert "--documents-per-workspace" in result.stdout
    assert "--concurrency" in result.stdout
