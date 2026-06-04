"""Make `polybot` and `services` importable when pytest is run from repo root."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                  # for `services.*`
sys.path.insert(0, str(ROOT / "packages"))     # for `polybot.*`
