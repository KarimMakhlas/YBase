"""CLI contract for atomic embedding-version operations."""

from pathlib import Path
import subprocess
import sys


def test_reembed_help_requires_explicit_version_workflow():
    script = Path(__file__).resolve().parents[2] / "scripts" / "reembed.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "--workspace" in result.stdout
    assert "--activate" in result.stdout
    assert "--rollback-to" in result.stdout
