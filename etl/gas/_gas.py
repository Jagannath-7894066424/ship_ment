"""Shared helpers for the gas loaders.

Every gas source feeds the same pair of tables (``cargo_gas`` and
``cargo_gas_property_values``), so the field-definition seed, the master upsert
and the property upsert live here rather than being duplicated per loader. The
oil branch has the same arrangement in ``etl/oil/_crude_oil.py``, and this
mirrors it deliberately - the two branches are the same shape.

Nothing here touches ``cargo_chemical`` or ``crude_oil``. A gas is its own
master; the three branches meet only at ``source``.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("gas")

# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------
# field_name -> (display_name, data_type, canonical_unit, category, description)
#
# field_definitions is shared by all three branches (cargo_property_values,
# crude_oil_property_values and cargo_gas_property_values all FK it), so a name
# added here is visible everywhere and must stay specific enough to be
# unambiguous.
FIELD_DEFS: Dict[str, Tuple[str, str, Optional[str], str, str]] = {
    "RELATIVE_VAPOUR_DENSITY": (
        "Relative Vapour Density", "number", None, "Physical",
        "Vapour density relative to air, which the source lists as 1.00. "
        "DIMENSIONLESS - it is a ratio, so `unit` is deliberately NULL. The "
        "source's 'KG / CUB.M' legend describes the densities the ratio is "
        "computed from, not the ratio itself. The measurement conditions vary "
        "per row and are recorded on the value, not here.",
    ),
    # The nine below already exist in field_definitions - they are shared with
    # the chemical and oil branches, which is the point: a gas boiling point is
    # the same measurement as a chemical one, so it reuses the same field rather
    # than minting a GAS_BOILING_POINT twin. They are listed here so a loader
    # can seed them on an empty database; ON CONFLICT DO NOTHING means an
    # existing definition (and its wording) is never overwritten.
    #
    # Keep these descriptions about the FIELD. Anything that is true only of one
    # source's column - the basis a figure was measured on, a footnote, a unit
    # printed differently - belongs on that value's `notes`, not here.
    "molecular_formula": (
        "Molecular Formula", "text", None, "Identity",
        "Chemical formula as the source prints it. Text, not a parsed formula: "
        "a mixture may have no single formula and the source may say so in words.",
    ),
    "UN_NUMBER": (
        "UN Number", "text", None, "Regulatory",
        "UN transport number as the source prints it. Text, not a number: a row "
        "may cite several ('1223/1202', '2398 & 1149').",
    ),
    "molecular_weight_g_mol": (
        "Molecular Weight", "number", "g/mol", "Physical",
        "Molecular weight. Canonical unit g/mol; kg/kmol is the same number and "
        "is stored as the value's own unit when a source prints it that way.",
    ),
    "boiling_point_c": (
        "Boiling Point", "number", "°C", "Physical",
        "Boiling point in °C. The pressure it was measured at is a property of "
        "the source's column, so it is recorded on the value.",
    ),
    "specific_gravity": (
        "Specific Gravity", "number", None, "Physical",
        "Density relative to water. DIMENSIONLESS - it is a ratio, so `unit` is "
        "deliberately NULL. The temperature it was measured at varies by source "
        "and by row, and is recorded on the value.",
    ),
    "flash_point_c": (
        "Flash Point", "number", "°C", "Physical",
        "Flash point in °C. Closed- or open-cup is a source detail and is "
        "recorded on the value when the source states it.",
    ),
    "flammable_limits": (
        "Flammable Limits", "text", None, "Physical",
        "Flammable (explosive) range in air, as the source prints it, e.g. "
        "'1.5-9%'. Text so the source's own wording survives; the two ends are "
        "normalized into normalized_min / normalized_max in % by volume.",
    ),
    "tlv_twa_ppm": (
        "TLV-TWA", "number", "ppm", "Health",
        "Threshold limit value, time-weighted average, in ppm.",
    ),
    "odour_limit": (
        "Odour Limit", "number", "ppm", "Physical",
        "Concentration at which the substance can be smelled, in ppm. Sources "
        "often answer this in words ('odourless', 'less than 1') rather than a "
        "figure; such an answer is stored verbatim as text. What exactly was "
        "measured - a detection threshold, a recognition concentration - varies "
        "by source and is recorded on the value.",
    ),
    "lel": (
        "Lower Explosive Limit", "number", "% vol", "Physical",
        "Lower flammable limit: the leanest mixture in air that will burn, in "
        "% by volume. Paired with `uel`; a source that prints the range as one "
        "string goes to `flammable_limits` instead.",
    ),
    "uel": (
        "Upper Explosive Limit", "number", "% vol", "Physical",
        "Upper flammable limit: the richest mixture in air that will burn, in "
        "% by volume. Paired with `lel`.",
    ),
    # New with the Properties of Gases Doc source. Neither exists in the
    # chemical branch's catalog (etl/common/field_definition.py) - the critical
    # point is a gas-cargo concern, since it is what decides whether a cargo can
    # be liquefied by pressure alone at a given temperature.
    "critical_temperature_c": (
        "Critical Temperature", "number", "°C", "Physical",
        "Temperature above which the substance cannot be liquefied by pressure, "
        "in °C. Always above the atmospheric boiling point - a source that "
        "prints it below is misprinted, not merely surprising.",
    ),
    # New with "Properties of Gases Doc Data.xlsx".
    "liquid_to_gas_expansion_ratio": (
        "Liquid to Gas Expansion Ratio", "number", None, "Physical",
        "Volume of gas produced per unit volume of liquid on vaporising at 15°C "
        "and 1 bar. DIMENSIONLESS.",
    ),
    "liquid_volume_per_ideal_gas_60f": (
        "Liquid Volume per Ideal Gas (60°F, 760 mmHg)", "number", None, "Physical",
        "The source's 'Liquid volume in ml of ideal gas at 60 °F and 760 mmHg', "
        "stored as printed. The source gives no further unit.",
    ),
    "specific_gravity_60_60f": (
        "Specific Gravity 60/60°F (vac.)", "number", None, "Physical",
        "Specific gravity at 60/60°F, corrected to vacuum. A different basis from "
        "`specific_gravity` (15°C/15°C), so it is its own field and the two never "
        "overwrite each other. DIMENSIONLESS.",
    ),
    "critical_pressure_kpa": (
        "Critical Pressure", "number", "kPa", "Physical",
        "Pressure at the critical point, in kPa. Absolute unless a source says "
        "otherwise, which is recorded on the value.",
    ),
    # New with the Cargo Data source.
    "density": (
        "Density", "number", "kg/l", "Physical",
        "Density of the liquid, in kg/l. Distinct from `specific_gravity`: use "
        "this when the source says 'density', and specific_gravity when it says "
        "the figure is relative to water. The two are numerically almost equal "
        "near 15°C, but only one of them is what the source actually claimed.",
    ),
    "imo_class": (
        "IMO Class", "text", None, "Regulatory",
        "Hazard class as a source's own column names it ('IMO class'), stored "
        "verbatim. Deliberately NOT folded into `imdg_class`: a source that "
        "prints a class beside a UN number does not always agree with the IMDG "
        "class for that number, and equating the two would assert a regulatory "
        "fact the source never made.",
    ),
    # New with the Thermodynamic Data_Properties source.
    #
    # These two hold the same physical quantity as `lel` / `uel` above, under
    # the name its source prints: that sheet heads the line "Limits of
    # Inflammability", and inflammability limit is the older name for what the
    # chemical branch's sources call an explosive limit. Same unit, so the
    # figures are directly comparable - see each description for how to query
    # both together.
    "lower_inflammability_limit": (
        "Lower Inflammability Limit", "number", "% vol", "Physical",
        "Leanest vapour-in-air mixture that will burn, in % by volume. The same "
        "quantity as `lel` (Lower Explosive Limit), recorded under the name "
        "used by sources that head it 'Limits of Inflammability'. To read every "
        "source at once, query field_name IN ('lel', 'lower_inflammability_limit').",
    ),
    "upper_inflammability_limit": (
        "Upper Inflammability Limit", "number", "% vol", "Physical",
        "Richest vapour-in-air mixture that will burn, in % by volume. The same "
        "quantity as `uel` (Upper Explosive Limit), recorded under the name "
        "used by sources that head it 'Limits of Inflammability'. To read every "
        "source at once, query field_name IN ('uel', 'upper_inflammability_limit').",
    ),
    "auto_ignition_temperature": (
        "Auto-Ignition Temperature", "number", "°C", "Physical",
        "Temperature at which the substance ignites in air without an ignition "
        "source, in °C.",
    ),
    "minimum_ignition_energy_mj": (
        "Minimum Ignition Energy", "number", "mJ", "Physical",
        "Spark energy needed to ignite the most easily ignitable mixture in "
        "air, in millijoules. Sources usually quote it as an order of "
        "magnitude ('approx. 1 millijoule'); the qualifier is kept on the value.",
    ),
    "gas_viscosity_cp": (
        "Gas-Phase Viscosity", "number", "cP", "Physical",
        "Dynamic viscosity of the gaseous phase, in centipoise. Strongly "
        "temperature-dependent, and the temperature a source measured it at "
        "varies - it is recorded on the value, not here. Distinct from "
        "`viscosity_cp_20c`, which fixes the temperature in its name.",
    ),
    # New with the IGC Code 2016 source (etl/gas/igc_code.py). All seven are
    # carriage REQUIREMENTS imposed on a ship by regulation, not properties
    # measured of a cargo, so every one is text with no unit and no normalized
    # value: "2G/2PG" is a ship type, not a quantity.
    "igc_ship_type": (
        "IGC Ship Type", "text", None, "Regulatory",
        "The ship type a cargo may only be carried in, from IGC Code chapter "
        "19 - 1G, 2G, 2PG or 3G, in decreasing order of the damage the ship "
        "must survive. 2G/2PG means the code allows either.",
    ),
    "igc_independent_tank_c_required": (
        "IGC Independent Tank Type C Required", "text", None, "Regulatory",
        "Whether IGC chapter 19 requires this cargo to be carried in an "
        "independent type C tank - a pressure vessel - rather than any tank "
        "type the ship type allows.",
    ),
    "igc_vapour_space_control": (
        "IGC Vapour Space Control", "text", None, "Regulatory",
        "What the cargo tank's vapour space must be kept as under IGC chapter "
        "19: 'Inert' or 'Dry'. Controls the atmosphere ABOVE the cargo, which "
        "is a carriage requirement rather than a property of the cargo.",
    ),
    "igc_vapour_detection": (
        "IGC Vapour Detection", "text", None, "Regulatory",
        "The vapour detection equipment IGC chapter 19 requires for this "
        "cargo - flammable, toxic, both, or asphyxiant. It states what the "
        "ship must be able to detect, not what the vapour is.",
    ),
    "igc_gauging": (
        "IGC Gauging Type", "text", None, "Regulatory",
        "The cargo gauging types IGC chapter 19 permits - indirect, closed or "
        "restricted. A permission, so the entry lists every type allowed.",
    ),
    "igc_gauging_reference": (
        "IGC Gauging Reference", "text", None, "Regulatory",
        "The IGC Code paragraph numbers behind the gauging entry (13.2.3.1 "
        "and following). A pointer into the code, not a value.",
    ),
    "igc_special_requirements": (
        "IGC Special Requirements", "text", None, "Regulatory",
        "IGC Code paragraph numbers imposing further requirements on this "
        "cargo, from chapters 14 and 17. A pointer into the code: the "
        "requirement itself is the paragraph's text, which this table does not "
        "reproduce.",
    ),
    "liquid_viscosity_cp": (
        "Liquid-Phase Viscosity", "number", "cP", "Physical",
        "Dynamic viscosity of the liquid phase, in centipoise. Like its "
        "gas-phase counterpart it is strongly temperature-dependent, and the "
        "temperature a source measured it at is recorded on the value rather "
        "than fixed here - the thermodynamic workbook quotes n-butane at 0°C "
        "and ethylene at -100°C, near each cargo's carriage temperature.",
    ),
    "condensing_ratio_dm3_per_m3": (
        "Condensing Ratio", "number", "dm³/m³", "Physical",
        "Volume of liquid, in dm³, that condenses from 1 m³ of gas. A cargo-"
        "handling figure rather than a thermodynamic one: it answers how much "
        "liquid a tank of vapour becomes.",
    ),
    # New with the IMO Cargo.XLSX source.
    #
    # That source also has a "Synonyms" column, and it is deliberately NOT a
    # field here. A name is not a property of a cargo: it goes to the shared
    # `synonyms` table through cargo_gas_synonym, the same route the chemical
    # and oil branches use, so one row of text serves all three branches and a
    # lookup by name is an indexed join rather than a scan of property values.
    # See upsert_synonym / link_synonym below.
    "mfag_number": (
        "MFAG Table No.", "text", None, "Health",
        "Table number in the IMO Medical First Aid Guide (MFAG), the emergency "
        "medical guidance carried for accidents involving dangerous goods. A "
        "POINTER to a table, not a measurement - text, and never arithmetic. "
        "Several cargoes share one table (the file that introduced this field "
        "sends every hydrocarbon gas to 310), so it identifies a treatment "
        "regime rather than the cargo.",
    ),
    # New with the Products.doc source.
    "vapour_density_kg_m3": (
        "Vapour Density", "number", "kg/m³", "Physical",
        "ABSOLUTE density of the vapour, in kg/m³ - the mass of a cubic metre "
        "of the gas itself. Distinct from `RELATIVE_VAPOUR_DENSITY`, which is "
        "the dimensionless ratio to air, and from the chemical branch's "
        "`vapour_density`, which carries no unit and no statement of basis, so "
        "the two cannot be assumed to hold the same quantity. A vapour density "
        "is meaningless without the state it was measured in - Products.doc "
        "tabulates it at the boiling point - and that state is recorded on the "
        "value, not here.",
    ),
    "composition": (
        "Composition", "text", None, "Identity",
        "What a mixture is made of, in the source's own words and proportions "
        "('Butadiene (43%) + Iso-Butylene (21%) + ...'). Text, not a parsed "
        "breakdown: sources state compositions as approximations, as ranges, "
        "or with the balance left unnamed, and reducing that to structured "
        "fractions would assert a precision they did not claim.",
    ),
    "tlv_twa_mg_m3": (
        "TLV-TWA (mass basis)", "number", "mg/m³", "Health",
        "Threshold limit value, time-weighted average, in mg/m³. The same limit "
        "as `tlv_twa_ppm` on a mass basis - sources print one, the other, or "
        "both, and converting between them needs the molecular weight, so each "
        "is stored as the source gave it.",
    ),
    # ------------------------------------------------------------------
    # New with the Tanker Safety Guide (Liquefied Gas) source
    # (etl/gas/tanker_safety_guide.py). That guide is the first gas source
    # that is neither purely physical nor purely regulatory: it is a SAFETY
    # guide, so half its table is what a crew must DO - emergency procedures,
    # effects of exposure, protective equipment, materials that must not be
    # used. None of it is a measurement, so none of it carries a unit.
    #
    # Four definitions below (appearance, cas_number, personal_protective_
    # methods, vapour_pressure) already exist in field_definitions, seeded by
    # the chemical branch. They are restated here only so ensure_field_
    # definitions can seed them on an empty database; ON CONFLICT DO NOTHING
    # means the chemical branch's wording is never overwritten.
    # ------------------------------------------------------------------
    "appearance": (
        "Appearance", "text", None, "Physical",
        "What the substance looks like, in the source's own words.",
    ),
    "cas_number": (
        "CAS Number", "text", None, "Identity",
        "CAS Registry Number as the source prints it. Text, not a number: a "
        "row may cite several, and a mixture legitimately lists one per "
        "component.",
    ),
    "personal_protective_methods": (
        "Personal Protection", "text", None, "Health",
        "Protective equipment and precautions the source requires for people "
        "handling the cargo.",
    ),
    "vapour_pressure": (
        "Vapour Pressure", "number", "bar", "Physical",
        "Vapour pressure in bar. Meaningless without the temperature it was "
        "measured at, which varies by source and by row and is recorded on "
        "the value.",
    ),
    "odour": (
        "Odour", "text", None, "Physical",
        "What the substance smells like, in the source's own words. Distinct "
        "from `odour_limit`, which is the CONCENTRATION at which it can be "
        "smelled: this field is the description, that one is the figure.",
    ),
    "main_hazard": (
        "Main Hazard", "text", None, "Health",
        "The hazard classes a source leads with for this cargo - FLAMMABLE, "
        "TOXIC, CORROSIVE, OXIDISING, ASPHYXIA - in its own words and order. "
        "A safety guide's headline summary, NOT a regulatory classification: "
        "`imdg_class` and `imo_class` hold those.",
    ),
    "emergency_procedure_fire": (
        "Emergency Procedure - Fire", "text", None, "Health",
        "What to do if the cargo is on fire, verbatim. An INSTRUCTION, so it "
        "is never paraphrased or shortened - the wording is the safety "
        "content.",
    ),
    "emergency_procedure_liquid_in_eye": (
        "Emergency Procedure - Liquid in Eye", "text", None, "Health",
        "First-aid instruction for liquid cargo in the eye, verbatim.",
    ),
    "emergency_procedure_liquid_on_skin": (
        "Emergency Procedure - Liquid on Skin", "text", None, "Health",
        "First-aid instruction for liquid cargo on the skin, verbatim.",
    ),
    "emergency_procedure_vapour_inhaled": (
        "Emergency Procedure - Vapour Inhaled", "text", None, "Health",
        "First-aid instruction for a casualty who has inhaled the vapour, "
        "verbatim.",
    ),
    "emergency_procedure_spillage": (
        "Emergency Procedure - Spillage", "text", None, "Health",
        "What to do about a spill of the cargo, verbatim. Covers the response "
        "to an escape, not the cleaning of a tank afterwards - that is "
        "cleaning_process.",
    ),
    "oel_tlv": (
        "Occupational Exposure Limit / TLV", "number", "ppm", "Health",
        "The occupational exposure limit a source prints under one heading "
        "('OEL-TLV'), in ppm. Deliberately NOT folded into `tlv_twa_ppm`: a "
        "source that heads one column this way mixes averaging bases under "
        "it - a short-term or ceiling limit ('25ppm (STEL-C)') is a different "
        "quantity from an eight-hour average, and filing it as a TWA would "
        "assert a claim the source never made. Which basis a figure is on is "
        "recorded on the value. Sources often answer in words instead ('Simple "
        "asphyxiant'), and such an answer is stored verbatim as text.",
    ),
    "effect_of_liquid_eyes": (
        "Effect of Liquid - Eyes", "text", None, "Health",
        "What contact with the LIQUID does to the eyes.",
    ),
    "effect_of_liquid_skin": (
        "Effect of Liquid - Skin", "text", None, "Health",
        "What contact with the LIQUID does to the skin.",
    ),
    "effect_of_liquid_skin_absorption": (
        "Effect of Liquid - Skin Absorption", "text", None, "Health",
        "Whether and how the LIQUID is absorbed through the skin. A separate "
        "question from surface damage (`effect_of_liquid_skin`): a cargo can "
        "burn the skin without being absorbed, or be absorbed without burning.",
    ),
    "effect_of_liquid_ingestion": (
        "Effect of Liquid - Ingestion", "text", None, "Health",
        "What swallowing the LIQUID does.",
    ),
    "effect_of_vapour_eyes": (
        "Effect of Vapour - Eyes", "text", None, "Health",
        "What exposure to the VAPOUR does to the eyes.",
    ),
    "effect_of_vapour_skin": (
        "Effect of Vapour - Skin", "text", None, "Health",
        "What exposure to the VAPOUR does to the skin.",
    ),
    "effect_of_vapour_inhalation_acute": (
        "Effect of Vapour - Inhalation (Acute)", "text", None, "Health",
        "What a single or short exposure to the VAPOUR does. Paired with "
        "`effect_of_vapour_inhalation_chronic`; a cargo can be mild acutely "
        "and serious chronically, so the two are never merged.",
    ),
    "effect_of_vapour_inhalation_chronic": (
        "Effect of Vapour - Inhalation (Chronic)", "text", None, "Health",
        "What repeated or prolonged exposure to the VAPOUR does, including "
        "carcinogenicity where the source states it. Paired with "
        "`effect_of_vapour_inhalation_acute`.",
    ),
    "explosion_hazard": (
        "Explosion Hazard", "text", None, "Physical",
        "How the substance can explode, in the source's own words - "
        "peroxide formation, runaway polymerisation, expanding-vapour "
        "explosion. A MECHANISM, not a range: the concentrations that will "
        "burn are `flammable_limits` / `lel` / `uel`.",
    ),
    "chemical_family": (
        "Chemical Family", "text", None, "Identity",
        "The class of chemistry a source assigns the cargo to ('Olefin', "
        "'Halogenated Hydrocarbon', 'Amine (Aliphatic)'), verbatim. Sources "
        "use their own vocabulary and their own level of detail, so this is "
        "text rather than an enum, and it is NOT `reactive_groups`: that "
        "table drives compatibility verdicts and is assigned by this "
        "database, while this field records what the source said.",
    ),
    "reactivity_water": (
        "Reactivity - Water", "text", None, "Physical",
        "How the substance behaves with water, fresh or salt, including "
        "solubility and hydrate formation. Prose, and broader than the "
        "boolean `water_reactive` or the `water_solubility` field - a source "
        "answers this with a mechanism ('dissolves exothermically to produce "
        "ammonium hydroxide') that neither of those can hold.",
    ),
    "reactivity_air": (
        "Reactivity - Air", "text", None, "Physical",
        "How the substance behaves on contact with air - peroxide formation, "
        "oxidation, polymerisation.",
    ),
    "reactivity_other_liquids_gases": (
        "Reactivity - Other Liquids or Gases", "text", None, "Physical",
        "Substances a source warns this cargo reacts dangerously with, in its "
        "own words. A WARNING in prose, not a compatibility verdict: "
        "cargo_gas_compatibility holds the pairwise answers, and this field "
        "must not be parsed into them - the source names classes of chemistry "
        "('oxidising agents', 'halogens') as often as it names a substance.",
    ),
    "freezing_point_c": (
        "Freezing Point", "number", "\u00b0C", "Physical",
        "Temperature at which the liquid freezes, in \u00b0C. The same quantity as "
        "`melting_point_c`, recorded under the name used by sources that head "
        "the column 'Freezing Point'. To read every source at once, query "
        "field_name IN ('melting_point_c', 'melting_point', 'freezing_point_c').",
    ),
    "latent_heat_of_vaporisation_kj_kg": (
        "Latent Heat of Vaporisation", "number", "kJ/kg", "Physical",
        "Heat needed to boil one kilogram of the liquid, in kJ/kg. Strongly "
        "temperature-dependent, and sources tabulate it at the atmospheric "
        "boiling point rather than at a fixed temperature, so the temperature "
        "is recorded on the value and two sources' figures are only "
        "comparable once it is read.",
    ),
    "electrostatic_generation": (
        "Electrostatic Generation", "text", None, "Physical",
        "Whether the cargo accumulates a static charge in handling, in the "
        "source's own words. Text rather than boolean: sources answer 'Yes', "
        "'Likely', 'Not known' and 'None' in the same column, and 'Not known' "
        "is a different statement from 'No'.",
    ),
    "coefficient_of_cubic_expansion_per_c": (
        "Coefficient of Cubic Expansion", "number", "1/\u00b0C", "Physical",
        "Fractional increase in the liquid's VOLUME per \u00b0C of temperature "
        "rise. Itself temperature-dependent, so the temperature a source "
        "quotes it at is recorded on the value.",
    ),
    "normal_carriage_condition": (
        "Normal Carriage Condition", "text", None, "Carriage",
        "How the cargo is normally carried - pressurised, semi-pressurised, "
        "fully refrigerated, or at ambient - in the source's own words. "
        "Usually a list, because most cargoes may be carried more than one "
        "way, so it is text rather than an enum.",
    ),
    "materials_of_construction_unsuitable": (
        "Materials of Construction - Unsuitable", "text", None, "Carriage",
        "Materials the source says must NOT be used in contact with this "
        "cargo - metals, elastomers, coatings. A PROHIBITION, and the "
        "counterpart of `materials_of_construction_suitable`; the absence of "
        "a material from one list is not its presence on the other.",
    ),
    "materials_of_construction_suitable": (
        "Materials of Construction - Suitable", "text", None, "Carriage",
        "Materials the source says may be used in contact with this cargo. "
        "Broader than `permitted_tank_materials`: it covers gaskets, seals "
        "and hoses as well as the tank itself.",
    ),
    # ------------------------------------------------------------------
    # New with the Tanker Safety Guide's CARRIAGE CONDITIONS table
    # (etl/gas/tanker_safety_guide_carriage.py). That table is a second,
    # much smaller table in the same publication as the properties table
    # above, and it answers two of the same questions in coarser figures -
    # ethylene boils at "-104" there and "-103.7°C" in the properties table.
    # Two of the three fields below are therefore deliberate twins rather
    # than new quantities: they exist so one table in the book cannot
    # overwrite the other's answer, since both load under the same source_id
    # and cargo_gas_property_values is keyed (cargo, source, field).
    # ------------------------------------------------------------------
    "boiling_point_atmospheric_c": (
        "Boiling Point at Atmospheric Pressure", "number", "\u00b0C", "Physical",
        "Boiling point at atmospheric pressure, in \u00b0C, from a source that "
        "states the pressure basis in its own column heading. The same "
        "quantity as `boiling_point_c`, which does not: kept apart so that a "
        "table which spells the basis out is not confused with one that "
        "leaves it to be assumed, and so two tables in one publication can "
        "each keep their figure. To read every source at once, query "
        "field_name IN ('boiling_point_c', 'boiling_point', "
        "'boiling_point_atmospheric_c').",
    ),
    "vapour_pressure_45c": (
        "Vapour Pressure at 45\u00b0C", "number", "bar", "Physical",
        "Vapour pressure at 45\u00b0C, in bar. NOT interchangeable with "
        "`vapour_pressure`, which sources tabulate at the cargo's boiling "
        "point: 45\u00b0C is a fixed design reference - the temperature a "
        "pressurised gas carrier's tanks must be designed to hold the cargo "
        "at - so this figure sizes a pressure vessel where the other "
        "describes the cargo at its carriage temperature. A cargo whose "
        "critical temperature is below 45\u00b0C has no value here at all, and a "
        "source that says so in words is stored verbatim as text.",
    ),
    "practical_carriage_conditions": (
        "Practical Carriage Conditions", "text", None, "Carriage",
        "How a cargo can be carried in practice, in the source's own words. "
        "A twin of `normal_carriage_condition`, kept apart for the same "
        "reason as `boiling_point_atmospheric_c`: a source may answer both "
        "questions in one publication and phrase them differently "
        "('Fully-pressurised, semi-pressurised or fully-refrigerated' against "
        "'Pressurised or Fully-refrigerated'), and neither should overwrite "
        "the other. Query both together.",
    ),
    "notes_special_requirements": (
        "Notes and Special Requirements", "text", None, "Carriage",
        "The source's numbered notes and special carriage requirements for "
        "this cargo, verbatim and in its own numbering. Cells elsewhere in "
        "the same row point into it by number ('(Note 1)'), so it is what "
        "those pointers resolve against and is never summarised.",
    ),
}

# Cells that mean "no data" rather than a value.
MISSING = {"", "-", "--", "---", "n/a", "na", "none", "?", "nil"}

# entered_by is set per loader; these match the other two branches' convention.
ENTRY_TYPE = "import"
IS_WINNING = True
CONFLICT_FLAG = False


def clean_text(value: Any) -> Optional[str]:
    """Trim a cell and collapse whitespace; placeholders become None."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return None if s.lower() in MISSING else s


