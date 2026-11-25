"""Legacy bridge to the v1 HPE-tool launcher.

The v2 protocol deliberately does not reimplement the upstream downloader. If
the v1 launcher is available (sibling skill directory or HPE_LEGACY_RUNNER), the
legacy subcommand forwards arguments unchanged. Otherwise it fails with a clear
message instead of pretending to support legacy mode.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import PACKAGE_ROOT
from .errors import ConfigError


def find_legacy_runner() -> Path | None:
    env = os.environ.get("HPE_LEGACY_RUNNER")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    # This repository keeps the v1 skill next to the v2 implementation.
    candidates.extend(
        [
            PACKAGE_ROOT.parent / "hpe-paper-evaluator" / "hpe-paper-evaluator" / "scripts" / "hpe_runner.py",
            PACKAGE_ROOT.parent / "hpe_runner.py",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def run_legacy(args: list[str]) -> int:
    runner = find_legacy_runner()
    if runner is None:
        raise ConfigError(
            "Legacy runner not found. Set HPE_LEGACY_RUNNER to the v1 hpe_runner.py path, "
            "or run the old skill directly. v2 never fabricates legacy output."
        )
    completed = subprocess.run([sys.executable, str(runner), *args], check=False)
    return completed.returncode
