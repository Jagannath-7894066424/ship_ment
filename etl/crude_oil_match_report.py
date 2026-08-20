#!/usr/bin/env python3
"""Cross-check the two crude-oil sources against each other. Read-only.

This writes NOTHING to the database. The two sources deliberately keep separate
crude_oil rows - identity is (oil_name, source_id) - so this is a validation
aid, not a merge step. It answers three questions:

  1. Which oils appear in both sources, by exact normalized name?
  2. Which are only near-matches and therefore need a human decision?
  3. Where both sources describe the same oil, do API and pour point agree?

Pour point is published in °F by the basic source and °C by the assay source,
so it is converted here purely for comparison; the stored values are never
touched.
"""

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("crude_oil_match")

BASIC = "Crude Oil Basic Properties"
ASSAY = "Crude Oil Assay and Operational Properties"
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "crude_oil_match_report.csv"

# Pour point is quoted to the nearest few degrees and the two sources are years
# apart, so only a gap wider than this is worth a human look.
POUR_TOLERANCE_C = 5.0
API_TOLERANCE = 1.0


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def fetch(cur, source_name: str) -> dict:
    """oil_name -> {field: row} for one source."""
    cur.execute("SELECT id FROM source WHERE name = %s", (source_name,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"Error: source {source_name!r} not found.")
    source_id = row[0]

    cur.execute(
        """
        SELECT o.oil_name, o.country_of_origin, p.field_name, p.value,
               p.normalized_value, p.normalized_min, p.normalized_max, p.unit
        FROM crude_oil o
        LEFT JOIN crude_oil_property_values p ON p.crude_oil_id = o.id
        WHERE o.source_id = %s
        """,
        (source_id,),
    )
    oils = {}
    for name, country, field, value, nval, nmin, nmax, unit in cur.fetchall():
        entry = oils.setdefault(name, {"country": country, "props": {}})
        if field:
            entry["props"][field] = {
                "value": value, "n": nval, "min": nmin, "max": nmax, "unit": unit,
            }
    return oils


def api_verdict(basic_prop, assay_prop):
    """Is the basic source's single API value consistent with the assay range?"""
    if not basic_prop or not assay_prop:
        return "", ""
    a = basic_prop["n"]
    if a is None:
        return "", ""
    lo, hi, point = assay_prop["min"], assay_prop["max"], assay_prop["n"]

    if lo is not None and hi is not None:
        detail = f"{a} vs {lo}-{hi}"
        return ("consistent" if lo <= a <= hi else "outside range"), detail
    ref = point if point is not None else lo
    if ref is None:
        return "", ""
    detail = f"{a} vs {ref}"
    return ("consistent" if abs(a - ref) <= API_TOLERANCE else "differs"), detail


def pour_verdict(basic_prop, assay_prop):
    """Compare pour points after converting the basic source's °F to °C."""
    if not basic_prop or not assay_prop:
        return "", ""
    if basic_prop["n"] is None:
        return "", ""
    basic_c = f_to_c(basic_prop["n"])

    lo, hi, point = assay_prop["min"], assay_prop["max"], assay_prop["n"]
    if lo is not None and hi is not None:
        detail = f"{basic_prop['value']}°F = {basic_c:.1f}°C vs {lo}..{hi}°C"
        if lo - POUR_TOLERANCE_C <= basic_c <= hi + POUR_TOLERANCE_C:
            return "consistent", detail
        return "outside range", detail

    ref = point if point is not None else (lo if lo is not None else hi)
    if ref is None:
        return "", ""
    detail = f"{basic_prop['value']}°F = {basic_c:.1f}°C vs {ref}°C"
    return ("consistent" if abs(basic_c - ref) <= POUR_TOLERANCE_C else "differs"), detail


def near_matches(only_basic, only_assay):
    """Names where one side is a prefix of the other - candidates, never merges.

    'Arabian Light' / 'Arabian Light - Berri' are different crudes, so these are
    reported for a human to decide and nothing is done automatically.
    """
    out = []
    for b in sorted(only_basic):
        for a in sorted(only_assay):
            if b == a:
                continue
            if b.startswith(a) or a.startswith(b):
                out.append((b, a))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="CSV report path")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            basic = fetch(cur, BASIC)
            assay = fetch(cur, ASSAY)
    finally:
        conn.close()

    both = sorted(set(basic) & set(assay))
    only_basic = set(basic) - set(assay)
    only_assay = set(assay) - set(basic)

    rows = []
    counts = {"api_consistent": 0, "api_flagged": 0,
              "pour_consistent": 0, "pour_flagged": 0}
    for name in both:
        bp, apr = basic[name]["props"], assay[name]["props"]
        av, ad = api_verdict(bp.get("API"), apr.get("API"))
        pv, pd = pour_verdict(bp.get("POUR_POINT"), apr.get("POUR_POINT"))
        if av == "consistent":
            counts["api_consistent"] += 1
        elif av:
            counts["api_flagged"] += 1
        if pv == "consistent":
            counts["pour_consistent"] += 1
        elif pv:
            counts["pour_flagged"] += 1
        rows.append({
            "oil_name": name, "match_type": "exact",
            "country_basic": basic[name]["country"] or "",
            "api_verdict": av, "api_detail": ad,
            "pour_verdict": pv, "pour_detail": pd,
        })

    near = near_matches(only_basic, only_assay)
    for b, a in near:
        rows.append({
            "oil_name": b, "match_type": f"near -> {a}",
            "country_basic": basic[b]["country"] or "",
            "api_verdict": "", "api_detail": "",
            "pour_verdict": "", "pour_detail": "needs human validation",
        })

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["oil_name", "match_type", "country_basic",
                                           "api_verdict", "api_detail",
                                           "pour_verdict", "pour_detail"])
        w.writeheader()
        w.writerows(rows)

    log.info("=" * 66)
    log.info("%-34s %5d", f"oils in basic source", len(basic))
    log.info("%-34s %5d", f"oils in assay source", len(assay))
    log.info("%-34s %5d", "exact name matches (both)", len(both))
    log.info("%-34s %5d", "only in basic", len(only_basic))
    log.info("%-34s %5d", "only in assay", len(only_assay))
    log.info("%-34s %5d", "near matches (need validation)", len(near))
    log.info("-" * 66)
    log.info("API   : %d consistent, %d flagged", counts["api_consistent"], counts["api_flagged"])
    log.info("Pour  : %d consistent, %d flagged (°F converted to °C for the check)",
             counts["pour_consistent"], counts["pour_flagged"])
    log.info("-" * 66)

    flagged = [r for r in rows if r["api_verdict"] not in ("", "consistent")
               or r["pour_verdict"] not in ("", "consistent")]
    if flagged:
        log.info("flagged pairs (first 12):")
        for r in flagged[:12]:
            log.info("  %-34s API %-14s %s", r["oil_name"][:34],
                     r["api_verdict"] or "-", r["pour_verdict"] or "-")
    if near:
        log.info("near matches (first 8):")
        for b, a in near[:8]:
            log.info("  %r  ~  %r", b[:40], a[:40])
    log.info("=" * 66)
    log.info("report -> %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
