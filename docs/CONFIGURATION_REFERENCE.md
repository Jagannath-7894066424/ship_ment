# Configuration & Mappings Reference

## LARS Format Configuration

### Column Mapping (File → Database)

| File Column Name | Database Column | Type | Notes |
|------------------|-----------------|------|-------|
| `Unnamed: 0` | `canonical_name` | TEXT | Parent chemical name (first column) |
| `COMMODITIES` | `synonym_text` | TEXT | Synonym names (child rows) |
| `SpGr` | `density_g_cm3` | FLOAT | Specific gravity at reference temp |
| `Temp` | `reference_temperature_c` | FLOAT | Reference temperature for measurements |
| `Correction factor` | `correction_factor` | FLOAT | Density correction factor |
| `Ship Type` | `ship_type` | INT | Ship classification |
| `Tank Type` | `tank_type` | TEXT | Tank material/type |
| `Pollution cat` | `ibc_pollution_category` | TEXT | IBC pollution category |
| `Compliance` | `compliance` | TEXT | Regulatory compliance info |
| `USCG compat` | `uscg_compatibility_group` | TEXT | USCG compatibility group |
| `Boiling point` | `boiling_point_c` | FLOAT | Boiling point (°C) |
| `Melting point` | `melting_point_c` | FLOAT | Melting point (°C) |
| `Flash point` | `flash_point_c` | FLOAT | Flash point (°C) |
| `Heat adjacent` | `heat_adjacent` | BOOLEAN | Heating required adjacent to tank |
| `Heat req V` | `heating_required_voyage` | BOOLEAN | Heating required during voyage |
| `Heat req D` | `heating_required_discharge` | BOOLEAN | Heating required for discharge |
| `Colour` | `colour` | TEXT | Product color |
| `Solubility` | `water_solubility` | TEXT | Water solubility description |
| `UnNr` | `un_number` | TEXT | UN number (e.g., "1170") |
| `Comments` | `stowage_notes` | TEXT | Stowage and handling notes |

### LARS Format Detection

```python
LARS_DETECTOR = {"Unnamed: 0", "COMMODITIES", "SpGr", "Temp"}
# Requires ≥ 2 matches (usually gets all 4)
```

### LARS Configuration

```python
FILE_FORMAT_CONFIG["lars"] = {
    "mapping": LARS_MAPPING,                    # 20 field mappings
    "detector": LARS_DETECTOR,                  # 4 signature columns
    "key_column": "canonical_name",             # Primary identifier
    "has_parent_child": True,                   # Hierarchy support
    "synonym_column": "COMMODITIES",            # Child row indicator
    "parent_column": "Unnamed: 0",              # Parent row indicator
}
```

### LARS Row Classification Logic

```
Parent Row (INSERT to cargo_chemical):
  ├─ Non-empty "Unnamed: 0" (canonical_name)
  └─ May have properties (SpGr, Temp, etc.)

Child Row (EXTRACT for synonyms):
  ├─ Empty "Unnamed: 0"
  ├─ Non-empty "COMMODITIES"
  └─ No property fields
```

### Example LARS Data Structure

```
Row  Unnamed: 0           COMMODITIES         SpGr   UnNr
2    Absolute alcohol                         0.79   1170    ← PARENT
3                         Ethyl alcohol                        ← CHILD
4                         Alcohol                              ← CHILD
5                         Ethanol                              ← CHILD
6                         Grain Alcohol                        ← CHILD
7    Acetone                                  0.78   1090    ← PARENT
8                         Dimethyl ketone                      ← CHILD
```

---

## IBC Format Configuration

### Column Mapping (File → Database)

| File Column Name | Database Column | Type | Notes |
|------------------|-----------------|------|-------|
| `product_name` | `canonical_name` | TEXT | Product/chemical name |
| `pollution_category` | `ibc_pollution_category` | TEXT | IBC pollution category |
| `hazards` | `hazards` | TEXT | Hazard description |
| `ship_type` | `ship_type` | INT | Ship classification |
| `tank_type` | `tank_type` | TEXT | Tank type/material |
| `tank_vents` | `tank_vents` | TEXT | Tank vent requirements |
| `tank_environmental_control` | `tank_environment_control` | TEXT | Environmental control |
| `electrical_equipment_temperature_class` | `electrical_equipment_temperature_class` | TEXT | Temperature class |
| `electrical_equipment_apparatus_group` | `electrical_equipment_apparatus_group` | TEXT | Apparatus group |
| `flashpoint_requirement` | `flashpoint_requirement` | TEXT | Flash point requirement |
| `gauging` | `gauging` | TEXT | Gauging method |
| `vapour_detection` | `vapour_detection` | TEXT | Vapor detection equipment |
| `fire_protection` | `fire_protection` | TEXT | Fire protection equipment |
| `emergency_equipment` | `emergency_equipment` | BOOLEAN | Emergency equipment required |

