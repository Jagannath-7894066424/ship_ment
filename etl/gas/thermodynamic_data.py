#!/usr/bin/env python3
"""
Load one gas sheet of the thermodynamic data book into cargo_gas,
cargo_gas_property_values and cargo_gas_thermodynamic_property.

SOURCE
------
"Thermodynamic Data_Properties" (source.json, category 'gas'). The FIFTH gas
source. The workbook holds one sheet per gas - BUTADIENE 1_3, AMMONIA, ETHANE,
ETHYLENE, n_BUTANE, PROPANE, VCM, PROPYLENE - and this loader reads ONE of them
per run (--sheet), so each gas's load is attributable on its own.

WHAT ONE SHEET HOLDS
--------------------
Three blocks, and they are three different SHAPES of fact, which is why they
land in two tables:

  1. General properties - molecular weight, boiling point, inflammability
     limits, ignition temperature and energy, viscosity, condensing ratio. One
     value each, no temperature.
     -> cargo_gas_property_values

  2. PROPERTIES OF LIQUID AND SATURATED VAPOUR - one row per temperature,
     giving vapour pressure, specific volume, density and enthalpy for both
     phases, plus latent heat.
     -> cargo_gas_thermodynamic_property, property_type = SATURATED

  3. PROPERTIES OF SUPERHEATED VAPOUR - a grid of temperatures x pressures,
     each cell giving specific volume and enthalpy.
     -> cargo_gas_thermodynamic_property, property_type = SUPERHEATED

WHY THE SECOND TABLE EXISTS
---------------------------
cargo_gas_property_values is keyed UNIQUE (cargo_gas_id, source_id,
field_name): one value of a property per gas per source. A saturated vapour
density has one value per temperature - 101 of them for butadiene. Writing them
to that table would keep exactly one and silently discard the rest, and the one
kept would read as "the" vapour density with no temperature attached. See
prisma/migrations/20260903000000_cargo_gas_thermodynamic_property.

PRESSURE IS DEPENDENT IN ONE TABLE AND INDEPENDENT IN THE OTHER
----------------------------------------------------------------
In the saturated table the pressure column is the vapour pressure AT that
temperature - a reading, not a setting - so it is stored as
property_name = 'vapour_pressure' and the row's `pressure` column stays NULL.
In the superheated grid the source chose the pressures, so there `pressure` is
set. The database enforces this with a CHECK constraint.

THE SHEETS DISAGREE WITH EACH OTHER, SO NOTHING IS READ BY POSITION
--------------------------------------------------------------------
The workbook was clearly assembled by hand over time, and no two sheets are
laid out alike:

  * BUTADIENE 1_3 puts each general property's figure at the end of a dotted
    leader in the label cell; AMMONIA leaves the label cell trailing dots and
    puts the figure in a later column; ETHANE separates them with a colon.
  * Labels differ in wording, spacing and case - "Boiling Points (at 1 bar)",
    "Boiling Point (at 1 bar)", "Boiling Point (at 1bar)"; "Limits of",
    "Limits Of", "Limis of" Inflammability.
  * The saturated table's columns MOVE. Butadiene and ammonia print enthalpy
    (liquid) in column 7; ethane inserts blank spacer columns and prints it in
    column 9, with latent heat in column 10.
  * The superheated heading is spelt "PROPERIES" on one sheet.
  * Ammonia and ethane carry a 15 bar pressure column that butadiene does not;
    ethane also has viscosity lines butadiene lacks.
  * The temperature axis is one degree per row on butadiene and ammonia, but
    ethane runs in 5 degree steps to -50, then one degree, then stops at 32.3 -
    its critical point, where the two densities meet and latent heat is 0.
  * Not every sheet fills in every property: ethane prints "Limits Of
    Inflammability :" and "Ignition Temperature :" with nothing after them.

So the saturated columns are located by READING THE HEADER - group headings
paired with their vapour/liquid sub-headings - and never by index. A column
that moves is then a column that is still found, while a column that is missing
or renamed is an error rather than a silent mis-load. General properties are
matched on label ignoring case and spacing, against a list of the spellings the
workbook actually uses, so a spelling nobody has seen fails loudly.

THE ZEROS IN THE SUPERHEATED GRID ARE NOT ZEROS
------------------------------------------------
Cells at the cold end of a pressure column read 0 - at 5 bar butadiene shows 0
up to 40 C. A specific volume of 0 m3/kg is not a measurement; it is not a
quantity any substance can have. Each such cell sits BELOW the boiling point at
its pressure, which the sheet's own saturated table gives (5.0 bar falls between
the 4.92 bar printed at 44 C and 5.06 bar at 45 C), so the state does not exist.

They are NOT loaded - no row, rather than a row with 0 or a row with NULL. A row
asserts "this state was tabulated"; there is nothing to say about a state the
source does not tabulate, and a stored 0 would be read as a measurement by
anything that averages, plots or retrieves it. The loader checks that every
absent cell forms a contiguous run at the COLD end of its pressure column, which
is what "below the boiling line" has to look like, and fails if one turns up
anywhere else - that would be a hole in the data, not an absent state.

A zero in the SATURATED block is the opposite case and is loaded: ethane's
liquid enthalpy is 0 at -100 C because that is where its datum is set, and its
latent heat is 0 at the critical point because there is no phase change left.

MISPRINTS ARE FLAGGED, NEVER CORRECTED
--------------------------------------
The sheets contain real errors: ammonia's inflammability limits read
"160--->28.0 %" (a limit cannot exceed 100% by volume, and the range reads
back-to-front), its saturated axis prints -56 where the sequence needs -46, and
several readings break runs that physics does not allow to break - along the
saturation line vapour pressure and both vapour quantities can only rise with
temperature while liquid density and latent heat can only fall, and at a fixed
pressure a superheated vapour's volume and enthalpy can only rise.

Every such figure is stored exactly as printed, flagged in `notes` and reported
in the run log. Where three consecutive readings identify WHICH of them is the
outlier - a single value spiking above or below its neighbours - the flag goes
on that value rather than on the innocent reading after it. Nothing is
corrected: guessing the intended figure would put a number in the database that
the source never printed.

Saturated enthalpy of the VAPOUR is deliberately not checked, because it
genuinely flattens and turns over near the critical point (ethane's peaks at
145.6 and falls to 122.2); enthalpy of the LIQUID is checked, because it cannot.

'keal/kg'
---------
The sheets print the enthalpy and latent-heat unit as "keal/kg". No such unit
exists; it is an OCR of kcal/kg, which the magnitudes confirm (butadiene's
latent heat of 107 kcal/kg is 448 kJ/kg, the right order). The unit is stored as
kcal/kg and the affected values' notes record the sheet's spelling, per series,
only where that sheet actually misspells it. No number is converted.

ENTHALPY HAS A DATUM, AND IT IS NOT THE SAME ON EVERY SHEET
------------------------------------------------------------
Butadiene's Note 1 reads "Enthalpy based on zero at -273'C. in liquid phase",
ammonia's "based on 100 keal/kg at 0'C.", ethane's "zero at -100'C." Enthalpy is
only meaningful relative to a datum, so the sentence is carried onto every
enthalpy row of that sheet. Comparing two gases' enthalpies without it would be
comparing numbers measured from different origins.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id), on (cargo_gas_id, source_id, field_name), and
on the two PARTIAL unique indexes the migration creates. The whole sheet is
validated before anything is written; one transaction.

Usage:
    python3 etl/gas/thermodynamic_data.py
    python3 etl/gas/thermodynamic_data.py --dry-run
    python3 etl/gas/thermodynamic_data.py --sheet ETHANE --gas-name Ethane
    python3 etl/gas/thermodynamic_data.py --list-sheets
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import ensure_field_definitions, upsert_gas, upsert_property  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_thermodynamic")

SOURCE_NAME = "Thermodynamic Data_Properties"
ENTERED_BY = "thermodynamic_data.py"
DEFAULT_FILE = input_file("tdynamictables OK.xls")
DEFAULT_SHEET = "BUTADIENE 1_3"
DEFAULT_GAS_NAME = "Butadiene 1-3"

TITLE_PREFIX = "THERMODYNAMIC PROPERTIES OF"
# VCM drops the R from "PROPERTIES". Listed rather than normalised, for the
# same reason as the superheated spellings below.
SATURATED_HEADINGS = ("PROPERTIES OF LIQUID AND SATURATED VAPOUR",
                      "PROPETIES OF LIQUID AND SATURATED VAPOUR")
SATURATED_HEADING = SATURATED_HEADINGS[0]      # for messages
# Four spellings are in the workbook: butadiene prints "PROPERIES", propane
# "SUPERHEADTED", ethylene adds a full stop, the rest manage it. Matched as
# printed rather than normalised, so a FIFTH spelling fails loudly instead of
# being silently tolerated - the block would otherwise just be absent, and a
# sheet's whole superheated grid would go missing without a word.
SUPERHEATED_HEADINGS = ("PROPERTIES OF SUPERHEATED VAPOUR",
                        "PROPERIES OF SUPERHEATED VAPOUR",
                        "PROPERTIES OF SUPERHEATED VAPOUR.",
                        "PROPERTIES OF SUPERHEADTED VAPOUR",
                        "PROPERTIES OF SATURATED VAPOUR")

# The last of those does not say "superheated" at all: VCM heads its superheated
# grid "PROPERTIES OF SATURATED VAPOUR". It is nonetheless that grid, and the
# block itself proves it - six pressures the source chose, each with its own v
# and h column, a legend reading "P = absolute pressure", and unreachable cold
# cells left at 0. A saturated table has ONE pressure per temperature, the
# vapour pressure, and VCM's real saturated table is already above this block.
#
# Accepting the string is safe rather than lax: a sheet that used it for a
# genuine saturated table would have no "P = <n> bar" columns, and the loader
# already refuses a superheated block without them. The rows carry the note
# below so the mislabelling is visible to anyone who checks them against the
# sheet.
MISLABELLED_SUPERHEATED = "PROPERTIES OF SATURATED VAPOUR"
MISLABELLED_SUPERHEATED_NOTE = (
    f"THE SHEET MISLABELS THIS BLOCK: it heads the grid these rows come from "
    f"{MISLABELLED_SUPERHEATED!r}, but the block is the SUPERHEATED vapour grid "
    f"and is stored as one. It tabulates six absolute pressures the source "
    f"chose, each with a specific volume and an enthalpy column, and the "
    f"sheet's actual saturated table - one vapour pressure per temperature - is "
    f"printed above it. The heading is wrong; the figures are as printed.")

# The sheets write ° as an apostrophe throughout ("-4.7'C.").
DEGREE = "'"

# The unit the sheets print for enthalpy and latent heat, and what it means.
PRINTED_ENERGY_UNIT = "keal"
ENERGY_UNIT = "kcal/kg"
ENERGY_UNIT_NOTE = (
    f"The sheet prints this unit as {PRINTED_ENERGY_UNIT!r}/kg, which is not a "
    f"unit; it is an OCR of {ENERGY_UNIT}, as the magnitudes confirm. Stored as "
    f"{ENERGY_UNIT}. The figure itself is unchanged.")

# For a sheet that states no reference state for its enthalpies. Every other
# sheet prints a "Note 1. - Enthalpy based on ..." line, and that sentence is
# carried onto its enthalpy rows; ethylene prints none, so this says so instead.
# It is deliberately NOT an inferred datum: the figures could be extrapolated
# back to find where the liquid enthalpy would reach zero, but a datum invented
# by the loader would be indistinguishable in the database from one the source
# actually stated.
NO_DATUM_NOTE = (
    "DATUM NOT STATED: enthalpy is only meaningful relative to a reference "
    "state, and this sheet prints no 'Enthalpy based on ...' note - unlike "
    "every other gas sheet in the workbook, which name theirs (zero at -273°C "
    "in the liquid phase, zero at -100°C, 100 kcal/kg at 0°C). The figure is "
    "stored exactly as printed, but its origin is unknown, so it CANNOT be "
    "compared with another gas's enthalpy or converted to one measured from a "
    "different datum. Differences within this sheet - latent heat, or the "
    "enthalpy change between two of its own temperatures - remain valid, "
    "because the unknown datum cancels out of a difference.")


# ---------------------------------------------------------------------------
# General properties
# ---------------------------------------------------------------------------
class General:
    """One general property: where to find it and what it becomes.

    `labels` holds every spelling the workbook uses for the line, matched
    ignoring case and spacing. They are listed rather than fuzzy-matched so
    that a spelling nobody has seen fails loudly and gets read by a human
    before its value is trusted.

    `required` is for the two properties every sheet carries. The rest are
    per-sheet: ethane prints "Ignition Temperature :" and stops, ammonia gives
    a gas viscosity where butadiene gives none. A label with nothing after it
    is the source declining to state the property - reported, not an error, and
    no row is written for it.
    """

    def __init__(self, labels, field: str, unit: Optional[str],
                 note: Optional[str] = None, required: bool = False):
        self.labels = (labels,) if isinstance(labels, str) else tuple(labels)
        self.field = field
        self.unit = unit
        self.note = note
        self.required = required

    @property
    def label(self) -> str:
        return self.labels[0]


GENERAL = [
    General("Molecular Weight", "molecular_weight_g_mol", "g/mol",
            "The sheet gives no unit for this figure. It is a molecular weight, "
            "whose canonical unit here is g/mol (identical in magnitude to the "
            "kg/kmol other gas sources print). The number is unchanged.",
            required=True),
    # Labels are compared with spaces removed, so "Boiling Point (at 1bar)"
    # already covers ethylene's "Boiling Point(at 1 bar)" and n-butane's
    # "Boiling Point(at 1bar)"; only the plural is a genuinely distinct spelling.
    General(("Boiling Point (at 1 bar)", "Boiling Points (at 1 bar)",
             "Boiling Point (at 1bar)"),
            "boiling_point_c", "°C",
            "At 1 bar, per the sheet's own heading.", required=True),
    General(("Ignition Temperature", "Ignition Temparature"),
            "auto_ignition_temperature", "°C", None),
    # Only ETHYLENE states this, and it misspells the label. It is worth having
    # as a property in its own right because it is the one general figure the
    # saturated table can be checked against: that table must stop AT the
    # critical temperature, where the two densities meet and latent heat is 0.
    General(("Critical Temperature", "Critical Temperayure"),
            "critical_temperature_c", "°C",
            "The temperature above which the gas cannot be liquefied by "
            "pressure alone, so it is also where the sheet's saturated table "
            "has to end."),
    # Only VCM states this. It is not a substitute for the ignition temperature
    # above: flash point is the temperature at which the liquid gives off enough
    # vapour to ignite in the presence of a flame, ignition temperature the one
    # at which vapour ignites with no flame at all.
    General("Flash Point", "flash_point_c", "°C"),
    General("Ignition Energy", "minimum_ignition_energy_mj", "mJ",
            "'millijoule(s)' is the unit spelled out, not a conversion. Where "
            "the sheet qualifies the figure ('approx.') the qualifier is part "
            "of the source's claim and is kept in `value`."),
    General(("Viscosity in gaseous phase", "Viscosity in gasseous phase"),
            "gas_viscosity_cp", "cP"),
    General("Viscosity in liquid phase", "liquid_viscosity_cp", "cP"),
    General("Condensing ratio", "condensing_ratio_dm3_per_m3", "dm³/m³"),
]

# One printed line holding two properties: "2% --->11.5%".
INFLAMMABILITY_LABELS = ("Limits of Inflammability", "Limis of Inflammability",
                         "Limits Of Inflmmability",
                         # VCM names the same pair of figures the other way
                         # round - the lower and upper EXPLOSIVE limit. Same
                         # quantity, same units, same two fields.
                         "Explosive Limits")


# ---------------------------------------------------------------------------
# Saturated block: what the header must yield
# ---------------------------------------------------------------------------
# keywords in the group heading -> (property_name, split by phase?)
# Order matters: "Vapour Pressure" contains "vapour", so the pressure keyword
# has to be tested before anything else could claim that cell. Each group lists
# every wording the workbook uses - ETHYLENE abbreviates the pressure heading to
# "Vap.press.", which the spelt-out keyword does not match.
GROUPS = [
    (("vapour press", "vap.press"), "vapour_pressure", False),
    (("specific vol",),             "specific_volume", True),
    (("density",),                  "density",         True),
    (("enthalpy",),                 "enthalpy",        True),
    (("latent",),                   "latent_heat",     False),
]

UNITS = {"vapour_pressure": "bar", "specific_volume": "m³/kg",
         "density": "kg/m³", "enthalpy": ENERGY_UNIT,
         "latent_heat": ENERGY_UNIT}

# Exactly these eight series must come out of the header, no more and no fewer.
SERIES = [("vapour_pressure", "NONE"), ("specific_volume", "VAPOUR"),
          ("specific_volume", "LIQUID"), ("density", "VAPOUR"),
          ("density", "LIQUID"), ("enthalpy", "VAPOUR"),
          ("enthalpy", "LIQUID"), ("latent_heat", "NONE")]

# Along the saturation line these seven series can only run one way, as a
# matter of physics rather than of this data: heat a liquid under its own
# vapour and the pressure rises, the vapour gets denser, the liquid gets less
# dense, and the latent heat falls away towards the critical point. A run that
# turns back on itself is a misprint, so the loader says so. "up"/"down" are
# non-strict - the tables round, and equal neighbours are common.
#
# Enthalpy of the VAPOUR is deliberately absent: it can genuinely flatten and
# turn over near the critical point, so a check would cry wolf. Enthalpy of the
# liquid cannot - heat capacity is positive - so it is checked.
SATURATED_MONOTONIC = [
    ("vapour_pressure", "NONE",   "up"),
    ("specific_volume", "VAPOUR", "down"),
    ("specific_volume", "LIQUID", "up"),
    ("density",         "VAPOUR", "up"),
    ("density",         "LIQUID", "down"),
    ("enthalpy",        "LIQUID", "up"),
    ("latent_heat",     "NONE",   "down"),
]

# Superheated block: the two properties each pressure column carries, in order.
SUPERHEATED_PAIR = [("specific_volume", "m³/kg"), ("enthalpy", ENERGY_UNIT)]


def cell(value) -> str:
    """Trim a cell and collapse its whitespace; NaN becomes ''."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def number(text: str) -> Optional[float]:
    """The LEADING number in a table cell, or None.

    Anchored on purpose: a cell of a data table must BE a number, so text in
    front of one means the column is not what the header says it is.
    """
    m = re.match(r"^\s*(-?(?:\d+(?:\.\d+)?|\.\d+))", text.replace(DEGREE, ""))
    return float(m.group(1)) if m else None