def ensure_field_definitions(cur, only: Optional[list] = None) -> int:
    """Create any missing gas field_definitions. Returns the number added.

    field_name is the FK target for cargo_gas_property_values, so a definition
    must exist before any value referencing it is inserted.
    """
    names = only if only is not None else list(FIELD_DEFS)
    added = 0
    for name in names:
        display, dtype, unit, category, description = FIELD_DEFS[name]
        cur.execute(
            """
            INSERT INTO field_definitions
                (field_name, display_name, data_type, unit, category, description,
                 typical_source, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (field_name) DO NOTHING
            """,
            (name, display, dtype, unit, category, description, "Gas cargo data"),
        )
        added += cur.rowcount
    return added


def gas_name_key(gas_name: str) -> str:
    """A gas name reduced to its letters and digits, for spotting one cargo
    spelled two ways WITHIN a single source.

    Only CASE, WHITESPACE and HYPHENS are removed, and deliberately nothing
    else. Stripping every non-alphanumeric character looks tidier and is wrong:
    it throws away characters that carry meaning, and the other two branches
    show exactly how. In crude_oil it would merge

        "Low sulfur fuel oil (sulfur >1 %)"  with  "... (sulfur <1%)"

    because > and < are punctuation to a regex and opposite statements to a
    reader; in cargo_chemical it would merge "a-Picoline", "b-Picoline" and
    "g-Picoline" - three different isomers - because Greek letters are not
    [a-z0-9]. Whitespace and hyphens are the only marks that vary purely by
    typography between one sheet of a workbook and the next, which is the
    mistake this function exists to catch.
    """
    return re.sub(r"[\s\-\u2010-\u2015]", "", gas_name.lower())