### IBC Format Detection

```python
IBC_DETECTOR = {"product_name", "pollution_category", "hazards"}
# Requires ≥ 2 matches (usually gets all 3)
```

### IBC Configuration

```python
FILE_FORMAT_CONFIG["ibc"] = {
    "mapping": IBC_MAPPING,                     # 14 field mappings
    "detector": IBC_DETECTOR,                   # 3 signature columns
    "key_column": "canonical_name",             # Primary identifier
    "has_parent_child": False,                  # No hierarchy
    "synonym_column": None,                     # No synonyms
    "parent_column": None,                      # All rows are parents
}
```

### IBC Row Classification

```
All Rows = PARENT (INSERT to cargo_chemical)
  └─ No child/synonym processing
  └─ Each row is independent
```

### Example IBC Data Structure

```
Row  product_name        pollution_category    hazards
2    Mineral Oil          Category Y            None        ← INDEPENDENT
3    Crude Oil            Category X            Flammable   ← INDEPENDENT
4    Toluene              Category Y            Toxic       ← INDEPENDENT
```

---

## Format Detection Flow

```
File Loaded
    ↓
Extract column names
    ↓
Calculate matches:
├─ LARS matches = count of columns in LARS_DETECTOR
└─ IBC matches = count of columns in IBC_DETECTOR
    ↓
Decision tree:
├─ If LARS matches ≥ 2 → Format = "LARS"
├─ Else if IBC matches ≥ 2 → Format = "IBC"
└─ Else → Raise ValueError (format unknown)
    ↓
Apply format-specific config
```

---

## Data Type Coercion Rules

### Boolean Field Coercion

```
Input String          Coerced Value    Notes
"Y"                   True
"y"                   True
"yes"                 True             Case-insensitive
"YES"                 True
"true"                True
"True"                True
"t"                   True
"1"                   True
---
"N"                   False
"n"                   False
"no"                  False             Case-insensitive
"NO"                  False
"false"               False
"False"               False
"f"                   False
"0"                   False
---
"invalid"             None              Logged as warning
""                    None
NULL                  None
```

### Integer Field Coercion

```
Input String          Regex Match       Result    Notes
"12"                  r"-?\d+"         12
"123"                 r"-?\d+"         123
"-5"                  r"-?\d+"         -5        Negative
"12.7"                r"-?\d+"         12        Truncates
"12 kg"               r"-?\d+"         12        Extracts leading
"Speed: 50 m/s"       r"-?\d+"         50        Extracts
"abc"                 (no match)       None      Invalid
""                    (no match)       None
NULL                  (no match)       None
```

### Float Field Coercion

```
Input String          Regex Match              Result    Notes
"12.5"                r"-?\d+(?:\.\d+)?"      12.5
"123.456"             r"-?\d+(?:\.\d+)?"      123.456
"-5.3"                r"-?\d+(?:\.\d+)?"      -5.3      Negative
"12"                  r"-?\d+(?:\.\d+)?"      12.0      Integer
"12.5 kg"             r"-?\d+(?:\.\d+)?"      12.5      Extracts
"Density: 0.79"       r"-?\d+(?:\.\d+)?"      0.79      Extracts
"abc"                 (no match)              None      Invalid
""                    (no match)              None
NULL                  (no match)              None
```

### Text Field Coercion

```
Input Value           Result            Notes
"value"               "value"
"  value  "           "value"           Whitespace trimmed
" "                   None              Blank strings → NULL
""                    None              Empty strings → NULL
NULL                  None              NULL preserved
NaN                   None              pandas NaN → NULL
```

---

## Source Resolution Rules

### Source Name Derivation

```
File Path: /data/Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx
    ↓
Base Name: "Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx"
    ↓
Remove Extensions (.xlsx, .xls, .csv):
    "Lars Stole Birkeland - Chemical Cargo specifications - 2002"
    ↓
Use as source name
```

### Source Name Normalization (Fuzzy Matching)

```
Original Source Name:
  "Lars Stole Birkeland - Chemical Cargo specifications - 2002"
    ↓
Normalization Steps:
  1. Lowercase
     "lars stole birkeland - chemical cargo specifications - 2002"
  2. Unify dashes (em/en dash → hyphen)
     "lars stole birkeland - chemical cargo specifications - 2002"
  3. Remove trailing year
     "lars stole birkeland - chemical cargo specifications"
  4. Collapse whitespace
     "lars stole birkeland chemical cargo specifications"
    ↓
Normalized Form:
  "lars stole birkeland chemical cargo specifications"
    ↓
Lookup in database source table:
  SELECT id FROM source WHERE NORMALIZED(name) = target
    ↓
If Match → Use source.id
If No Match → Exit with warning
```

---

## Configuration Constants

### Boolean Sets

```python
TRUE_SET = {"true", "t", "yes", "y", "1"}
FALSE_SET = {"false", "f", "no", "n", "0"}
```