def stated_number(text: str) -> Optional[float]:
    """The first number ANYWHERE in a stated value, or None.

    For the general-properties lines only, where a sheet writes prose around
    the figure ("approx. 1 millijoule"). The wording is kept in `value`; this
    just finds the number to normalize.

    The leading-dot alternative is not decoration: the AMMONIA sheet writes a
    gas viscosity as ".00933 Centipoises", and a pattern that insists on a digit
    before the point reads that as 933 - out by five orders of magnitude, in a
    figure nothing downstream could sanity-check.
    """
    m = re.search(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text.replace(DEGREE, ""))
    return float(m.group(0)) if m else None


def norm(text: str) -> str:
    """Lowercase and drop spaces - for comparing labels across sheets."""
    return re.sub(r"\s+", "", text.lower())


def norm_header(text: str) -> str:
    """A header cell reduced to what it identifies.

    The sheets differ in case ("Specific volume" / "Specific Vol.") and in the
    OCR of kcal while naming the same column. Those are normalised away; which
    column is which is not.
    """
    return re.sub(r"\s+", " ", text.lower()).strip()


def near_match(a: str, b: str, allowed: int = 2) -> Tuple[bool, int]:
    """(close enough, edit distance) on letters and digits only.

    Used only to compare a sheet's printed gas name with the name it is being
    loaded under. The workbook misspells its own title ("AMONIA"), so an exact
    comparison would block the load; a bounded edit distance still catches the
    mistake this guard exists for - loading the PROPANE sheet as ammonia.
    """
    x = re.sub(r"[^a-z0-9]", "", a.lower())
    y = re.sub(r"[^a-z0-9]", "", b.lower())
    prev = list(range(len(y) + 1))
    for i, cx in enumerate(x, 1):
        cur = [i]
        for j, cy in enumerate(y, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cx != cy)))
        prev = cur
    return prev[-1] <= allowed, prev[-1]


