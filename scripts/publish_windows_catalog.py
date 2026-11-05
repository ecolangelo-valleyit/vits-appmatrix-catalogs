"""Validate generator output and write the GitHub Pages catalog directory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "windows-catalog-generator"))

from winget_catalog.publish import main

if __name__ == "__main__":
    raise SystemExit(main())
