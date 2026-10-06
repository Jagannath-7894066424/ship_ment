#!/usr/bin/env python3
"""
Load the Tanker Safety Guide (Liquefied Gas) cargo tables into cargo_gas,
cargo_gas_property_values and synonyms + cargo_gas_synonym.

SOURCE
------
"Tanker Safety Guide - Liquefied Gas" (source.json, category 'gas'). The
FOURTEENTH gas source, and the broadest: 38 cargoes with forty-nine columns
each,
covering identity, regulation, physical properties, health effects, emergency
procedures and carriage requirements in one table. No other gas source spans
more than a few of those.

It is the first gas source that is a SAFETY guide rather than a data book, and
that is what it is ranked on. Half its columns are not measurements at all but
instructions - what to do when the cargo is on fire, what to do when a casualty
has inhaled the vapour - so it takes rank_health 1, the first gas source to
carry a health rank at all. It is ranked second for physical properties (the
data books that cite their own sources outrank it) and second for regulatory
status (it restates the IGC Code, which is ranked first in its own right).

It overlaps every other gas source by name and nothing is merged: cargo_gas is
keyed (gas_name, source_id), so each source keeps its own rows and a
disagreement between them stays visible.

INPUT
-----
"Tanker Safety Guide - Liquefied gas & Properties.csv": 38 cargoes, one header
row, 52 columns. The header is matched exactly, column by column - a re-export
that reorders the sheet stops this loader rather than loading a freezing point
into a boiling point.

Two of the 52 columns are not properties:

  * "SL No." is the guide's own entry number for the cargo. It is not a fact
    about the substance, so it is not a field; it is written to
    source_page_ref on every value of that row, which is what that column is
    for - a pointer back into the document a figure came from.
  * "Synonyms" is a list of other names, and a name is not a property of a
    cargo. It goes to the shared `synonyms` table through cargo_gas_synonym,
    the same route the chemical and oil branches use, so one row of text serves
    all three branches and a lookup by name is an indexed join rather than a
    scan of property values.

The remaining 49 columns each map to one field - see COLUMNS below.

WHAT GETS REUSED, AND WHAT IS NEW
---------------------------------
Twenty columns reuse fields that already exist, so the same measurement lands
in the same field whichever source it came from. Four of those reuses are worth
stating outright because the heading and the field name are not word-for-word
the same:

  * "Personal Protection" -> personal_protective_methods, the field Sittig's
    handbook already publishes into.
  * "Ship Type", "Vapour Detection", "Independent Tank Required", "Gauging"
    and "Control of Vapour within Cargo Tank" -> the five igc_* fields. These
    five columns are the guide REPUBLISHING IGC Code chapter 19: they use the
    code's own vocabulary ("2G/2PG", "Asphyxiant", "Indirect, Closed or
    Restricted"), and filing them anywhere else would hide the fact that two
    sources in this database now answer the same regulatory question - which
    is exactly the comparison the branch is built to support. Every such value
    records on itself that it comes from the guide restating the code, and the
    guide's wording is kept verbatim where it is fuller than the code's.

Twenty-nine columns are new (see etl/gas/_gas.py). Fifteen of them are health
and emergency text, which no gas source had before; the guide is the first gas
source in this database that says what to DO about a cargo rather than only
what it is.

One of the twenty-nine is a twin rather than a fresh idea: "Freezing Point" ->
freezing_point_c, the same quantity as the chemical branch's melting_point_c,
recorded under the name this source prints. The field's description says how to
query both at once. It follows the precedent already set for lel /
lower_inflammability_limit.

"Odour" and "Odour Threshold" are two different columns and two different
fields: `odour` is what the cargo smells LIKE, `odour_limit` is the
concentration at which it can be smelled. The guide's threshold is a detection
threshold, which is a lower figure than the recognition concentration another
source prints in the same field, so every value says which it is.

NOTHING IS SKIPPED
------------------
Every non-empty cell in all fifty columns is written. This loader deliberately
does NOT use _gas.clean_text, which maps '-', 'n/a' and 'none' to None: in this
file every one of those is an answer rather than a blank.

  * "None" under Electrostatic Generation is the guide saying the cargo does
    not accumulate a charge. That is the opposite of "Not known", which is also
    in the column, and folding both to NULL would destroy the distinction.
  * "N/A" under Ship Type is the guide saying the question does not arise -
    ethyl alcohol is carried on a gas tanker as an antifreeze additive, not as
    a cargo, so no ship type applies to it.
  * "Not available" is the guide stating that it has no figure, which is
    itself worth recording: a reader can tell it from a cell this loader never
    reached.

Only a genuinely empty cell produces no row.

"(NOTE N)" IS A CROSS-REFERENCE, NOT A VALUE
--------------------------------------------
The guide footnotes cargoes, and the footnote text lives in the row's own
"Notes and Special Requirements" cell. Other cells in the row point into it by
number. A marker appears two ways and is handled two ways:

  * alongside a figure - "383°C (Note 1)" - the figure is normalized as usual
    and the pointer is recorded on the value;
  * as the whole cell - "(Note 1)" - there is no figure, so the cell is stored
    verbatim as text with no normalized value, and its note says the guide
    answered this column by referring to the row's numbered note. Six cargoes
    (the C4 streams, the mixtures, LPG) are described almost entirely this way.

A marker is checked against the row's notes cell; one that points at a note the
row does not have is a validation error, because it means the cross-reference
cannot be followed.

FIGURES THE FILE CONTRADICTS ITSELF ABOUT
-----------------------------------------
Three checks are run over the parsed file, all of them decidable from the
file's own contents. A value that fails one is stored exactly as printed, with
is_winning = false and conflict_flag = true so a query cannot serve it back as
the answer, and `notes` saying what is wrong. Nothing is corrected: guessing
the intended figure would be inventing data.

  1. RELATIVE VAPOUR DENSITY AGAINST MOLECULAR WEIGHT. Vapour density relative
     to air is molecular weight divided by air's 28.96 g/mol, so the two
     columns are arithmetically tied. Across the 33 rows that print both as
     plain numbers, 32 agree to within 3%. Ammonia does not: it is given
     molecular weight 0.59 and relative vapour density 17.03. Both are stored
     and both are flagged. The two are consistent if read the other way round -
     ammonia's molecular weight is 17.03 and 17.03/28.96 = 0.588 - which the
     note records as an observation about the file, not as a correction.

  2. MOLECULAR WEIGHT BELOW 1 g/mol. No substance is lighter than atomic
     hydrogen. Catches the same ammonia cell independently of check 1.

  3. CAS REGISTRY NUMBERS. A CAS RN names exactly one substance, so:
       * a number that is not in n(2-7)-nn-n form is malformed - Propylene's
         "0115-07-01" and Sulphur Dioxide's "7446-09-05" have both been
         mangled by something that read them as dates;
       * a number whose check digit does not match the registry's own checksum
         is misprinted;
       * one number given as the SOLE CAS of two cargoes whose Formula cells
         differ cannot be right for both - "75-07-0" is given to Acetaldehyde
         (C2H4O), Ethyl Chloride (C2H5Cl) and Pentane (C5H12), and "64-17-5"
         to both Ethyl Alcohol (C2H6O) and Ethylamine (C2H7N).
     A cargo whose CAS cell lists SEVERAL numbers is exempt from the last
     check, because a mixture legitimately names one per component - the
     EO/PO mixture shares a number with Ethylene Oxide and another with
     Propylene Oxide, and both are correct.

REPEATED UN NUMBERS ARE NOTED, NEVER FLAGGED
--------------------------------------------
A UN number covers a GROUP of substances - 1965 is "hydrocarbon gas mixture,
liquefied, n.o.s." and correctly appears on both C4 streams - so two rows
sharing one is not evidence of an error the way a shared CAS RN is. Repeats are
recorded on the value and listed in the run log, and that is all. This is the
same rule etl/gas/imo_cargo_numbers.py applies to the same column.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id), (cargo_gas_id, source_id, field_name) and
(cargo_gas_id, synonym_id). The whole file is validated before anything is
written; one transaction.

Usage:
    python3 etl/gas/tanker_safety_guide.py
    python3 etl/gas/tanker_safety_guide.py --dry-run
    python3 etl/gas/tanker_safety_guide.py "/path/to/...& Properties.csv"
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import (ensure_field_definitions, link_synonym, upsert_gas,  # noqa: E402
                  upsert_property, upsert_synonym)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_tanker_safety_guide")

SOURCE_NAME = "Tanker Safety Guide - Liquefied Gas"
ENTERED_BY = "tanker_safety_guide.py"
DEFAULT_FILE = input_file("Tanker Safety Guide - Liquefied gas & Properties.csv")

# The three columns that are not property fields.
ENTRY_NO_HEADER = "SL No."
NAME_HEADER = "Chemical Name"
SYNONYMS_HEADER = "Synonyms"
# The column every "(Note N)" marker in the row points into.
NOTES_FIELD = "notes_special_requirements"
NOTES_FIELD_HEADING = "Notes and Special Requirements"
# Air, for the relative-vapour-density cross-check. g/mol.
AIR_MOLECULAR_WEIGHT = 28.96


def collapse(value: Optional[str]) -> str:
    """Trim a cell and collapse its whitespace. Cells hold hard line breaks."""
    return re.sub(r"\s+", " ", (value or "").replace("\n", " ")).strip()


class Column(NamedTuple):
    heading: str          # the CSV header, exactly as printed
    field: str            # field_definitions.field_name
    label: str            # what to call it in a log line
    numeric: bool         # try to normalize a figure out of the cell
    unit: Optional[str]   # unit of a normalized value
    strip: Optional[str]  # unit text to remove before parsing a figure
    note: Optional[str]   # what the column heading says, recorded on every value


# Shared notes, so the same statement is worded identically on every value it
# applies to.
IGC_NOTE = ("The guide is republishing IGC Code chapter 19 here, in its own "
            "wording. Compare with the 'IGC Code 2016 Cargo Gas' source, which "
            "holds the code's own entry for the same requirement.")
LIQUID_NOTE = "Of the LIQUID phase."

COLUMNS: List[Column] = [
    Column("Appearance", "appearance", "Appearance", False, None, None, None),
    Column("Odour", "odour", "Odour", False, None, None,
           "What the cargo smells LIKE. The concentration at which it can be "
           "smelled is the separate 'Odour Threshold' column, in `odour_limit`."),
    Column("UN Number", "UN_NUMBER", "UN Number", False, None, None, None),
    Column("CASRN", "cas_number", "CAS Number", False, None, None, None),
    Column("MFAG", "mfag_number", "MFAG", False, None, None,
           "The guide gives MFAG table numbers as a LIST, and gives every "
           "cargo in the book the same list - these are the tables that apply "
           "to liquefied gases as a class, not to this cargo in particular."),
    Column("Main Hazard", "main_hazard", "Main Hazard", False, None, None, None),
    Column("Fire - Emergency Procedure", "emergency_procedure_fire",
           "Emergency: Fire", False, None, None, None),
    Column("Liquid in Eye - Emergency Procedure",
           "emergency_procedure_liquid_in_eye", "Emergency: Liquid in Eye",
           False, None, None, None),
    Column("Liquid on Skin - Emergency Procedure",
           "emergency_procedure_liquid_on_skin", "Emergency: Liquid on Skin",
           False, None, None, None),
    Column("Vapour Inhaled - Emergency Procedure",
           "emergency_procedure_vapour_inhaled", "Emergency: Vapour Inhaled",
           False, None, None, None),
    Column("Spillage - Emergency Procedure", "emergency_procedure_spillage",
           "Emergency: Spillage", False, None, None, None),
    Column("OEL-TLV", "oel_tlv", "OEL-TLV", True, "ppm", r"ppm",
           "The guide heads one column 'OEL-TLV' and prints limits on "
           "different averaging bases under it. Where it marks the basis "
           "(STEL, ceiling) the marking is kept on this value; where it does "
           "not, the basis is not stated by the source and must not be "
           "assumed to be an eight-hour average."),
    Column("Odour Threshold", "odour_limit", "Odour Threshold", True, "ppm", r"ppm",
           "The guide measures an odour DETECTION THRESHOLD - the "
           "concentration at which the smell is first noticeable. That is a "
           "lower figure than the 100% recognition concentration other "
           "sources print in this field."),
    Column("Effect of Liquid - Eyes", "effect_of_liquid_eyes",
           "Effect of Liquid: Eyes", False, None, None, None),
    Column("Effect of Liquid - Skin", "effect_of_liquid_skin",
           "Effect of Liquid: Skin", False, None, None, None),
    Column("Effect of Liquid - Skin Absorption",
           "effect_of_liquid_skin_absorption", "Effect of Liquid: Skin Absorption",
           False, None, None, None),
    Column("Effect of Liquid - Ingestion", "effect_of_liquid_ingestion",
           "Effect of Liquid: Ingestion", False, None, None, None),
    Column("Effect of Vapour - Eyes", "effect_of_vapour_eyes",
           "Effect of Vapour: Eyes", False, None, None, None),
    Column("Effect of Vapour - Skin", "effect_of_vapour_skin",
           "Effect of Vapour: Skin", False, None, None, None),
    Column("Effect of Vapour - Inhalation (Acute)",
           "effect_of_vapour_inhalation_acute", "Effect of Vapour: Acute",
           False, None, None, None),
    Column("Effect of Vapour - Inhalation (Chronic)",
           "effect_of_vapour_inhalation_chronic", "Effect of Vapour: Chronic",
           False, None, None, None),
    Column("Personal Protection", "personal_protective_methods",
           "Personal Protection", False, None, None, None),
    Column("Flashpoint", "flash_point_c", "Flashpoint", True, "°C", r"°C",
           "Closed cup or open cup where the guide states it, on this value."),
    Column("Explosion Hazard", "explosion_hazard", "Explosion Hazard",
           False, None, None, None),
    Column("Auto-ignition Temperature", "auto_ignition_temperature",
           "Auto-ignition Temperature", True, "°C", r"°C", None),
    Column("Flammable Limits", "flammable_limits", "Flammable Limits",
           True, "% vol", r"%\s*by\s*volume\.?",
           "In air, in % by volume, per the column's own wording."),
    Column("Formula", "molecular_formula", "Formula", False, None, None, None),
    Column("Chemical Family", "chemical_family", "Chemical Family",
           False, None, None, None),
    Column("Reactivity - Water (Fresh or Salt)", "reactivity_water",
           "Reactivity: Water", False, None, None,
           "Fresh or salt water, per the column heading."),
    Column("Reactivity - Air", "reactivity_air", "Reactivity: Air",
           False, None, None, None),
    Column("Reactivity - Other Liquids or Gases",
           "reactivity_other_liquids_gases", "Reactivity: Other",
           False, None, None,
           "A warning in prose. It is NOT a compatibility verdict - "
           "cargo_gas_compatibility holds those - and must not be parsed into "
           "one: the guide names classes of chemistry as often as substances."),
    Column("Boiling Point", "boiling_point_c", "Boiling Point", True, "°C", r"°C",
           "At atmospheric pressure: the guide tabulates its other figures at "
           "this temperature, which is what a fully-refrigerated cargo is "
           "carried at."),
    Column("Freezing Point", "freezing_point_c", "Freezing Point", True, "°C", r"°C",
           None),
    Column("Latent Heat of Vaporisation (kJ/kg)",
           "latent_heat_of_vaporisation_kj_kg", "Latent Heat of Vaporisation",
           True, "kJ/kg", None, None),
    Column("Vapour Pressure (bar (A))", "vapour_pressure", "Vapour Pressure",
           True, "bar", None,
           "ABSOLUTE pressure, per the column heading's '(A)'. Where the "
           "guide quotes the cargo at more than one temperature the cell holds "
           "several figures and no single one is normalized - read the value."),
    Column("Relative Vapour Density", "RELATIVE_VAPOUR_DENSITY",
           "Relative Vapour Density", True, None, None,
           "Relative to air = 1. DIMENSIONLESS, so `unit` is NULL."),
    Column("Electrostatic Generation", "electrostatic_generation",
           "Electrostatic Generation", False, None, None, None),
    Column("Specific Gravity", "specific_gravity", "Specific Gravity",
           True, None, None,
           "Of the LIQUID, relative to water. DIMENSIONLESS, so `unit` is "
           "NULL. The temperature is on this value and is usually the cargo's "
           "boiling point, not a standard 15°C - the two are not "
           "interchangeable."),
    Column("Molecular Weight (g/mol)", "molecular_weight_g_mol",
           "Molecular Weight", True, "g/mol", None, None),
    Column("Coefficient of Cubic Expansion",
           "coefficient_of_cubic_expansion_per_c", "Coefficient of Cubic Expansion",
           True, "1/°C", r"per\s*°C", LIQUID_NOTE),
    Column("Normal Carriage Condition", "normal_carriage_condition",
           "Normal Carriage Condition", False, None, None, None),
    Column("Control of Vapour within Cargo Tank", "igc_vapour_space_control",
           "Control of Vapour in Tank", False, None, None, IGC_NOTE),
    Column("Ship Type", "igc_ship_type", "Ship Type", False, None, None, IGC_NOTE),
    Column("Vapour Detection", "igc_vapour_detection", "Vapour Detection",
           False, None, None, IGC_NOTE),
    Column("Independent Tank Required", "igc_independent_tank_c_required",
           "Independent Tank Required", False, None, None,
           IGC_NOTE + " The guide answers this column 'Yes', 'No' or 'Type C'; "
           "where it says only 'Yes' it has not named the tank type, and one "
           "must not be inferred."),
    Column("Gauging", "igc_gauging", "Gauging", False, None, None, IGC_NOTE),
    Column("Materials of Construction - Unsuitable",
           "materials_of_construction_unsuitable", "Materials: Unsuitable",
           False, None, None, None),
    Column("Materials of Construction - Suitable",
           "materials_of_construction_suitable", "Materials: Suitable",
           False, None, None, None),
    Column("Notes and Special Requirements", NOTES_FIELD,
           "Notes and Special Requirements", False, None, None,
           "The numbered notes the row's other cells point into with "
           "'(Note N)'. Kept whole and in the guide's numbering."),
]

# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------

NOTE_REF_RE = re.compile(r"\(\s*Notes?\s+([\d]+(?:\s*(?:,|and)\s*\d+)*)\s*\)",
                         re.IGNORECASE)
NUM = r"-?\d[\d,]*(?:\.\d+)?"
# A figure qualified by the state it was measured in: "569 at 20.4°C". Lower
# case 'at' with something before it, so "At or below -18°C" is left alone.
AT_RE = re.compile(r"(?<=\S)\s+at\s+")
# "n-butane: 1.00 at -0.5°C; Isobutane: ..." and "R12 = 165; R22 = 234" - the
# guide answering for more than one substance in one cell.
LABELLED_RE = re.compile(r"(^|;)\s*[A-Za-z][\w'./+-]*(\s+[\w'./+-]+){0,3}\s*[:=]\s")

CROSS_REF_NOTE = ("The guide answers this column by pointing at note {refs} in "
                  "this cargo's own 'Notes and Special Requirements' entry "
                  "rather than by stating a value, so there is nothing to "
                  "normalize. Read {field}.")
POINTER_NOTE = "The guide marks this value '(Note {refs})'; see {field}."
MULTI_NOTE = ("The guide states more than one figure in this cell - it is "
              "answering for several substances or several conditions at once - "
              "so no single normalized value can stand for it. The cell is kept "
              "whole; read it.")
QUALIFIER_NOTE = "The guide qualifies this figure: {quals}."
CONDITION_NOTE = "Measured at {cond}, per the source."
RESIDUE_NOTE = "The guide adds: {residue}."
WORDS_NOTE = ("The guide answers this column in words rather than with a "
              "figure; kept verbatim, with no normalized value.")


def to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class Parsed(NamedTuple):
    value_type: str
    normalized_value: Optional[float]
    normalized_min: Optional[float]
    normalized_max: Optional[float]
    notes: List[str]


def parse_measure(text: str, strip_unit: Optional[str]) -> Parsed:
    """One numeric cell -> a value type, its normalized figures and its notes.

    The cell itself is never rewritten; everything here decides only what can
    be put in the normalized_* columns beside it.
    """
    notes: List[str] = []
    work = text

    # 1. Cross-references to the row's numbered notes, wherever they appear.
    refs = [m.group(1) for m in NOTE_REF_RE.finditer(work)]
    work = NOTE_REF_RE.sub(" ", work).strip()
    if refs:
        notes.append(POINTER_NOTE.format(refs=", ".join(refs), field=NOTES_FIELD))
    if not work:
        # The marker WAS the cell.
        return Parsed("text", None, None, None,
                      [CROSS_REF_NOTE.format(refs=", ".join(refs), field=NOTES_FIELD)])

    # 2. More than one figure in the cell: nothing single to normalize.
    if ";" in work or LABELLED_RE.search(work):
        return Parsed("text", None, None, None, notes + [MULTI_NOTE])

    # 3. Parenthesised qualifiers - "(Closed Cup)", "(Liquid)", "(STEL-C)".
    quals = re.findall(r"\(([^()]*)\)", work)
    quals = [q.strip() for q in quals if q.strip()]
    work = re.sub(r"\([^()]*\)", " ", work).strip()
    if quals:
        notes.append(QUALIFIER_NOTE.format(quals="; ".join(quals)))

    # 4. The state the figure was measured in - "1.02 at -33°C".
    parts = AT_RE.split(work, maxsplit=1)
    if len(parts) == 2:
        work, cond = parts[0].strip(), parts[1].strip(" .")
        if cond:
            notes.append(CONDITION_NOTE.format(cond=cond))

    # 5. The unit the column prints, now that it is the only thing left.
    if strip_unit:
        work = re.sub(strip_unit, " ", work, flags=re.IGNORECASE)
    work = re.sub(r"°C", " ", work).strip(" .,")
    work = re.sub(r"\s+", " ", work).strip()
    if not work:
        return Parsed("text", None, None, None, notes + [WORDS_NOTE])

    def residue(rest: str) -> None:
        rest = rest.strip(" .,;")
        if rest:
            notes.append(RESIDUE_NOTE.format(residue=rest))

    # 6a. A range: "4 - 60", "0.4 to 50", "5,000 – 20,000".
    m = re.match(rf"^({NUM})\s*(?:-|–|—|to)\s*({NUM})\b", work)
    if m and to_float(m.group(1)) is not None and to_float(m.group(2)) is not None:
        residue(work[m.end():])
        return Parsed("range", None, to_float(m.group(1)), to_float(m.group(2)), notes)

    # 6b. A one-sided bound: "<1", "Less than -35", "Above 275", "At or below -18".
    m = re.match(rf"^(?:<|less than|below|under|at or below)\s*({NUM})\b", work,
                 flags=re.IGNORECASE)
    if m:
        residue(work[m.end():])
        return Parsed("range", None, None, to_float(m.group(1)), notes)
    m = re.match(rf"^(?:>|greater than|more than|above|at or above)\s*({NUM})\b",
                 work, flags=re.IGNORECASE)
    if m:
        residue(work[m.end():])
        return Parsed("range", None, to_float(m.group(1)), None, notes)

    # 6c. A plain figure.
    m = re.match(rf"^({NUM})\b", work)
    if m and to_float(m.group(1)) is not None:
        residue(work[m.end():])
        return Parsed("number", to_float(m.group(1)), None, None, notes)

    return Parsed("text", None, None, None, notes + [WORDS_NOTE])


# ---------------------------------------------------------------------------
# CAS registry numbers
# ---------------------------------------------------------------------------

CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")

MALFORMED_CAS = ("IMPOSSIBLE AS PRINTED: a CAS Registry Number is written "
                 "n(2-7)-nn-n, and {cas!r} is not. Numbers in this column have "
                 "been mangled by something that read them as dates. Stored "
                 "exactly as printed and NOT corrected.")
BAD_CHECKSUM_CAS = ("IMPOSSIBLE AS PRINTED: {cas!r} fails the CAS Registry's "
                    "own check-digit test, so it is not a valid registry "
                    "number. Stored exactly as printed and NOT corrected.")
SHARED_CAS = ("IMPOSSIBLE AS PRINTED: a CAS Registry Number names exactly one "
              "substance, and this file gives {cas!r} as the sole CAS of {n} "
              "cargoes with different formulae - {who}. At most one of them can "
              "be right and this file does not say which. Stored exactly as "
              "printed and NOT corrected.")


def cas_checksum_ok(cas: str) -> bool:
    """The CAS Registry's own check digit: sum(digit * position from the right)."""
    body, _, check = cas.rpartition("-")
    digits = body.replace("-", "")[::-1]
    return sum(int(d) * (i + 1) for i, d in enumerate(digits)) % 10 == int(check)