def _preferred_name(existing: str, incoming: str) -> str:
    """Which of two spellings of one cargo to keep on the cargo_gas row.

    The one with more word separators, because a source that writes
    "Propylene Oxide" on one sheet and "Propyleneoxide" on another has spelled
    it out properly once and run it together once - and the spelled-out form is
    both the readable one and the one that matches how other sources write it.
    A tie keeps what is already stored, so a re-run never churns the name.
    """
    separators = lambda n: len(re.findall(r"[^A-Za-z0-9]", n))
    return incoming if separators(incoming) > separators(existing) else existing


def upsert_gas(cur, source_id: int, gas_name: str,
               country_of_origin: Optional[str] = None) -> Tuple[int, bool]:
    """Insert or find one cargo_gas row. Returns (id, created).

    Identity is (gas_name, source_id), the same per-source rule crude_oil uses:
    two sources naming the same gas keep their own rows and their own figures.

    ONE CARGO SPELLED TWO WAYS IN ONE SOURCE IS ONE CARGO
    ------------------------------------------------------
    That per-source rule is about DIFFERENT sources disagreeing, and it must not
    be read as licence for a single source to hold the same cargo twice. The
    liquefied-gas compatibility workbook writes "Propylene Oxide" and
    "Vinyl Chloride" on one sheet and "Propyleneoxide" and "Vinylchloride" on
    another, and taking those at face value produced four cargo_gas rows for two
    cargoes - with the compatibility verdicts hanging off one row and the
    cleaning procedures off the other, so that neither answered a question about
    the cargo in full.

    So a name that is not found exactly is looked up again ignoring case,
    spaces and punctuation, WITHIN THIS SOURCE ONLY. A match means the source
    has spelled a cargo it already named a second way:

      * the existing row is reused - no second cargo_gas row;
      * the better-spelled form of the two is kept as gas_name;
      * the other spelling is recorded as a synonym, so the source's own
        wording survives and a lookup on either finds the cargo;
      * the merge is logged, because collapsing two names is a judgement and a
        reader has to be able to see that it happened.

    Deliberately scoped to one source. Across sources the spellings differ on
    purpose and merging them would destroy the per-source identity the whole
    branch is built on.
    """
    cur.execute("SELECT id FROM cargo_gas WHERE gas_name = %s AND source_id = %s",
                (gas_name, source_id))
    row = cur.fetchone()
    if row:
        return row[0], False

    key = gas_name_key(gas_name)
    if key:
        cur.execute(
            # Same rule as gas_name_key, expressed in SQL: whitespace and
            # hyphens only. The two must never drift apart.
            "SELECT id, gas_name FROM cargo_gas WHERE source_id = %s "
            "AND regexp_replace(lower(gas_name), '[\s\-]', '', 'g') = %s "
            "ORDER BY id",
            (source_id, key),
        )
        matches = cur.fetchall()
        if matches:
            gas_id, existing_name = matches[0]
            if len(matches) > 1:
                log.warning(
                    "cargo_gas already holds %d spellings of %r in this source "
                    "(%s); reusing id=%s. Merge the rest before relying on "
                    "them.", len(matches), gas_name,
                    ", ".join(f"id={i} {n!r}" for i, n in matches), gas_id)
            keep = _preferred_name(existing_name, gas_name)
            if keep != existing_name:
                cur.execute("UPDATE cargo_gas SET gas_name = %s, updated_at = now() "
                            "WHERE id = %s", (keep, gas_id))
            variant = gas_name if keep == existing_name else existing_name
            log.warning("one cargo, two spellings in this source: %r and %r. "
                        "Kept %r (id=%s); %r recorded as a synonym.",
                        existing_name, gas_name, keep, gas_id, variant)
            try:
                synonym_id, _ = upsert_synonym(cur, source_id, variant)
                link_synonym(cur, gas_id, synonym_id, source_id,
                             relationship_type="spelling_variant",
                             notes=f"The source also spells this cargo "
                                   f"{variant!r}; stored under {keep!r}. Same "
                                   f"cargo, two sheets, two typographies.")
            except Exception:
                # A source with no synonym tables in play must still load.
                log.exception("could not record %r as a synonym of %r",
                              variant, keep)
            return gas_id, False

    cur.execute(
        "INSERT INTO cargo_gas (gas_name, source_id, country_of_origin, "
        "created_at, updated_at) VALUES (%s, %s, %s, now(), now()) RETURNING id",
        (gas_name, source_id, country_of_origin),
    )
    return cur.fetchone()[0], True