def after_label(text: str, label: str) -> Optional[str]:
    """The raw remainder of `text` after `label`, matched loosely.

    Case and spacing are ignored, because the sheets differ in both, but the
    remainder returned is the RAW text - the value must never be read from a
    normalised copy.
    """
    want = norm(label)
    seen = ""
    for i, ch in enumerate(text):
        if not ch.isspace():
            seen += ch.lower()
        if seen == want:
            return text[i + 1:]
        if not want.startswith(seen):
            return None
    return None


def split_label(text: str, label: str) -> Tuple[str, str]:
    """Split a property line into (the heading as printed, the rest of the cell).

    The heading keeps the label's own parenthesised qualifier, because that is
    part of what the sheet is claiming - "(dm3/1m3gas)" says what the condensing
    ratio is a ratio OF, and "(at -100'C)" is the temperature a viscosity was
    measured at. What comes after it is the value.

    Needed because ETHYLENE prints label and value in ONE cell
    ("Molecular Weight : 28.05"); without the split every note quoting the
    sheet's own wording would quote the figure back with it.
    """
    rest = after_label(text, label)
    if rest is None:
        return text.rstrip(" :.").strip(), ""
    qualifier = re.match(r"^\s*\([^)]*\)", rest)       # "(at 0'C.)"
    if qualifier:
        rest = rest[qualifier.end():]
    head = text[:len(text) - len(rest)]
    return head.rstrip(" :.").strip(), rest


def row_value(row: List[str], label: str, col: int = 0) -> str:
    """The value of a general-property line, wherever the sheet put it.

    Four shapes, one fact: butadiene appends the figure to the label cell
    ("Molecular Weight .. .. 54.1"), ammonia leaves that cell trailing dots and
    puts the figure in a later column, ethane separates them with a colon, and
    ethylene does the same but from column 9 of a worksheet.

    What follows the label counts as the value only if it actually carries a
    number once the label's own parenthesised qualifier and separator are
    removed. Without that test "Condensing ratio (dm3/1m3gas)" reads as the
    value 3 and "Viscosity In Gaseous Phase(at 0'C)" as 0 - both from digits
    inside the qualifier, and both wrong in a way nothing downstream could
    detect.

    The scan for a value in a later column starts at the label's OWN column, not
    at column 0. On ETHYLENE the left of the sheet is a cargo cool-down
    calculation, so scanning the whole row would take "Condensing ratio"'s value
    from the 9.1 kcal/kg of that worksheet's enthalpy working.
    """
    _, inline = split_label(row[col], label)
    inline = re.sub(r"^[.:\s]+", "", inline)           # drop leader or colon
    # Trim a leader remnant (".." or longer) but never a single trailing stop:
    # the sheets write degrees Celsius as "-4.7'C.", where that period is the
    # abbreviation's own and part of what the source printed.
    inline = re.sub(r"\s*\.{2,}\s*$", "", inline).strip()
    if inline and stated_number(inline) is not None:
        return inline
    for c in row[col + 1:]:
        if c and not re.fullmatch(r"[.:\s]+", c) and stated_number(c) is not None:
            # Propylene splits some lines so the separator lands at the head of
            # the VALUE cell (": 0.00846 Centiposes"). A leading colon is
            # punctuation, not part of the figure. A leading DOT is not touched:
            # ammonia writes a viscosity as ".00933 Centipoises", and trimming
            # that would read as 933.
            return re.sub(r"^\s*:\s*", "", c).strip()
    return ""


