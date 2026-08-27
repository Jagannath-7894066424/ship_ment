"""
Source registry helpers - resolving a document to its `source` row.

Shared by both branches: every loader, chemical or oil, has to answer "which
source.id is this file?" before it can write a row. These functions used to live
in cargo_chemicals.py, which meant the oil loaders imported a chemical module to
get at them; they are branch-agnostic, so they live here instead.

Matching is deliberately fuzzy. Source names are typed by hand into source.json
and derived from file names that carry editions, years and inconsistent dashes,
so an exact string compare would miss far more often than it would hit:

    get_source_id          exact after normalization (dashes, trailing year)
    get_source_id_partial  either name contained in the other, longest wins
    create_source          last resort, only when a loader asks for it
"""

import logging
import re
from pathlib import Path

log = logging.getLogger("sources")


def derive_source_name(path: Path) -> str:
    """Source name = file name up to the first extension.

    'Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv'
    -> 'Lars Stole Birkeland - Chemical Cargo specifications - 2002'
    """
    name = path.name
    for ext in (".xlsx", ".xls", ".csv"):
        if ext in name:
            return name.split(ext)[0].strip()
    return path.stem.strip()


def normalize_source_name(s: str) -> str:
    """Normalize a source name for fuzzy comparison.

    - lowercase
    - unify dashes: em/en dash -> hyphen
    - drop a trailing year (e.g. ' - 2002')
    - collapse whitespace
    """
    s = s.lower()
    s = s.replace("—", "-").replace("–", "-")  # — – -> -
    s = re.sub(r"[-\s]*\d{4}\s*$", "", s)                 # trailing year
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def get_source_id(cur, name):
    """Return source.id whose name fuzzy-matches `name`, or None if none do."""
    target = normalize_source_name(name)
    log.info("Looking up source by normalized name: %r", target)
    cur.execute("SELECT id, name FROM source")
    for sid, sname in cur.fetchall():
        if normalize_source_name(sname) == target:
            log.info("Matched source id=%s (%r)", sid, sname)
            return sid
    return None


def _alnum(s: str) -> str:
    """Lowercase and reduce to space-separated alphanumeric tokens."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def get_source_id_partial(cur, name):
    """Return source.id that PARTIALLY matches `name`, or None.

    Compares on alphanumeric-lowercased text: a match is when either the stored
    source name is contained in `name` or vice versa (e.g. file
    'Unknown - Products CHEM - 1996' vs a source named 'Products CHEM'). The
    longest such match wins so a short generic name can't shadow a specific one.
    """
    target = _alnum(name)
    log.info("Looking up source by PARTIAL name match: %r", target)
    cur.execute("SELECT id, name FROM source")
    best = None  # (match_len, id, name)
    for sid, sname in cur.fetchall():
        ns = _alnum(sname)
        if ns and (ns in target or target in ns):
            if best is None or len(ns) > best[0]:
                best = (len(ns), sid, sname)
    if best is None:
        return None
    log.info("Partial source match id=%s (%r) for %r", best[1], best[2], name)
    return best[1]


def create_source(cur, name):
    """Insert a new source row named `name` and return its id."""
    log.info("Creating new source row: %r", name)
    cur.execute(
        "INSERT INTO source (name, source_type, notes, date_ingested, created_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now(), now()) RETURNING id",
        (name, "reference", "Auto-created during cargo_chemicals import"),
    )
    sid = cur.fetchone()[0]
    log.info("✓ Created source id=%s (%r)", sid, name)
    return sid
