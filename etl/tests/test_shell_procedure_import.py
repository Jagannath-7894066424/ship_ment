#!/usr/bin/env python3
"""
Tests for the Shell procedure-template import.

No pytest in this project, so these are plain asserts with a tiny runner:

    python3 etl/tests/test_shell_procedure_import.py
    python3 etl/tests/test_shell_procedure_import.py --source-id 24

Every test reads the database the importer wrote. Test 2 re-runs the importer to
prove idempotency; test 9 writes a cleaning_process row inside a transaction it
then rolls back, so the suite leaves the database exactly as it found it.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent.parent
IMPORTER = REPO / "etl" / "shell_procedure_templates.py"
SOURCE_NAME = "Shell Tank Cleaning Procedure"

EXPECTED_CODES = {"WD", "BF", "BF-VP", "CW", "CW-VP", "CWM", "CFW", "HW", "HWM", "HFW", "NC"}

_results = []


def check(name):
    """Decorator registering a test function."""
    def wrap(fn):
        _results.append((name, fn))
        return fn
    return wrap


def steps_of(cur, source_id, code):
    cur.execute("""
        SELECT s.step_order, s.step_name, s.step_type::text, s.medium, s.mandatory
          FROM procedure_template_steps s
          JOIN procedure_templates t ON t.id = s.procedure_templates_id
         WHERE t.source_id = %s AND t.procedure_code = %s
         ORDER BY s.step_order ASC
    """, (source_id, code))
    return cur.fetchall()


def requirements_of(cur, source_id, code):
    cur.execute("""
        SELECT r.display_order, r.requirement_type, r.requirement_value, r.operator,
               r.unit, r.mandatory
          FROM procedure_template_requirement r
          JOIN procedure_templates t ON t.id = r.procedure_template_id
         WHERE t.source_id = %s AND t.procedure_code = %s
         ORDER BY r.display_order ASC
    """, (source_id, code))
    return cur.fetchall()


def instructions_of(cur, source_id, code):
    cur.execute("""
        SELECT i.display_order, i.instruction_type::text, i.message, i.mandatory
          FROM procedure_template_instruction i
          JOIN procedure_templates t ON t.id = i.procedure_templates_id
         WHERE t.source_id = %s AND t.procedure_code = %s
         ORDER BY i.display_order ASC
    """, (source_id, code))
    return cur.fetchall()


# ---------------------------------------------------------------------------
@check("Test 1  - all 11 procedures imported, with their child rows")
def test_import(cur, source_id):
    cur.execute("SELECT procedure_code FROM procedure_templates WHERE source_id = %s",
                (source_id,))
    codes = {r[0] for r in cur.fetchall()}
    assert codes == EXPECTED_CODES, f"expected {sorted(EXPECTED_CODES)}, got {sorted(codes)}"
    assert len(codes) == 11, f"expected 11 templates, got {len(codes)}"

    cur.execute("""SELECT count(*) FROM procedure_template_steps s
                     JOIN procedure_templates t ON t.id = s.procedure_templates_id
                    WHERE t.source_id = %s""", (source_id,))
    assert cur.fetchone()[0] == 37, "expected 37 step rows"

    cur.execute("""SELECT count(*) FROM procedure_template_requirement r
                     JOIN procedure_templates t ON t.id = r.procedure_template_id
                    WHERE t.source_id = %s""", (source_id,))
    assert cur.fetchone()[0] == 25, "expected 25 requirement rows"

    cur.execute("""SELECT count(*) FROM procedure_template_instruction i
                     JOIN procedure_templates t ON t.id = i.procedure_templates_id
                    WHERE t.source_id = %s""", (source_id,))
    assert cur.fetchone()[0] == 4, "expected 4 instruction rows"


@check("Test 2  - second import creates no duplicates")
def test_idempotent(cur, source_id):
    def counts():
        cur.connection.commit()          # see what the subprocess committed
        cur.execute("""
            SELECT (SELECT count(*) FROM procedure_templates WHERE source_id = %s),
                   (SELECT count(*) FROM procedure_template_steps s
                      JOIN procedure_templates t ON t.id = s.procedure_templates_id
                     WHERE t.source_id = %s),
                   (SELECT count(*) FROM procedure_template_requirement r
                      JOIN procedure_templates t ON t.id = r.procedure_template_id
                     WHERE t.source_id = %s),
                   (SELECT count(*) FROM procedure_template_instruction i
                      JOIN procedure_templates t ON t.id = i.procedure_templates_id
                     WHERE t.source_id = %s)
        """, (source_id,) * 4)
        return cur.fetchone()

    before = counts()
    proc = subprocess.run(
        [sys.executable, str(IMPORTER), "--source-id", str(source_id)],
        cwd=str(REPO / "etl"), capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"re-import failed:\n{proc.stderr}"
    after = counts()
    assert before == after, f"row counts changed on re-import: {before} -> {after}"

    # And no duplicate slots anywhere.
    cur.execute("""
        SELECT count(*) FROM (
            SELECT procedure_templates_id, step_order
              FROM procedure_template_steps GROUP BY 1, 2 HAVING count(*) > 1) d
    """)
    assert cur.fetchone()[0] == 0, "duplicate (template, step_order) rows exist"


@check("Test 3  - WD: ROB <= 0.05 %, pump well only")
def test_wd(cur, source_id):
    reqs = {r[1]: r for r in requirements_of(cur, source_id, "WD")}
    assert "ROB_LIMIT" in reqs, "WD has no ROB_LIMIT requirement"
    _, _, value, operator, unit, mandatory = reqs["ROB_LIMIT"]
    assert value == "0.05", f"ROB_LIMIT value {value!r}"
    assert operator == "<=", f"ROB_LIMIT operator {operator!r}"
    assert unit == "%", f"ROB_LIMIT unit {unit!r}"
    assert mandatory is True

    assert reqs["ROB_LOCATION"][2] == "Pump Well Only", "ROB_LOCATION value"

    cur.execute("SELECT source_definition FROM procedure_templates "
                "WHERE source_id = %s AND procedure_code = 'WD'", (source_id,))
    definition = cur.fetchone()[0]
    assert definition and "drain" in definition.lower(), \
        "WD source_definition was not preserved"


@check("Test 4  - CFW: bulk = Cold Sea Water, final = Cold Fresh Water (mandatory)")
def test_cfw(cur, source_id):
    steps = steps_of(cur, source_id, "CFW")
    assert [s[1] for s in steps] == ["Well Drained", "Bulk Wash", "Final Wash", "Drain"], \
        f"CFW steps {[s[1] for s in steps]}"
    assert steps[1][3] == "Cold Sea Water", f"bulk wash medium {steps[1][3]!r}"
    assert steps[2][3] == "Cold Fresh Water", f"final wash medium {steps[2][3]!r}"

    reqs = {r[1]: r for r in requirements_of(cur, source_id, "CFW")}
    assert reqs["BULK_WASH_MEDIUM"][2] == "Cold Sea Water"
    assert reqs["BULK_WASH_MEDIUM"][5] is False, "bulk wash is an allowance, not mandatory"
    assert reqs["FINAL_WASH_MEDIUM"][2] == "Cold Fresh Water"
    assert reqs["FINAL_WASH_MEDIUM"][5] is True, "final fresh-water wash must be mandatory"

    # The bulk-wash exception must survive as source wording, not only as columns.
    msgs = " ".join(i[2] for i in instructions_of(cur, source_id, "CFW"))
    assert "Cold Sea Water" in msgs and "Cold Fresh Water" in msgs, \
        "CFW bulk-wash exception wording was lost"


@check("Test 5  - HFW: bulk = Hot Sea Water, final = Hot Fresh Water (mandatory)")
def test_hfw(cur, source_id):
    steps = steps_of(cur, source_id, "HFW")
    assert [s[1] for s in steps] == ["Well Drained", "Bulk Wash", "Final Wash", "Drain"]
    assert steps[1][3] == "Hot Sea Water", f"bulk wash medium {steps[1][3]!r}"
    assert steps[2][3] == "Hot Fresh Water", f"final wash medium {steps[2][3]!r}"

    reqs = {r[1]: r for r in requirements_of(cur, source_id, "HFW")}
    assert reqs["BULK_WASH_MEDIUM"][2] == "Hot Sea Water"
    assert reqs["BULK_WASH_MEDIUM"][5] is False
    assert reqs["FINAL_WASH_MEDIUM"][2] == "Hot Fresh Water"
    assert reqs["FINAL_WASH_MEDIUM"][5] is True


@check("Test 6  - CW-VP: WD, Cold Water Wash, Drain, Ventilate OR Purge (one requirement)")
def test_cw_vp(cur, source_id):
    steps = steps_of(cur, source_id, "CW-VP")
    assert [s[1] for s in steps] == \
        ["Well Drained", "Cold Water Wash", "Drain", "Ventilate OR Purge"], \
        f"CW-VP steps {[s[1] for s in steps]}"

    for code in ("BF-VP", "CW-VP", "HW"):
        reqs = requirements_of(cur, source_id, code)
        atmos = [r for r in reqs if r[1] == "ATMOSPHERE_TREATMENT"]
        assert len(atmos) == 1, f"{code}: expected exactly 1 ATMOSPHERE_TREATMENT, got {len(atmos)}"
        assert atmos[0][2] == "VENTILATE_OR_PURGE", f"{code}: {atmos[0][2]!r}"
        # The OR must not have been split into two mandatory obligations.
        types = [r[1] for r in reqs]
        assert "VENTILATION_REQUIRED" not in types and "PURGE_REQUIRED" not in types, \
            f"{code}: the OR was split into separate mandatory requirements"

    cur.execute("""SELECT ventilation_required, inert_gas_required
                     FROM procedure_templates
                    WHERE source_id = %s AND procedure_code IN ('BF-VP','CW-VP','HW')""",
                (source_id,))
    for vent, inert in cur.fetchall():
        assert not (vent and inert), \
            "ventilation_required AND inert_gas_required both true asserts both are mandatory"


@check("Test 7  - CWM / HWM: wash, drain, gas-free, mop dry")
def test_cwm_hwm(cur, source_id):
    assert [s[1] for s in steps_of(cur, source_id, "CWM")] == \
        ["Well Drained", "Cold Water Wash", "Drain", "Render Gas-Free", "Mop Dry"]
    assert [s[1] for s in steps_of(cur, source_id, "HWM")] == \
        ["Well Drained", "Hot Water Wash", "Drain", "Render Gas-Free", "Mop Dry"]

    for code in ("CWM", "HWM"):
        types = {s[2] for s in steps_of(cur, source_id, code)}
        assert "GAS_FREEING" in types and "MOPPING" in types, f"{code} step types {types}"
        reqs = {r[1]: r[2] for r in requirements_of(cur, source_id, code)}
        assert reqs.get("GAS_FREE") == "REQUIRED"
        assert reqs.get("MOP_DRY") == "REQUIRED"


@check("Test 8  - NC: loading_allowed = false, no steps")
def test_nc(cur, source_id):
    cur.execute("""SELECT template_name, loading_allowed FROM procedure_templates
                    WHERE source_id = %s AND procedure_code = 'NC'""", (source_id,))
    name, allowed = cur.fetchone()
    assert name == "Not Compatible", f"NC template_name {name!r}"
    assert allowed is False, f"NC loading_allowed is {allowed!r}, expected False"
    assert steps_of(cur, source_id, "NC") == [], "NC must have no steps"

    # Every other procedure is a real one and must allow loading.
    cur.execute("""SELECT procedure_code FROM procedure_templates
                    WHERE source_id = %s AND procedure_code <> 'NC'
                      AND loading_allowed IS DISTINCT FROM true""", (source_id,))
    assert cur.fetchall() == [], "a non-NC procedure has loading_allowed <> true"


@check("Test 9  - cleaning_process -> procedure_template_id -> template -> steps")
def test_cleaning_process_integration(cur, source_id):
    cur.execute("""SELECT id FROM procedure_templates
                    WHERE source_id = %s AND procedure_code = 'CW'""", (source_id,))
    template_id = cur.fetchone()[0]

    cur.execute("SELECT id FROM crude_oil ORDER BY id LIMIT 2")
    oils = [r[0] for r in cur.fetchall()]
    assert len(oils) == 2, "need two crude_oil rows to exercise an OIL transition"

    # Written inside a savepoint and rolled back: the test proves the join path
    # works without leaving a fabricated matrix row behind.
    cur.execute("SAVEPOINT integration")
    cur.execute("""
        INSERT INTO cleaning_process
            (cargo_type, from_cargo_id, to_cargo_id, procedure_template_id,
             procedure_code, source_id, source_page_ref, created_at, updated_at)
        VALUES ('OIL', %s, %s, %s, 'CW', %s, '3', now(), now())
        RETURNING id
    """, (oils[0], oils[1], template_id, source_id))
    process_id = cur.fetchone()[0]

    cur.execute("""
        SELECT t.procedure_code, t.template_name, t.loading_allowed,
               count(s.id) AS step_count
          FROM cleaning_process cp
          JOIN procedure_templates t ON t.id = cp.procedure_template_id
          LEFT JOIN procedure_template_steps s ON s.procedure_templates_id = t.id
         WHERE cp.id = %s
         GROUP BY t.procedure_code, t.template_name, t.loading_allowed
    """, (process_id,))
    code, name, allowed, step_count = cur.fetchone()
    assert code == "CW" and name == "Cold Water Wash", f"resolved to {code!r}/{name!r}"
    assert allowed is True
    assert step_count == 3, f"CW should reach 3 steps through the join, got {step_count}"

    # cleaning_process must NOT carry its own copy of the procedure's steps.
    cur.execute("SELECT count(*) FROM cleaning_process_step WHERE cleaning_process_id = %s",
                (process_id,))
    assert cur.fetchone()[0] == 0, "cleaning_process duplicated the template's steps"

    cur.execute("ROLLBACK TO SAVEPOINT integration")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-id", type=int)
    args = ap.parse_args()

    url = dotenv_values(REPO / ".env")["DATABASE_URL"]
    conn = psycopg2.connect(url)
    cur = conn.cursor()

    source_id = args.source_id
    if source_id is None:
        cur.execute("SELECT id FROM source WHERE name = %s", (SOURCE_NAME,))
        row = cur.fetchone()
        if row is None:
            print(f"FAIL: source {SOURCE_NAME!r} not found. Run the importer first.")
            return 1
        source_id = row[0]

    print(f"Database : {url.split('@')[-1]}")
    print(f"Source   : id={source_id} ({SOURCE_NAME})\n")

    failed = 0
    for name, fn in _results:
        try:
            fn(cur, source_id)
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n          {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")
            conn.rollback()

    conn.rollback()          # discard anything a test wrote
    conn.close()

    print(f"\n{len(_results) - failed}/{len(_results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