def find_row(grid: List[List[str]], predicate) -> Optional[int]:
    for i, row in enumerate(grid):
        if predicate(row):
            return i
    return None


def locate_label(grid: List[List[str]],
                 labels) -> Optional[Tuple[int, int, str]]:
    """First cell starting with any of `labels`, as (row, column, label).

    Searched across the whole row rather than in column 0 alone. Butadiene,
    ammonia and ethane print their property lines down the left edge, but
    ETHYLENE's sheet is a cargo cool-down worksheet with the gas's data pasted
    around it, and its property lines start in column 9.

    Rows in order, then left to right within a row, so a sheet that does print
    its labels in column 0 still finds them exactly where it did before.
    """
    for label in labels:
        want = norm(label)
        for i, row in enumerate(grid):
            for col, text in enumerate(row):
                if text and norm(text).startswith(want):
                    return i, col, label
    return None


def read_sheet(path: Path, sheet: str, gas_name: str) -> Tuple[dict, List[str]]:
    """Parse one gas sheet. Returns (parsed, errors); nothing is written unless
    errors is empty."""
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    grid = [[cell(v) for v in row] for row in df.values.tolist()]
    errors: List[str] = []

    # --- the title tells us which gas the sheet is about ---------------------
    # Not always in the first column: AMMONIA indents its title by one.
    title = None
    for row in grid:
        for c in row:
            if c.startswith(TITLE_PREFIX):
                title = c
                break
        if title:
            break
    if title is None:
        return {}, [f"sheet {sheet!r} has no {TITLE_PREFIX!r} title row - "
                    f"is this a gas sheet of the thermodynamic workbook?"]
    printed_gas = title[len(TITLE_PREFIX):].strip()

    # Guard against loading one gas's sheet under another's name. Bounded, not
    # exact: the AMMONIA sheet titles itself "AMONIA", and refusing to load a
    # sheet over the source's own typo would be a guard working against the job.
    # A real mix-up is nowhere near this close.
    close, distance = near_match(printed_gas, gas_name)
    if not close:
        errors.append(f"sheet {sheet!r} is titled {printed_gas!r} but the gas "
                      f"name given is {gas_name!r} ({distance} edits apart); "
                      f"pass --gas-name to match the sheet")

    # --- Note 1: the enthalpy datum ------------------------------------------
    # Matched on "Enthalpy based on", not on the datum itself: the sheets use
    # DIFFERENT reference states - zero at -273°C in the liquid phase for
    # butadiene, 100 kcal/kg at 0°C for ammonia, zero at -100°C for ethane -
    # which is precisely why the sentence has to travel with every enthalpy row
    # instead of being assumed.
    datum_i = find_row(grid, lambda r: any("Enthalpy based on" in c for c in r))
    if datum_i is None:
        # ETHYLENE states no datum anywhere - not on its own sheet and not on
        # the workbook's Details sheet. Its enthalpies are still real readings
        # and are loaded, but every one of them carries NO_DATUM_NOTE saying the
        # origin is unknown, because an enthalpy whose datum is unknown cannot
        # be compared with another gas's. Latent heat is unaffected: it is a
        # DIFFERENCE of two enthalpies, so the datum cancels out of it.
        enthalpy_note = ""
    else:
        raw = next(c for c in grid[datum_i] if "Enthalpy based on" in c)
        # The note is numbered three different ways across the workbook:
        # "Note 1. - " (butadiene), "NOTE 1. -" (VCM), "Note 1: " (propylene).
        enthalpy_note = re.sub(r"^Note\s*\d+\s*[.:]\s*-?\s*", "", raw,
                               flags=re.IGNORECASE).strip()

    general, declined, unclaimed = read_general(grid, errors)
    saturated, series = read_saturated(grid, errors)
    superheated = read_superheated(grid, errors)

    return ({"gas_name": gas_name, "printed_gas": printed_gas,
             "misspelt": distance > 0, "sheet": sheet, "general": general,
             "declined": declined, "unclaimed": unclaimed,
             "saturated": saturated, "series": series,
             "superheated": superheated, "enthalpy_note": enthalpy_note},
            errors)


def unclaimed_property_lines(grid: List[List[str]],
                             claimed: List[Tuple[int, int]]) -> List[str]:
    """Lines that state a property which no spec in GENERAL claimed.

    The loader can only find a property it knows a spelling for, so a sheet that
    invents a new one loses it in SILENCE - the property is simply not in the
    output, and nothing says so. n-BUTANE prints "Viscosity in gasseous
    phase(at 20'C): 0.0071 centipose", and 'gasseous' matched nothing.

    Required properties already fail loudly when missing. This covers the rest:
    anything shaped like a stated property and left unclaimed is reported, so an
    unknown spelling reaches a human instead of vanishing. It reports, and does
    not error - a line it cannot interpret is not proof of a problem.

    Scoped to the column this sheet prints its properties in, which keeps the
    data tables out of the scan and, on ETHYLENE, the cargo cool-down worksheet
    that shares the sheet. Only the "label : value" form is recognised, not the
    dotted-leader form butadiene and ammonia use for some lines.
    """
    if not claimed:
        return []
    col = min(c for _, c in claimed)
    done = {r for r, _ in claimed}
    out: List[str] = []
    for i, row in enumerate(grid):
        if i in done or col >= len(row):
            continue
        m = re.match(r"^([^:]{3,60}?)\s*:\s*(.+)$", row[col])
        if not m:
            continue
        # A label is words, not an expression: this rejects the superheated
        # legend ("v-specific volume, m3/kg: h- enthalpy, keal/kg:") without
        # rejecting "Viscosity in gasseous phase(at 20'C)".
        label = re.sub(r"\([^)]*\)", "", m.group(1)).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", label):
            continue
        if stated_number(m.group(2)) is None:
            continue
        out.append(row[col])
    return out


def read_general(grid: List[List[str]],
                 errors: List[str]) -> Tuple[List[dict], List[str], List[str]]:
    """The property lines above the tables, plus the ones the sheet leaves blank."""
    out: List[dict] = []
    declined: List[str] = []
    claimed: List[Tuple[int, int]] = []

    for spec in GENERAL:
        found = locate_label(grid, spec.labels)
        if found is None:
            if spec.required:
                errors.append(f"general property {spec.label!r} is not on the "
                              f"sheet under any spelling this loader knows "
                              f"({', '.join(map(repr, spec.labels))})")
            continue
        i, col, label = found
        claimed.append((i, col))
        row = grid[i]
        raw = row_value(row, label, col)
        heading, _ = split_label(row[col], label)
        if not raw:
            # The label is printed with nothing after it: the source is saying
            # it does not state this property, which is not the same as the
            # loader failing to find it.
            declined.append(heading)
            if spec.required:
                errors.append(f"general property {heading!r} has no value "
                              f"beside it, and every sheet states this one")
            continue

        note = spec.note or ""
        if spec.field == "condensing_ratio_dm3_per_m3":
            explanation = ""
            for c in row[col + 1:]:
                if c.startswith("(") and len(c) > 12:
                    explanation = c.strip("()")
            note = f"The sheet's own heading is {heading!r}."
            if explanation:
                note += f" It explains the figure as: {explanation}."
        if spec.field.endswith("_viscosity_cp"):
            # The temperature is part of the label, and a viscosity without one
            # is not a measurement.
            note = (f"The sheet's own heading is {heading!r}, so this is the "
                    f"temperature the figure was measured at. 'Centipoises' is "
                    f"the unit spelled out (cP), not a conversion.")
            if not re.search(r"\d", heading.split("(")[-1]):
                errors.append(f"{heading!r} states no temperature; a viscosity "
                              f"without one cannot be stored")

        value = stated_number(raw)
        if value is None:
            errors.append(f"general property {heading!r} reads {raw!r}, "
                          f"which carries no number")
            continue
        # `label` is the spelling THIS sheet uses, so the run log names what a
        # reader will find in the workbook.
        out.append({"field": spec.field, "label": heading, "value": raw,
                    "normalized": value, "unit": spec.unit,
                    "notes": [n for n in [note] if n]})

    limits, limits_declined, limits_claimed = read_inflammability(grid, errors)
    out.extend(limits)
    declined.extend(limits_declined)
    claimed.extend(limits_claimed)
    return out, declined, unclaimed_property_lines(grid, claimed)