def split_cas(cell: str) -> List[str]:
    """The CAS numbers in a cell, dropping any label the guide puts in front."""
    out = []
    for piece in re.split(r"[;,]", cell):
        piece = re.sub(r"^\s*[\w.'\- ]*?[:=]\s*", "", piece).strip()
        if re.fullmatch(r"[\d-]+", piece):
            out.append(piece)
    return out


# ---------------------------------------------------------------------------
# Cross-column checks
# ---------------------------------------------------------------------------

MW_TOO_LIGHT = ("IMPOSSIBLE AS PRINTED: {mw} g/mol is lighter than a single "
                "hydrogen atom (1.008 g/mol), so no substance can have it. "
                "Stored exactly as printed and NOT corrected.")
RVD_MW_MISMATCH = (
    "CONTRADICTED BY THIS FILE: vapour density relative to air is molecular "
    "weight divided by air's {air} g/mol, so this row's two columns must agree "
    "- and across the rest of the file they do, to within 3%. Here the guide "
    "prints molecular weight {mw} and relative vapour density {rvd}, which "
    "implies {implied}. The two figures ARE consistent read the other way "
    "round ({rvd}/{air} = {swapped}), which is an observation about this file, "
    "not a correction: both cells are stored exactly as printed and neither "
    "has been changed.")