def upsert_property(cur, cargo_gas_id: int, source_id: int, field_name: str,
                    value: Optional[str], normalized_value: Optional[float] = None,
                    normalized_min: Optional[float] = None,
                    normalized_max: Optional[float] = None,
                    unit: Optional[str] = None, value_type: str = "number",
                    entered_by: str = "gas_loader",
                    source_page_ref: Optional[str] = None,
                    notes: Optional[str] = None,
                    is_winning: bool = IS_WINNING,
                    conflict_flag: bool = CONFLICT_FLAG) -> None:
    """Insert or refresh one (gas, source, field) value.

    ON CONFLICT updates rather than skipping, so a re-run picks up corrections
    to the source file. Values from other sources are untouched - source_id is
    part of the key, so one source can never overwrite another's figure.

    is_winning / conflict_flag default to the branch convention (a loaded value
    is the answer, and nothing is in dispute). Pass is_winning=False with
    conflict_flag=True for a figure the source prints that CANNOT be served as
    the answer - one this database can show is impossible on the source's own
    evidence. Such a value is still stored, because the source did print it and
    `notes` says what is wrong with it; the flags are what stop a query from
    handing it back as a measurement. Storing nothing would hide the defect,
    and correcting the number would invent data.
    """
    cur.execute(
        """
        INSERT INTO cargo_gas_property_values
            (cargo_gas_id, source_id, field_name, value, normalized_value,
             normalized_min, normalized_max, unit, value_type, source_synonym_id,
             source_page_ref, as_of_date, entered_date, entered_by, entry_type,
             is_winning, conflict_flag, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, NULL,
                now(), %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (cargo_gas_id, source_id, field_name) DO UPDATE SET
            value            = EXCLUDED.value,
            normalized_value = EXCLUDED.normalized_value,
            normalized_min   = EXCLUDED.normalized_min,
            normalized_max   = EXCLUDED.normalized_max,
            unit             = EXCLUDED.unit,
            value_type       = EXCLUDED.value_type,
            source_page_ref  = EXCLUDED.source_page_ref,
            is_winning       = EXCLUDED.is_winning,
            conflict_flag    = EXCLUDED.conflict_flag,
            notes            = EXCLUDED.notes,
            updated_at       = now()
        """,
        (cargo_gas_id, source_id, field_name, value, normalized_value,
         normalized_min, normalized_max, unit, value_type, source_page_ref,
         entered_by, ENTRY_TYPE, is_winning, conflict_flag, notes),
    )