def read_inflammability(grid: List[List[str]], errors: List[str]
                        ) -> Tuple[List[dict], List[str], List[Tuple[int, int]]]:
    """One printed line, two properties: '2% --->11.5%', '160--->28.0 %.'.

    Split on the sheet's own arrow rather than by hunting for percent signs:
    AMMONIA marks only the upper figure with a % ("160--->28.0 %."), so
    counting '%' would find one limit where the line states two.
    """
    found = locate_label(grid, INFLAMMABILITY_LABELS)
    if found is None:
        errors.append(f"the inflammability-limits line is not on the sheet "
                      f"under any spelling this loader knows "
                      f"({', '.join(map(repr, INFLAMMABILITY_LABELS))})")
        return [], [], []
    i, col, label = found
    heading, _ = split_label(grid[i][col], label)

    raw = row_value(grid[i], label, col)
    if not raw:
        return [], [heading], [(i, col)]   # printed, but left blank - e.g. ethane

    halves = re.split(r"-{2,}>|→", raw)
    if len(halves) != 2:
        errors.append(f"{heading!r} reads {raw!r}; expected a lower and an "
                      f"upper limit separated by the sheet's '--->' arrow")
        return [], [], [(i, col)]

    out: List[dict] = []
    limits: List[float] = []
    for field, half, which in (("lower_inflammability_limit", halves[0], "lower"),
                               ("upper_inflammability_limit", halves[1], "upper")):
        value = stated_number(half)
        if value is None:
            errors.append(f"{heading!r}: the {which} half of {raw!r} carries no "
                          f"number")
            return [], [], [(i, col)]
        limits.append(value)
        printed = half.strip().rstrip(".").strip()
        out.append({
            "field": field, "label": f"{heading} ({which})",
            "value": printed or half.strip(), "normalized": value,
            "unit": "% vol",
            "notes": [f"The sheet prints both limits on one line as {raw!r}; "
                      f"this is the {which} of the two. The percentage is by "
                      f"volume in air, the convention for an inflammability "
                      f"range. This is the same quantity other sources record "
                      f"as {'lel' if which == 'lower' else 'uel'} (the "
                      f"{which} explosive limit)."]})

    # Two independent proofs that a limit is misprinted, both from the line
    # itself. Flagged, never corrected.
    lower, upper = limits
    for row in out:
        if row["normalized"] > 100:
            row["notes"].append(
                f"IMPOSSIBLE AS PRINTED: {row['normalized']:g}% by volume "
                f"cannot be an inflammability limit - a mixture cannot be more "
                f"than 100% vapour. Stored exactly as the sheet prints it and "
                f"NOT corrected; guessing the intended figure would be "
                f"inventing data.")
    if lower >= upper:
        for row in out:
            row["notes"].append(
                f"INCONSISTENT IN THE SOURCE: the sheet gives a lower limit of "
                f"{lower:g}% and an upper limit of {upper:g}%, so the range "
                f"reads back-to-front. One of the two figures is misprinted; "
                f"both are stored as printed.")
    return out, [], [(i, col)]


def map_saturated_columns(head: List[str], sub: List[str], errors: List[str],
                          temp_col: int = 0) -> Dict[Tuple[str, str], dict]:
    """Locate each (property, phase) series by READING the two header rows.

    Driven off the SUB-header row, not the group headings. The group heading is
    not reliably above its own data: ethane centres "Specific Vol." over column
    3 while its vapour figures are in column 2, so spans measured from the
    heading would hand column 2 to the group before it. The "Vapour"/"Liquid"
    sub-headings, on the other hand, sit exactly over their columns on every
    sheet - they are what the typist aligned.

    So: take the phase columns in order, pair them off, and give pair i to the
    i-th phased group named in the heading row. The heading row still decides
    WHICH property each pair is and in what order, so a renamed or reordered
    group is an error rather than a silent mis-load; it just does not decide
    where the columns are. Each pair is then checked to fall between its own
    group heading and the next, which is what catches a header that really is
    scrambled rather than merely centred.
    """
    # Only the columns to the RIGHT of the temperature axis are the table's.
    # On ETHYLENE everything left of it belongs to a cargo cool-down worksheet
    # that happens to share the rows. For the sheets whose axis is column 0 this
    # is the same test as before.
    groups: List[Tuple[int, str, bool, str]] = []
    for col, text in enumerate(head):
        if col <= temp_col or not text:
            continue
        low = norm_header(text)
        for keywords, prop, phased in GROUPS:
            if any(k in low for k in keywords):
                groups.append((col, prop, phased, text))
                break

    phased = [g for g in groups if g[2]]
    unphased = [g for g in groups if not g[2]]

    # The phase columns, in the order the sheet prints them.
    marks: List[Tuple[int, str]] = []
    for col, text in enumerate(sub):
        if col <= temp_col:
            continue
        low = norm_header(text)
        if "vapour" in low:
            marks.append((col, "VAPOUR"))
        elif "liquid" in low:
            marks.append((col, "LIQUID"))

    if len(marks) != 2 * len(phased):
        errors.append(
            f"the saturated header has {len(marks)} vapour/liquid column(s) but "
            f"{len(phased)} property group(s) that need a pair each "
            f"({', '.join(g[1] for g in phased)}). Header rows read: "
            f"{head!r} / {sub!r}")
        return {}

    found: Dict[Tuple[str, str], dict] = {}

    def entry(col: int, blob: str, prop: str) -> dict:
        # The keal/kcal note is attached per series, from THIS sheet's own
        # header text: ethane spells the latent-heat unit correctly ("Kcal/Kg")
        # while misspelling the enthalpy one in the same row.
        return {"col": col, "unit": UNITS[prop],
                "printed_unit": PRINTED_ENERGY_UNIT in blob.lower()}

    for i, (gcol, prop, _, text) in enumerate(phased):
        pair = marks[2 * i:2 * i + 2]
        if [p for _, p in pair] != ["VAPOUR", "LIQUID"]:
            errors.append(f"the {prop} group's columns are labelled "
                          f"{[p for _, p in pair]!r}, expected a vapour column "
                          f"then a liquid one")
            continue
        # The pair must sit in this group's stretch of the row: at or after the
        # column before its heading, and before the next group's heading.
        after = [g[0] for g in groups if g[0] > gcol]
        limit = after[0] if after else len(head)
        if not all(gcol - 1 <= c < limit + 1 for c, _ in pair):
            errors.append(
                f"the {prop} group is headed at column {gcol} but its "
                f"vapour/liquid columns are {[c for c, _ in pair]}, outside the "
                f"stretch of the row that heading covers - the header rows do "
                f"not line up with the data")
            continue
        for col, phase in pair:
            found[(prop, phase)] = entry(col, text + " " + sub[col], prop)

    for gcol, prop, _, text in unphased:
        blob = text + " " + (sub[gcol] if gcol < len(sub) else "")
        found[(prop, "NONE")] = entry(gcol, blob, prop)

    missing = [s for s in SERIES if s not in found]
    extra = [s for s in found if s not in SERIES]
    if missing or extra:
        errors.append(
            f"the saturated header does not describe the eight expected series. "
            f"Missing: {missing or 'none'}. Unexpected: {extra or 'none'}. "
            f"Header rows read: {head!r} / {sub!r}")
    return found


