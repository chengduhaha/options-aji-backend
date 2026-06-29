"""CLI entrypoint for Baidu Netdisk blog document imports."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_import_cli_help_lists_safety_options() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_baidu_blog_documents.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--dry-run" in result.stdout
    assert "--min-free-gb" in result.stdout
    assert "--yes" in result.stdout