### Numeric Regex Patterns

```python
# Integer extraction
Integer Pattern: r"-?\d+"
Examples:
  "12" → "12"
  "12.5" → "12"
  "-5" → "-5"
  "12 kg" → "12"

# Float extraction  
Float Pattern: r"-?\d+(?:\.\d+)?"
Examples:
  "12.5" → "12.5"
  "123" → "123"
  "-5.3" → "-5.3"
  "12.5 kg" → "12.5"
```

### Column Configuration

```python
SOURCE_ID_COLUMN = "canonical_name_source_id"
# Stamped on every inserted row from source lookup
```

---

## Processing Configuration

### Default Command-Line Arguments

```python
DEFAULT_FILE = "/home/lap044/Downloads/Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv"
DEFAULT_TABLE = "cargo_chemical"

# Optional Arguments
--table         Default: "cargo_chemical"
--sheet         Default: None (first sheet)
--truncate      Default: False
--dry-run       Default: False
```

### Database Configuration

```python
# Read from .env file
DATABASE_URL = os.getenv("DATABASE_URL")
# Format: postgresql://user:password@host:port/database

# Table configuration
TARGET_TABLE = "cargo_chemical"

# Batch insert configuration
page_size = 1000  # Records per batch
```

---

## Processing Configuration Summary

| Setting | Value | Purpose |
|---------|-------|---------|
| `LARS_DETECTOR` | 4 columns | Identify LARS format files |
| `IBC_DETECTOR` | 3 columns | Identify IBC format files |
| `LARS_MAPPING` | 20 mappings | Column renaming for LARS |
| `IBC_MAPPING` | 14 mappings | Column renaming for IBC |
| `TRUE_SET` | 5 strings | Boolean true recognition |
| `FALSE_SET` | 5 strings | Boolean false recognition |
| `SOURCE_ID_COLUMN` | `canonical_name_source_id` | Source tracking |
| `page_size` | 1000 | Batch insert size |

---

## Format Comparison Matrix

| Aspect | LARS | IBC |
|--------|------|-----|
| **Hierarchy** | Parent-Child | Flat |
| **Synonyms** | Yes (child rows) | No |
| **Key Column** | `Unnamed: 0` | `product_name` |
| **Mappings** | 20 fields | 14 fields |
| **Detector Columns** | 4 | 3 |
| **Row Processing** | Classify | All same |
| **Child Handling** | Extract | N/A |
| **Synonym Processing** | Separate scripts | N/A |
| **Typical Source** | Legacy specs | Regulations |

---

## Workflow Configuration Summary

### LARS Workflow

```
Input: LARS Excel file
    ↓ [1] Detect: LARS format
    ↓ [2] Process: Classify parent/child
    ↓ [3] Apply: LARS_MAPPING
    ↓ [4] Insert: Parent rows to cargo_chemical
    ↓ [5] Extract: Child rows (synonyms)
Output: 
  - X rows in cargo_chemical (parents)
  - Y rows for import_synonyms.py (children)
```

### IBC Workflow

```
Input: IBC CSV file
    ↓ [1] Detect: IBC format
    ↓ [2] Process: All rows same (no classification)
    ↓ [3] Apply: IBC_MAPPING
    ↓ [4] Insert: All rows to cargo_chemical
    ↓ [5] Complete: No synonym processing
Output:
  - X rows in cargo_chemical (all independent)
```

---

## Configuration for Future Extensions

### To Add New Format (e.g., "CUSTOM")

```python
# Step 1: Create mapping
CUSTOM_MAPPING = {
    "source_col": "database_col",
    # ... all field mappings
}

# Step 2: Create detector
CUSTOM_DETECTOR = {"column1", "column2"}  # Signature columns

# Step 3: Add configuration
FILE_FORMAT_CONFIG["custom"] = {
    "mapping": CUSTOM_MAPPING,
    "detector": CUSTOM_DETECTOR,
    "key_column": "canonical_name",
    "has_parent_child": False,  # Set based on format
    "synonym_column": None,      # Set if applicable
    "parent_column": None,       # Set if applicable
}

# Step 4: Update detect_format()
if len(custom_matches) >= 2:
    return "custom", FILE_FORMAT_CONFIG["custom"]
```

---

## Summary

✅ **LARS Format**
- 20 field mappings
- Parent-child hierarchy
- Synonym extraction
- Fuzzy source matching

✅ **IBC Format**
- 14 field mappings
- Independent records
- No synonyms
- Direct database insertion

✅ **Auto-Detection**
- Signature column matching
- Requires ≥2 matches
- Case-insensitive

✅ **Data Normalization**
- Boolean: Y/N → True/False
- Integer: Extract from strings
- Float: Regex-based extraction
- Text: Trim whitespace
- Empty: Convert to NULL

✅ **Production Ready**
- Configurable
- Extensible
- Well-documented
- Thoroughly tested