def find_saturated_header(grid: List[List[str]],
                          errors: List[str]) -> Optional[Tuple[int, int]]:
    """(header row, temperature column) of the liquid/saturated-vapour table.

    Butadiene, ammonia and ethane print a PROPERTIES OF LIQUID AND SATURATED
    VAPOUR banner, and the header is the first 'Temp.' row below it. ETHYLENE
    prints no banner at all - its table is pasted into the top right of a cargo
    cool-down worksheet, with the header sharing row 0 with the sheet title - so
    when the banner is absent the block is identified by the only thing that is
    always true of it: a 'Temp.' cell with the saturated property groups named
    to its right.

    The superheated table also has a 'Temp.' cell, but heads its columns
    'P = <n> bar' rather than with property names, so it cannot match this test.
    """
    start = find_row(grid, lambda r: any(c in SATURATED_HEADINGS for c in r))
    for i in range(0 if start is None else start, len(grid)):
        for col, text in enumerate(grid[i]):
            if norm(text) != "temp.":
                continue
            right = " ".join(norm_header(c) for c in grid[i][col + 1:])
            if any(k in right for keywords, _, _ in GROUPS for k in keywords):
                return i, col
    if start is None:
        errors.append(f"no saturated-vapour table on the sheet: there is no "
                      f"{SATURATED_HEADING!r} heading, and no 'Temp.' header "
                      f"cell with saturated property groups beside it either")
    else:
        errors.append(f"the {SATURATED_HEADING!r} block has no 'Temp.' header row")
    return None


def read_saturated(grid: List[List[str]],
                   errors: List[str]) -> Tuple[List[dict], Dict]:
    """The liquid / saturated-vapour table."""
    found = find_saturated_header(grid, errors)
    if found is None:
        return [], {}
    h1, temp_col = found
    head, sub = grid[h1], grid[h1 + 1]
    unit_cell = sub[temp_col] if temp_col < len(sub) else ""
    if "c" not in norm_header(unit_cell):
        errors.append(f"the saturated temperature column is headed "
                      f"{unit_cell!r}, which does not name a temperature unit")
    columns = map_saturated_columns(head, sub, errors, temp_col)
    if errors:
        return [], columns

    rows: List[dict] = []
    seen: set = set()
    started = False
    for i in range(h1 + 2, len(grid)):
        row = grid[i]
        if not any(row):
            if started:
                break
            continue
        temp = number(row[temp_col]) if temp_col < len(row) else None
        if temp is None:
            if started:
                break
            continue
        started = True
        if temp in seen:
            errors.append(f"saturated row {i + 1}: temperature {temp} appears twice")
            continue
        seen.add(temp)
        values = []
        for prop, phase in SERIES:
            spec = columns[(prop, phase)]
            raw = row[spec["col"]] if spec["col"] < len(row) else ""
            value = number(raw)
            if value is None:
                errors.append(
                    f"saturated row {i + 1} ({temp:g}°C): {prop}"
                    f"{'/' + phase.lower() if phase != 'NONE' else ''} reads "
                    f"{raw!r} in column {spec['col']}, which is not a number")
                continue
            values.append({"property_name": prop, "phase": phase,
                           "unit": spec["unit"],
                           "printed_unit": spec["printed_unit"],
                           "value": value, "raw": raw})
        rows.append({"temperature": temp, "values": values})

    if not rows:
        errors.append(f"the {SATURATED_HEADING!r} block has no data rows")
        return rows, columns

    flag_axis(rows)
    for prop, phase, direction in SATURATED_MONOTONIC:
        series = [(r["temperature"], v) for r in rows for v in r["values"]
                  if v["property_name"] == prop and v["phase"] == phase]
        flag_monotonic(series, direction, f"{prop.replace('_', ' ')}"
                       f"{' of the ' + phase.lower() if phase != 'NONE' else ''}",
                       "along the saturation line", "with temperature", "°C")
    return rows, columns


def flag_axis(rows: List[dict]) -> None:
    """Flag a temperature that goes backwards.

    The step is NOT assumed: butadiene and ammonia advance one degree per row,
    ethane runs in fives to -50, then ones, and finishes at its critical point
    32.3. What no sheet does is descend, so a temperature lower than the row
    above it is a misprinted label rather than a different sampling.
    """
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if cur["temperature"] > prev["temperature"]:
            continue
        nxt = rows[i + 1]["temperature"] if i + 1 < len(rows) else None
        correction = ""
        if nxt is not None and round(nxt - prev["temperature"], 6) == 2.0:
            correction = (f" Its neighbours are {prev['temperature']:g}°C and "
                          f"{nxt:g}°C, so in sequence this row should read "
                          f"{prev['temperature'] + 1:g}°C.")
        cur["anomaly"] = (
            f"MISPRINTED TEMPERATURE AXIS: the sheet labels this row "
            f"{cur['temperature']:g}°C, below the {prev['temperature']:g}°C of "
            f"the row above it, and the table ascends everywhere else."
            f"{correction} The row is stored at the temperature PRINTED, not a "
            f"corrected one: temperature is this row's identity, and rewriting "
            f"identity on an inference is not this loader's call.")


def flag_monotonic(series: List[Tuple[float, dict]], direction: str,
                   what: str, where: str, against: str, unit: str) -> None:
    """Flag readings that break a run physics does not allow to break.

    Where three consecutive readings show WHICH one is the outlier - a single
    value spiking away from its neighbours and back - the flag goes on that
    value rather than on the innocent reading after it. Ammonia prints a vapour
    density of 5.904, 5.094, 6.289 (the middle one is low) and ethane a liquid
    enthalpy of 52.8, 63.6, 54.4 (the middle one is high); both are caught, and
    in both cases the middle row is the one flagged.
    """
    rising = direction == "up"
    for i in range(1, len(series)):
        (x_prev, prev), (x, cur) = series[i - 1], series[i]
        broken = cur["value"] < prev["value"] if rising else cur["value"] > prev["value"]
        if not broken:
            continue
        culprit, at, other, other_at = cur, x, prev, x_prev
        # Never reason from a reading already called a misprint: with a corrupt
        # anchor the before/prev/cur shape carries no information, so the break
        # itself is flagged rather than a guess at which reading caused it.
        if i >= 2 and not series[i - 2][1].get("anomaly"):
            x_before, before = series[i - 2]
            spike = ((before["value"] < prev["value"] and cur["value"] > before["value"])
                     if rising else
                     (before["value"] > prev["value"] and cur["value"] < before["value"]))
            # Three points cannot tell a spike at `prev` from a dip at `cur`:
            # the two give the same before/prev/cur shape, and both really do
            # occur. A FOURTH point settles it - draw the line through the
            # readings either side of the suspect pair and blame whichever of
            # the two sits further off it:
            #
            #   ammonia, 1.1 bar   1.785, 1.88, 1.875, 1.92   -> 1.88 spikes
            #   propane, 1.1 bar   210.2, 214.6, 213.9, 223.3 -> 213.9 dips
            #
            # and in the propane case 214.6 is exactly what every other pressure
            # column prints at that temperature, so blaming it would have put
            # the flag on the one reading there is no reason to doubt.
            if spike and i + 1 < len(series):
                x_next, nxt = series[i + 1]
                span = x_next - x_before
                if span:
                    slope = (nxt["value"] - before["value"]) / span
                    def off(value, at_x):
                        return abs(value - (before["value"] + slope * (at_x - x_before)))
                    spike = off(prev["value"], x_prev) >= off(cur["value"], x)
            if spike:
                culprit, at, other, other_at = prev, x_prev, before, x_before
        way = "rise" if rising else "fall"
        culprit["anomaly"] = (
            f"BREAKS A PHYSICAL RUN: {where} {what} can only {way} {against}, "
            f"but the sheet prints {other['raw']} at {other_at:g}{unit} and "
            f"{culprit['raw']} at {at:g}{unit}. This reading is the one out of "
            f"line with its neighbours on both sides. Stored as printed and NOT "
            f"corrected.")