# ---------------------------------------------------------------------------
# Synonyms
# ---------------------------------------------------------------------------
# `synonyms` is owner-agnostic and shared by all three branches; cargo_gas
# reaches it through cargo_gas_synonym (prisma/migrations/
# 20260904000000_cargo_gas_synonym), the gas twin of cargo_synonym and
# crude_oil_synonym. These two helpers mirror the oil branch's so the three
# branches key and link names the same way.


def normalize_synonym(text: str) -> str:
    """Match master_loader.normalize_synonym so all branches key names alike."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def upsert_synonym(cur, source_id: int, text_value: str,
                   cache: Optional[Dict[str, int]] = None) -> Tuple[int, bool]:
    """The `synonyms` row for this text, reusing an existing one. (id, created).

    Keyed on normalized_text, so a gas source naming a cargo 'VCM' converges on
    the row the chemical branch already published for that text instead of
    storing it a second time - which is the whole point of a shared table.

    source_id lands on the row only when this call CREATES it: a reused row
    keeps the source that first published the name, so the column records
    origin rather than whoever ran last. Which source applied the name to a
    particular gas is recorded on the link instead - see link_synonym.
    """
    normalized = normalize_synonym(text_value)
    if cache is not None and normalized in cache:
        return cache[normalized], False

    cur.execute("SELECT id FROM synonyms WHERE normalized_text = %s ORDER BY id LIMIT 1",
                (normalized,))
    row = cur.fetchone()
    if row:
        if cache is not None:
            cache[normalized] = row[0]
        return row[0], False

    cur.execute(
        "INSERT INTO synonyms (synonym_text, normalized_text, source_id, "
        "date_added, created_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now(), now()) RETURNING id",
        (text_value, normalized, source_id),
    )
    sid = cur.fetchone()[0]
    if cache is not None:
        cache[normalized] = sid
    return sid, True


def link_synonym(cur, cargo_gas_id: int, synonym_id: int, source_id: int,
                 relationship_type: str, ambiguity_flag: bool = False,
                 notes: Optional[str] = None) -> None:
    """Insert or refresh one cargo_gas <-> synonym link.

    ON CONFLICT updates rather than skipping, so a re-run picks up a corrected
    relationship_type or an ambiguity that has appeared or gone away since the
    last load. Another source's link to the same name is untouched: the key is
    (cargo_gas_id, synonym_id) and cargo_gas rows are themselves per-source.
    """
    cur.execute(
        """
        INSERT INTO cargo_gas_synonym
            (cargo_gas_id, synonym_id, relationship_type, ambiguity_flag,
             source_id, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (cargo_gas_id, synonym_id) DO UPDATE SET
            relationship_type = EXCLUDED.relationship_type,
            ambiguity_flag    = EXCLUDED.ambiguity_flag,
            source_id         = EXCLUDED.source_id,
            notes             = EXCLUDED.notes,
            updated_at        = now()
        """,
        (cargo_gas_id, synonym_id, relationship_type, ambiguity_flag,
         source_id, notes),
    )
