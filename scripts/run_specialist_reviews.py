#!/usr/bin/env python3
"""Run the specialist session runtime from the action workspace."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pr_reviewer.specialist_runtime.cli import main  # noqa: E402


raise SystemExit(main())