def read_superheated(grid: List[List[str]], errors: List[str]) -> List[dict]:
    """The temperature x pressure grid, with its zero cells rejected."""
    head = find_row(grid, lambda r: any(c in SUPERHEATED_HEADINGS for c in r))
    mislabelled = head is not None and any(c == MISLABELLED_SUPERHEATED
                                           for c in grid[head])
    if head is None:
        errors.append(f"no superheated-vapour block on the sheet - looked for "
                      f"{' or '.join(map(repr, SUPERHEATED_HEADINGS))}")
        return []

    # The legend states what v, h and P are. P being ABSOLUTE is the part that
    # changes the meaning of every pressure below, so it is required.
    legend = " ".join(c for r in grid[head:head + 6] for c in r)
    if "absolute pressure" not in legend.lower():
        errors.append("the superheated block's legend no longer says its "
                      "pressures are absolute; a gauge/absolute mix-up cannot "
                      "be detected later")

    # The 'Temp.' cell is not always in column 0 - ETHYLENE's grid starts at
    # column 15, to the right of the worksheet that shares the sheet.
    h1 = temp_col = None
    for i in range(head, len(grid)):
        for col, text in enumerate(grid[i]):
            if norm(text) == "temp.":
                h1, temp_col = i, col
                break
        if h1 is not None:
            break
    if h1 is None:
        errors.append("the superheated-vapour block has no 'Temp.' header row")
        return []

    # Whether THIS sheet misspells kcal, read from its own legend rather than
    # assumed: butadiene, ammonia and ethane all print 'keal/kg' here, but
    # ethylene spells it 'Kcal/Kg' and must not carry a note saying otherwise.
    misprinted_unit = PRINTED_ENERGY_UNIT in legend.lower()

    # "P = 1.0 bar" spans the v and h columns beneath it.
    pressures: List[Tuple[int, float]] = []
    for col, text in enumerate(grid[h1]):
        if col <= temp_col:
            continue
        # Case-insensitive: propylene heads these columns "p = 1.0 bar" where
        # every other sheet uses a capital P.
        m = re.match(r"^P\s*=\s*(\d+(?:\.\d+)?)\s*bar\.?$", text.strip(),
                     flags=re.IGNORECASE)
        if m:
            pressures.append((col, float(m.group(1))))
    if not pressures:
        errors.append("the superheated header carries no 'P = <n> bar' columns")
        return []

    sub = grid[h1 + 1]
    for col, bar in pressures:
        got = [norm(sub[col]) if col < len(sub) else "",
               norm(sub[col + 1]) if col + 1 < len(sub) else ""]
        if got != ["v", "h"]:
            errors.append(f"the P = {bar} bar column has sub-headers {got!r}, "
                          f"expected ['v', 'h']")

    rows: List[dict] = []
    zeros: Dict[float, List[float]] = {bar: [] for _, bar in pressures}
    seen: set = set()
    started = False
    for i in range(h1 + 2, len(grid)):
        row = grid[i]
        if not any(row):
            if started:
                break
            continue
        temp = number(row[temp_col]) if temp_col < len(row) else None
        if temp is None:
            if started:
                break
            continue
        started = True
        if temp in seen:
            errors.append(f"superheated row {i + 1}: temperature {temp} twice")
            continue
        seen.add(temp)

        for col, bar in pressures:
            cells = [row[col] if col < len(row) else "",
                     row[col + 1] if col + 1 < len(row) else ""]
            values = [number(c) for c in cells]
            if values[0] == 0 and all(v == 0 for v in values if v is not None):
                # Not a state: see THE ZEROS ... in the module docstring.
                zeros[bar].append(temp)
                continue
            for (prop, unit), raw, value in zip(SUPERHEATED_PAIR, cells, values):
                if value is None:
                    errors.append(f"superheated row {i + 1} ({temp:g}°C, {bar} "
                                  f"bar): {prop} reads {raw!r}, not a number")
                    continue
                if value == 0:
                    errors.append(
                        f"superheated row {i + 1} ({temp:g}°C, {bar} bar): "
                        f"{prop} is 0 but its neighbour is not. A zero here "
                        f"means the state is not tabulated, which applies to "
                        f"the whole cell, so half a zero pair is a hole in the "
                        f"data")
                    continue
                rows.append({"temperature": temp, "pressure": bar,
                             "property_name": prop, "phase": "VAPOUR",
                             "unit": unit,
                             "printed_unit": prop == "enthalpy" and misprinted_unit,
                             "value": value, "raw": raw})

    # At a fixed pressure both series can only rise with temperature: a gas
    # expands and gains enthalpy as it is heated.
    for _, bar in pressures:
        for prop, _unit in SUPERHEATED_PAIR:
            series = sorted(((r["temperature"], r) for r in rows
                             if r["pressure"] == bar and r["property_name"] == prop),
                            key=lambda pair: pair[0])
            flag_monotonic(series, "up", prop.replace("_", " ") + " of a vapour",
                           f"at a fixed {bar:g} bar", "with temperature", "°C")

    # A missing state must sit below the boiling line, i.e. at the COLD end of
    # its column. A gap anywhere else is missing data, not an absent state.
    temps = sorted(seen)
    for bar, missing in zeros.items():
        if not missing:
            continue
        expected = temps[:len(missing)]
        if sorted(missing) != expected:
            errors.append(
                f"P = {bar} bar has no value at {sorted(missing)}, which is not "
                f"a run from the cold end of the column ({expected}). Below the "
                f"boiling point is the only reason a superheated state is "
                f"absent, so this looks like missing data instead")
    return [{"rows": rows, "zeros": zeros, "temps": temps,
             "mislabelled": mislabelled}]


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


def upsert_thermo(cur, cargo_gas_id: int, source_id: int, property_type: str,
                  temperature: float, pressure: Optional[float], phase: str,
                  property_name: str, value: float, raw_value: str, unit: str,
                  notes: Optional[str], page_ref: str) -> None:
    """Insert or refresh one thermodynamic reading.

    ON CONFLICT names the PARTIAL unique index by repeating its predicate -
    PostgreSQL infers a partial index only when the statement carries the same
    WHERE clause. The two states have different natural keys, so there are two
    statements rather than one.
    """
    common = """
        INSERT INTO cargo_gas_thermodynamic_property
            (cargo_gas_id, source_id, property_type, temperature,
             temperature_unit, pressure, pressure_unit, phase, property_name,
             value, raw_value, unit, source_page_ref, notes,
             created_at, updated_at)
        VALUES (%s, %s, %s, %s, '°C', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                now(), now())
    """
    update = """
        DO UPDATE SET value           = EXCLUDED.value,
                      raw_value       = EXCLUDED.raw_value,
                      unit            = EXCLUDED.unit,
                      pressure_unit   = EXCLUDED.pressure_unit,
                      source_page_ref = EXCLUDED.source_page_ref,
                      notes           = EXCLUDED.notes,
                      updated_at      = now()
    """
    if property_type == "SATURATED":
        conflict = ("ON CONFLICT (cargo_gas_id, source_id, temperature, phase, "
                    "property_name) WHERE property_type = 'SATURATED' ")
    else:
        conflict = ("ON CONFLICT (cargo_gas_id, source_id, temperature, pressure, "
                    "phase, property_name) WHERE property_type = 'SUPERHEATED' ")
    cur.execute(
        common + conflict + update,
        (cargo_gas_id, source_id, property_type, temperature, pressure,
         "bar (absolute)" if pressure is not None else None, phase,
         property_name, value, raw_value, unit, page_ref, notes),
    )


