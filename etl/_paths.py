"""Shared path helpers for the ETL scripts.

Input data files (the source spreadsheets / CSVs) live *outside* the repo and
differ per developer, so we never hardcode machine-specific paths. Instead each
developer points the ETL at their own copy via the ``SHIP_DATA_DIR`` environment
variable (set it in the repo-root ``.env`` — see ``.env.example``).

Resolution order for a default input file:

    1. ``SHIP_DATA_DIR`` from the environment / repo-root ``.env`` (if set).
    2. Fallback: ``etl/data/inputs/`` inside the repo.

Importing this module loads the repo-root ``.env`` immediately, so callers can
compute module-level ``DEFAULT_FILE = input_file("...")`` and still pick up
``SHIP_DATA_DIR`` from ``.env``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# etl/_paths.py -> repo root is two levels up.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Load the repo-root .env on import so SHIP_DATA_DIR is available before any
# caller defines its DEFAULT_FILE. (load_dotenv does not override real env vars.)
load_dotenv(REPO_ROOT / ".env")


def data_dir() -> Path:
    """Base directory that holds the source input files.

    Set ``SHIP_DATA_DIR`` to wherever you keep the spreadsheets/CSVs; otherwise
    the scripts look in ``etl/data/inputs/``.
    """
    base = os.environ.get("SHIP_DATA_DIR")
    if base:
        return Path(base).expanduser()
    return Path(__file__).resolve().parent / "data" / "inputs"


def input_file(name: str) -> Path:
    """Resolve a default input file *name* under the configured data directory."""
    return data_dir() / name