SHARED_UN = ("The guide gives UN number {un} to {n} cargoes in this file - "
             "{who}. A UN number covers a GROUP of substances, so this is "
             "normal rather than a contradiction; it is recorded so a lookup "
             "on the number is known not to resolve to one cargo.")

SYNONYM_NOTE = ("Listed under 'Synonyms' in the Tanker Safety Guide's entry "
                "for {gas!r}.")
AMBIGUOUS_SYNONYM_NOTE = (" This text is given to {n} cargoes in this source, "
                          "so a lookup on it cannot resolve to one of them.")


def read_table(path: Path) -> Tuple[List[dict], List[str]]:
    """Parse the CSV into cargo rows. Returns (rows, errors).

    Nothing is written unless errors is empty.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if len(grid) < 2:
        return [], [f"{path.name} has no data rows"]

    want = [ENTRY_NO_HEADER, NAME_HEADER] + [c.heading for c in COLUMNS]
    want.insert(want.index("Main Hazard") + 1, SYNONYMS_HEADER)
    width = len(want)
    got = [collapse(c) for c in (grid[0] + [""] * width)[:width]]
    if got != want or len(grid[0]) != width:
        for i, (g, w) in enumerate(zip(got, want)):
            if g != w:
                errors.append(f"column {i} is headed {g!r}, expected {w!r}")
        if len(grid[0]) != width:
            errors.append(f"the sheet has {len(grid[0])} columns, expected {width}")
        errors.append("is this the Tanker Safety Guide liquefied-gas properties sheet?")
        return [], errors
    index = {h: i for i, h in enumerate(want)}

    rows: List[dict] = []
    seen: Dict[str, int] = {}
    for line, raw in enumerate(grid[1:], start=2):
        cells = [collapse(c) for c in (list(raw) + [""] * width)[:width]]
        entry_no, name = cells[index[ENTRY_NO_HEADER]], cells[index[NAME_HEADER]]
        if not name and not any(cells):
            continue
        if not name:
            errors.append(f"row {line}: properties with no cargo name")
            continue

        # "Refrigerant Gases (Note 1)" is a name plus a cross-reference, not a
        # name. Strip the marker, the same way the reference markers are
        # stripped from product names in etl/gas/properties_of_gases.py.
        name_refs = [m.group(1) for m in NOTE_REF_RE.finditer(name)]
        gas_name = NOTE_REF_RE.sub(" ", name)
        gas_name = re.sub(r"\s+", " ", gas_name).strip()
        if gas_name in seen:
            errors.append(f"row {line}: duplicate cargo {gas_name!r} "
                          f"(also row {seen[gas_name]})")
            continue
        seen[gas_name] = line

        notes_cell = cells[index[NOTES_FIELD_HEADING]]
        available_notes = set(re.findall(r"(?<![\d.])(\d+)\s*\.", notes_cell))

        values = []
        for col in COLUMNS:
            text = cells[index[col.heading]]
            if not text:
                continue
            notes: List[str] = []
            if col.note:
                notes.append(col.note)

            if col.numeric:
                parsed = parse_measure(text, col.strip)
            else:
                refs = [m.group(1) for m in NOTE_REF_RE.finditer(text)]
                stripped = NOTE_REF_RE.sub(" ", text).strip()
                extra = []
                if refs and not stripped:
                    extra = [CROSS_REF_NOTE.format(refs=", ".join(refs),
                                                   field=NOTES_FIELD)]
                elif refs and col.field != NOTES_FIELD:
                    extra = [POINTER_NOTE.format(refs=", ".join(refs),
                                                 field=NOTES_FIELD)]
                parsed = Parsed("text", None, None, None, extra)

            # Every "(Note N)" in the row must resolve against the row's own
            # notes cell, or the cross-reference cannot be followed.
            if col.field != NOTES_FIELD:
                for group in NOTE_REF_RE.findall(text):
                    for ref in re.findall(r"\d+", group):
                        if ref not in available_notes:
                            errors.append(
                                f"row {line}: {gas_name!r} points at '(Note {ref})' "
                                f"in {col.label!r}, but its "
                                f"'Notes and Special Requirements' cell has no "
                                f"note {ref}")

            values.append({
                "field": col.field, "column": col.label, "value": text,
                "value_type": parsed.value_type,
                "normalized_value": parsed.normalized_value,
                "normalized_min": parsed.normalized_min,
                "normalized_max": parsed.normalized_max,
                "unit": col.unit if parsed.value_type != "text" else None,
                "notes": notes + list(parsed.notes),
                "flagged": False,
            })

        for ref in name_refs:
            for one in re.findall(r"\d+", ref):
                if one not in available_notes:
                    errors.append(
                        f"row {line}: the cargo name {name!r} points at note "
                        f"{one}, which its own notes cell does not have")

        synonyms = [s.strip() for s in cells[index[SYNONYMS_HEADER]].split(";")]
        synonyms = [s for s in synonyms if s and not NOTE_REF_RE.fullmatch(s)
                    and s.upper() not in {"N/A", "NA"}]

        rows.append({
            "gas_name": gas_name, "printed_name": name, "line": line,
            "entry_no": entry_no, "values": values, "synonyms": synonyms,
            "name_refs": name_refs,
        })

    if not rows:
        errors.append(f"{path.name} has a header but no cargo rows")
    return rows, errors


def flag(value: dict, note: str) -> None:
    """Mark one value as something this file contradicts, and say why."""
    value["flagged"] = True
    value["notes"].append(note)


def check_rows(rows: List[dict]) -> None:
    """Run the three cross-column checks; annotate and flag in place."""
    by_field = [{v["field"]: v for v in r["values"]} for r in rows]

    # 1 + 2. Molecular weight against relative vapour density, and against the
    #        mass of a hydrogen atom.
    for r, fields in zip(rows, by_field):
        mw = fields.get("molecular_weight_g_mol")
        rvd = fields.get("RELATIVE_VAPOUR_DENSITY")
        mw_n = mw["normalized_value"] if mw else None
        rvd_n = rvd["normalized_value"] if rvd else None
        if mw_n is not None and mw_n < 1.008:
            flag(mw, MW_TOO_LIGHT.format(mw=mw["value"]))
        if mw_n is None or rvd_n is None or mw_n <= 0 or rvd_n <= 0:
            continue
        implied = mw_n / AIR_MOLECULAR_WEIGHT
        if max(implied / rvd_n, rvd_n / implied) >= 2:
            note = RVD_MW_MISMATCH.format(
                air=AIR_MOLECULAR_WEIGHT, mw=mw["value"], rvd=rvd["value"],
                implied=round(implied, 4),
                swapped=round(rvd_n / AIR_MOLECULAR_WEIGHT, 4))
            flag(mw, note)
            flag(rvd, note)

    # 3. CAS registry numbers: form, check digit, and one number given as the
    #    sole CAS of two cargoes the file gives different formulae.
    sole: Dict[str, List[int]] = defaultdict(list)
    for i, fields in enumerate(by_field):
        cas = fields.get("cas_number")
        if not cas:
            continue
        numbers = split_cas(cas["value"])
        for number in numbers:
            if not CAS_RE.match(number):
                flag(cas, MALFORMED_CAS.format(cas=number))
            elif not cas_checksum_ok(number):
                flag(cas, BAD_CHECKSUM_CAS.format(cas=number))
        if len(numbers) == 1:
            sole[numbers[0]].append(i)

    for number, idxs in sole.items():
        formulae = {(by_field[i].get("molecular_formula") or {}).get("value")
                    for i in idxs}
        if len(idxs) < 2 or len(formulae) < 2:
            continue
        who = "; ".join(f"{rows[i]['gas_name']} ({(by_field[i].get('molecular_formula') or {}).get('value')})"
                        for i in idxs)
        for i in idxs:
            flag(by_field[i]["cas_number"],
                 SHARED_CAS.format(cas=number, n=len(idxs), who=who))

    # UN numbers: noted, never flagged - see the module docstring.
    shared_un: Dict[str, List[int]] = defaultdict(list)
    for i, fields in enumerate(by_field):
        un = fields.get("UN_NUMBER")
        if un and re.fullmatch(r"\d{4}", un["value"]):
            shared_un[un["value"]].append(i)
    for number, idxs in shared_un.items():
        if len(idxs) < 2:
            continue
        who = ", ".join(rows[i]["gas_name"] for i in idxs)
        for i in idxs:
            by_field[i]["UN_NUMBER"]["notes"].append(
                SHARED_UN.format(un=number, n=len(idxs), who=who))


def resolve_source(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {name!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/common/source.py"
        )
    return row[0]


def report(rows: List[dict]) -> None:
    per_field = Counter(v["field"] for r in rows for v in r["values"])
    total = sum(per_field.values())
    log.info("%d cargo(es), %d property value(s), %d synonym(s)",
             len(rows), total, sum(len(r["synonyms"]) for r in rows))
    for col in COLUMNS:
        got = per_field.get(col.field, 0)
        log.info("    %-32s -> %-38s %2d value(s)%s", col.label, col.field, got,
                 "" if got == len(rows) else f"  ({len(rows) - got} cargo(es) leave it empty)")

    kinds = Counter(v["value_type"] for r in rows for v in r["values"])
    log.info("value types: %s", ", ".join(f"{k}={n}" for k, n in sorted(kinds.items())))

    cross = [(r["gas_name"], v["column"]) for r in rows for v in r["values"]
             if any(n.startswith("The guide answers this column by pointing")
                    for n in v["notes"])]
    log.info("cells answered only by a cross-reference to the row's notes: %d",
             len(cross))
    for name, column in cross:
        log.info("    %-40s %s", name, column)

    renamed = [(r["printed_name"], r["gas_name"]) for r in rows if r["name_refs"]]
    for printed, clean in renamed:
        log.info("cargo name carries a note marker: %r stored as %r", printed, clean)

    shared_un = [(r["gas_name"], v["value"]) for r in rows for v in r["values"]
                 if v["field"] == "UN_NUMBER"
                 and any(n.startswith("The guide gives UN number") for n in v["notes"])]
    log.info("UN numbers this file gives to more than one cargo (normal, noted "
             "only): %d value(s)", len(shared_un))
    for name, un in shared_un:
        log.info("    %-40s %s", name, un)

    bad = [(r["gas_name"], v["column"], v["value"], v["notes"][-1])
           for r in rows for v in r["values"] if v["flagged"]]
    if bad:
        log.warning("%d value(s) this file contradicts - loaded as printed with "
                    "is_winning=false, flagged in notes, NOT corrected:", len(bad))
        for name, column, value, why in bad:
            log.warning("    %-40s %-26s %s", name, column, value)
            log.warning("        %s", why.split(". ")[0] + ".")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors = read_table(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1
    check_rows(rows)

    log.info("%s", path.name)
    report(rows)

    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    # A synonym text given to more than one cargo in this source cannot resolve
    # to one of them; the link is written on every cargo and each says so.
    synonym_owners: Dict[str, int] = Counter()
    for r in rows:
        for s in r["synonyms"]:
            synonym_owners[s.lower()] += 1

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            fields = list(dict.fromkeys(c.field for c in COLUMNS))
            added = ensure_field_definitions(cur, only=fields)
            log.info("field_definitions: %d created, %d already present",
                     added, len(fields) - added)

            cache: Dict[str, int] = {}
            created = written = linked = 0
            for r in rows:
                gas_id, is_new = upsert_gas(cur, source_id, r["gas_name"])
                created += is_new
                page_ref = f"entry {r['entry_no']}" if r["entry_no"] else None
                for v in r["values"]:
                    upsert_property(
                        cur, gas_id, source_id, v["field"],
                        value=v["value"],
                        normalized_value=v["normalized_value"],
                        normalized_min=v["normalized_min"],
                        normalized_max=v["normalized_max"],
                        unit=v["unit"], value_type=v["value_type"],
                        entered_by=ENTERED_BY, source_page_ref=page_ref,
                        notes=" ".join(v["notes"]) or None,
                        is_winning=not v["flagged"], conflict_flag=v["flagged"],
                    )
                    written += 1
                for text in r["synonyms"]:
                    ambiguous = synonym_owners[text.lower()] > 1
                    note = SYNONYM_NOTE.format(gas=r["gas_name"])
                    if ambiguous:
                        note += AMBIGUOUS_SYNONYM_NOTE.format(
                            n=synonym_owners[text.lower()])
                    synonym_id, _ = upsert_synonym(cur, source_id, text, cache)
                    link_synonym(cur, gas_id, synonym_id, source_id,
                                 relationship_type="synonym",
                                 ambiguity_flag=ambiguous, notes=note)
                    linked += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d (%d created this run) | "
                 "cargo_gas_property_values: %d | cargo_gas_synonym: %d",
                 len(rows), created, written, linked)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