def report(parsed: dict) -> None:
    sat = parsed["saturated"]
    sup = parsed["superheated"][0] if parsed["superheated"] else {"rows": [], "zeros": {}}
    log.info("sheet %r - %s", parsed["sheet"], parsed["printed_gas"])
    log.info("gas_name: %r", parsed["gas_name"])

    log.info("general properties: %d", len(parsed["general"]))
    for g in parsed["general"]:
        log.info("    %-34s %-28s %s %s", g["label"], g["field"],
                 g["value"], g["unit"] or "")

    if parsed.get("declined"):
        log.info("propert(ies) the sheet prints but leaves blank (no row "
                 "written): %s", "; ".join(parsed["declined"]))

    if parsed.get("unclaimed"):
        log.warning("%d line(s) state a property this loader has no spelling "
                    "for, so NO row is written for them. Add the spelling to "
                    "GENERAL if the property belongs in the database:",
                    len(parsed["unclaimed"]))
        for line in parsed["unclaimed"]:
            log.warning("    %s", line)

    if sat:
        temps = [r["temperature"] for r in sat]
        log.info("saturated: %d temperature(s) %g..%g°C x %d series = %d row(s)",
                 len(sat), temps[0], temps[-1], len(SERIES),
                 sum(len(r["values"]) for r in sat))
        # The axis is not the same on every sheet, so print its shape rather
        # than assume it: an odd step stands out here without the loader having
        # to claim it is wrong.
        steps: Dict[float, int] = {}
        for prev, cur in zip(temps, temps[1:]):
            step = round(cur - prev, 6)
            steps[step] = steps.get(step, 0) + 1
        log.info("    temperature steps: %s",
                 ", ".join(f"{k:g}°C x{v}" for k, v in sorted(steps.items())))
        if parsed.get("series"):
            log.info("    columns read from the header: %s",
                     ", ".join(f"{prop}"
                               f"{'/' + phase.lower() if phase != 'NONE' else ''}"
                               f"=col{spec['col']}"
                               for (prop, phase), spec in parsed["series"].items()))

    if sup.get("mislabelled"):
        log.warning("the sheet heads its superheated grid %r. It IS the "
                    "superheated grid - six chosen absolute pressures, each "
                    "with v and h, and the real saturated table is above it - "
                    "so it is stored as SUPERHEATED and every row says the "
                    "heading is wrong.", MISLABELLED_SUPERHEATED)

    if sup["rows"]:
        pressures = sorted({r["pressure"] for r in sup["rows"]})
        log.info("superheated: %d temperature(s) %g..%g°C x pressures %s = %d row(s)",
                 len(sup["temps"]), sup["temps"][0], sup["temps"][-1],
                 ", ".join(f"{p} bar" for p in pressures), len(sup["rows"]))
        absent = sum(len(v) for v in sup["zeros"].values())
        log.info("superheated states the sheet prints as 0 (below the boiling "
                 "point at that pressure - NOT loaded, not stored as 0): %d", absent)
        for bar, temps in sorted(sup["zeros"].items()):
            if temps:
                log.info("    %5s bar  %s", bar,
                         ", ".join(f"{t:g}°C" for t in sorted(temps)))
    if parsed["enthalpy_note"]:
        log.info("enthalpy datum recorded on every enthalpy row: %s",
                 parsed["enthalpy_note"])
    else:
        log.warning("this sheet states NO enthalpy datum. Its enthalpies are "
                    "loaded as printed, each flagged that the reference state "
                    "is unknown and so not comparable with another gas's. "
                    "Latent heat is unaffected - a difference cancels the datum.")

    if parsed.get("misspelt"):
        log.warning("the sheet titles itself %r; loaded as %r",
                    parsed["printed_gas"], parsed["gas_name"])

    anomalies = []
    for g in parsed["general"]:
        for n in g["notes"]:
            if n.startswith(("IMPOSSIBLE", "INCONSISTENT")):
                anomalies.append((f"general/{g['field']}", g["value"], n))
    for row in sat:
        if row.get("anomaly"):
            anomalies.append((f"saturated {row['temperature']:g}°C",
                              "temperature axis", row["anomaly"]))
        for v in row["values"]:
            if v.get("anomaly"):
                anomalies.append((f"saturated {row['temperature']:g}°C",
                                  f"{v['property_name']}/{v['phase'].lower()} "
                                  f"= {v['raw']}", v["anomaly"]))
    for r in sup["rows"]:
        if r.get("anomaly"):
            anomalies.append((f"superheated {r['temperature']:g}°C @ "
                              f"{r['pressure']:g} bar",
                              f"{r['property_name']} = {r['raw']}", r["anomaly"]))

    if anomalies:
        log.warning("%d figure(s) the sheet contradicts itself or physics on - "
                    "loaded as printed, flagged in notes, NOT corrected:",
                    len(anomalies))
        for where, what, why in anomalies:
            log.warning("    %-30s %-34s %s", where, what,
                        why.split(":")[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help="gas sheet to load")
    ap.add_argument("--gas-name", default=None,
                    help=f"cargo_gas.gas_name (default {DEFAULT_GAS_NAME!r} "
                         f"for the default sheet)")
    ap.add_argument("--list-sheets", action="store_true", help="list the workbook's sheets")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    if args.list_sheets:
        for name in pd.ExcelFile(path).sheet_names:
            log.info("  %s", name)
        return 0

    gas_name = args.gas_name or (DEFAULT_GAS_NAME if args.sheet == DEFAULT_SHEET
                                 else args.sheet.replace("_", " ").title())
    parsed, errors = read_sheet(path, args.sheet, gas_name)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    report(parsed)
    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    page_ref = f"{path.name} [{args.sheet}]"
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            fields = [g["field"] for g in parsed["general"]]
            added = ensure_field_definitions(cur, only=list(dict.fromkeys(fields)))
            log.info("field_definitions: %d created, %d already present",
                     added, len(set(fields)) - added)

            gas_id, created = upsert_gas(cur, source_id, parsed["gas_name"])
            log.info("cargo_gas id=%s (%s)", gas_id,
                     "created" if created else "already present")

            for g in parsed["general"]:
                notes = list(g["notes"])
                notes.append(f"Read from {page_ref}, the general-properties "
                             f"block above the thermodynamic tables.")
                upsert_property(
                    cur, gas_id, source_id, g["field"], value=g["value"],
                    normalized_value=g["normalized"], unit=g["unit"],
                    value_type="number", entered_by=ENTERED_BY,
                    source_page_ref=page_ref, notes=" ".join(notes),
                )

            datum = parsed["enthalpy_note"]
            written = 0
            for row in parsed["saturated"]:
                for v in row["values"]:
                    notes = []
                    if v["printed_unit"]:
                        notes.append(ENERGY_UNIT_NOTE)
                    if v["property_name"] == "enthalpy":
                        notes.append(f"Datum, from the sheet's Note 1: {datum}"
                                     if datum else NO_DATUM_NOTE)
                    if v["property_name"] == "vapour_pressure":
                        notes.append(
                            "The saturation pressure AT this temperature - a "
                            "reading, not a state the table was measured at, "
                            "which is why the row's `pressure` column is NULL "
                            "and this is stored as a property.")
                    if row.get("anomaly"):
                        notes.append(row["anomaly"])
                    if v.get("anomaly"):
                        notes.append(v["anomaly"])
                    upsert_thermo(cur, gas_id, source_id, "SATURATED",
                                  row["temperature"], None, v["phase"],
                                  v["property_name"], v["value"], v["raw"],
                                  v["unit"], " ".join(notes) or None, page_ref)
                    written += 1

            sup = parsed["superheated"][0]
            for r in sup["rows"]:
                notes = []
                if sup.get("mislabelled"):
                    notes.append(MISLABELLED_SUPERHEATED_NOTE)
                if r["printed_unit"]:
                    notes.append(ENERGY_UNIT_NOTE)
                if r["property_name"] == "enthalpy":
                    notes.append(f"Datum, from the sheet's Note 1: {datum}"
                                 if datum else NO_DATUM_NOTE)
                if r.get("anomaly"):
                    notes.append(r["anomaly"])
                upsert_thermo(cur, gas_id, source_id, "SUPERHEATED",
                              r["temperature"], r["pressure"], r["phase"],
                              r["property_name"], r["value"], r["raw"],
                              r["unit"], " ".join(notes) or None, page_ref)
                written += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: 1 | cargo_gas_property_values: %d | "
                 "cargo_gas_thermodynamic_property: %d",
                 len(parsed["general"]), written)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
