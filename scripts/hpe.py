#!/usr/bin/env python3
"""Thin entry point for the HPE v2 CLI."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/hpe.py ...` from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hpe.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
