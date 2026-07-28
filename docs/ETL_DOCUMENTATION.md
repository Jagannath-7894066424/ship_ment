# Chemical Cargo ETL - Multi-Format Implementation

## Overview

The updated `cargo_chemicals.py` is a **multi-format ETL loader** that supports two different file formats for importing chemical cargo data into the PostgreSQL database:

1. **LARS Format** - Legacy hierarchical format with parent-child relationships
2. **IBC Format** - Standard tabular format with independent records

The script **automatically detects** the file format based on column names and applies the appropriate transformation logic.

---

## Supported Formats

### LARS Format (Parent-Child Hierarchy)

**Source**: "Chemical Cargo specifications" spreadsheet  
**Characteristics**:
- Parent rows: Have canonical name + property fields
- Child rows: Have only synonym names (COMMODITIES column) 
- Hierarchical structure with indented synonyms

**Example Structure**:
```
Absolute alcohol                    <- Parent row
  Ethyl alcohol                     <- Child row (synonym)
  Alcohol                           <- Child row (synonym)  
  Ethanol                           <- Child row (synonym)
  Grain Alcohol                     <- Child row (synonym)
```

**LARS Column Mapping**:
```python
{
    "Unnamed: 0": "canonical_name",
    "COMMODITIES": "synonym_text",
    "SpGr": "density_g_cm3",
    "Temp": "reference_temperature_c",
    "Correction factor": "correction_factor",
    "Ship Type": "ship_type",
    "Tank Type": "tank_type",
    "Pollution cat": "ibc_pollution_category",
    "Compliance": "compliance",
    "USCG compat": "uscg_compatibility_group",
    "Boiling point": "boiling_point_c",
    "Melting point": "melting_point_c",
    "Flash point": "flash_point_c",
    "Heat adjacent": "heat_adjacent",
    "Heat req V": "heating_required_voyage",
    "Heat req D": "heating_required_discharge",
    "Colour": "colour",
    "Solubility": "water_solubility",
    "UnNr": "un_number",
    "Comments": "stowage_notes",
}
```

### IBC Format (Tabular)

**Source**: IBC regulations or similar standards  
**Characteristics**:
- All rows are independent cargo records
- No parent-child relationships
- No synonym handling

**IBC Column Mapping**:
```python
{
    "product_name": "canonical_name",
    "pollution_category": "ibc_pollution_category",
    "hazards": "hazards",
    "ship_type": "ship_type",
    "tank_type": "tank_type",
    "tank_vents": "tank_vents",
    "tank_environmental_control": "tank_environment_control",
    "electrical_equipment_temperature_class": "electrical_equipment_temperature_class",
    "electrical_equipment_apparatus_group": "electrical_equipment_apparatus_group",
    "flashpoint_requirement": "flashpoint_requirement",
    "gauging": "gauging",
    "vapour_detection": "vapour_detection",
    "fire_protection": "fire_protection",
    "emergency_equipment": "emergency_equipment",
}
```

---

## Key Features

### 1. Auto-Detection

The script detects the file format by checking for signature column names:

**LARS Detector**:
```python
{"Unnamed: 0", "COMMODITIES", "SpGr", "Temp"}  # Need ≥2 matches
```

**IBC Detector**:
```python
{"product_name", "pollution_category", "hazards"}  # Need ≥2 matches
```

### 2. Data Normalization

Every cell value is normalized before insertion:

**Normalization Steps**:
- Empty values → `None` (SQL NULL)
- NaN/NaT values → `None`
- Whitespace trimming
- Blank strings → `None`

**Type-Specific Coercion**:
```python
# Boolean: Y/N, Yes/No, TRUE/FALSE → True/False or None
# Integer: Extract leading digits (e.g., "12.5" → 12)
# Float: Extract numeric value (e.g., "12.5 m/s" → 12.5)
# Text: Return normalized string
```

### 3. Parent-Child Classification (LARS Only)

Rows are classified as **Parent** or **Child** based on content:

**Parent Row**:
- Non-empty canonical name (first column)
- May contain properties
- **Inserted** into `cargo_chemical` table

**Child Row**:
- Non-empty synonym (COMMODITIES)
- Empty canonical name
- **Extracted** for separate processing via `cargo_synonym.py`

### 4. Source Resolution

The source ID is resolved by matching the file name against the `source` table:

```python
File name: "Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx"
↓
Derived:   "Lars Stole Birkeland - Chemical Cargo specifications - 2002"
↓
Normalized: "lars stole birkeland chemical cargo specifications"
↓
Matched against: source.name (case-insensitive, year-agnostic)
```

### 5. Row Normalization to JSON

Each row is converted to a normalized JSON object:

```python
{
    "canonical_name": "Absolute alcohol",
    "density_g_cm3": 0.79,
    "boiling_point_c": 78,
    "melting_point_c": -114,
    "flash_point_c": 12,
    "un_number": "1170",
    "canonical_name_source_id": 5
}
```

**Features**:
- Keys match database column names (after mapping)
- Only non-None values included (empty omitted)
- Values coerced to database types
- Source ID stamped on every row

---

## ETL Workflow

### Step-by-Step Process

```
1. VALIDATE INPUT
   └─ Check file exists
   
2. LOAD & DETECT
   ├─ Read CSV/XLSX file
   ├─ Normalize column names
   └─ Auto-detect format (LARS or IBC)
   
3. CONNECT & SCHEMA
   ├─ Connect to PostgreSQL
   └─ Retrieve table schema & columns
   
4. RESOLVE SOURCE
   ├─ Derive source name from filename
   ├─ Lookup in source table
   └─ Exit if not found
   
5. PROCESS ROWS
   ├─ For each row:
   │  ├─ Classify as parent or child
   │  ├─ Normalize to JSON
   │  ├─ Validate key column
   │  └─ Collect or skip
   └─ Separate parents & children
   
6. DATABASE WRITE
   ├─ Optionally truncate table
   ├─ Batch insert parent rows
   ├─ Commit transaction
   └─ Log results
   
7. COMPLETE
   └─ Close connection & report stats
```

---

## Usage

### Basic Usage

```bash
# Load LARS format file (auto-detect)
python3 cargo_chemicals.py /path/to/lars_file.xlsx

# Load IBC format file
python3 cargo_chemicals.py /path/to/ibc_file.csv

# Specify sheet (Excel only)
python3 cargo_chemicals.py data/cargo.xlsx --sheet "Sheet2"

# Preview without writing
python3 cargo_chemicals.py data/cargo.xlsx --dry-run

# Clear and reload
python3 cargo_chemicals.py data/cargo.xlsx --truncate

# Use default file
python3 cargo_chemicals.py
```

### Command Options

```
positional arguments:
  file                 Path to .csv or .xlsx file

options:
  --table TABLE        Target table name (default: cargo_chemical)
  --sheet SHEET        Excel sheet name (Excel only)
  --truncate           Empty table before loading
  --dry-run            Parse & validate, don't write to DB
```

---

## Output & Logging

The script produces detailed logging output:

```
[08:45:23] [INFO] ================================================================================
[08:45:23] [INFO] CHEMICAL CARGO ETL LOADER - Starting
[08:45:23] [INFO] ================================================================================
[08:45:23] [INFO] file=data/cargo.xlsx table=cargo_chemical sheet=None truncate=False dry_run=False
[08:45:23] [INFO] Checking that file exists: data/cargo.xlsx
[08:45:23] [INFO] ✓ File exists
[08:45:23] [INFO] Reading input file...
[08:45:24] [INFO] ✓ Loaded 150 rows, 18 columns
[08:45:24] [INFO] Detecting file format based on column names...
[08:45:24] [INFO] ✓ Format: lars
[08:45:24] [INFO]   - Has parent-child relationships: True
[08:45:24] [INFO]   - Synonym column: COMMODITIES
[08:45:24] [INFO] Connecting to the database...
[08:45:25] [INFO] ✓ Database connection established
[08:45:25] [INFO] Querying schema for columns of table 'cargo_chemical'
[08:45:25] [INFO] Table 'cargo_chemical' has 52 columns: [...]
[08:45:25] [INFO] ✓ Retrieved 52 columns from cargo_chemical
[08:45:25] [INFO] Resolving source from file name: 'Lars Stole Birkeland - Chemical Cargo...'
[08:45:25] [INFO] Looking up source by normalized name: 'lars stole birkeland chemical cargo...'
[08:45:25] [INFO] Matched source id=5 ('Lars Stole Birkeland - Chemical Cargo specifications - 2002')
[08:45:25] [INFO] ✓ Source matched: id=5
[08:45:25] [INFO] ================================================================================
[08:45:25] [INFO] PROCESSING ROWS
[08:45:25] [INFO] ================================================================================
[08:45:25] [INFO] ✓ row 2 PARENT: {"canonical_name": "Absolute alcohol", "density_g_cm3": 0.79, ...}
[08:45:25] [INFO] ✓ row 3 CHILD (SYNONYM): {"synonym_text": "Ethyl alcohol"}
[08:45:25] [INFO] ✓ row 4 CHILD (SYNONYM): {"synonym_text": "Alcohol"}
...
[08:45:26] [INFO] ================================================================================
[08:45:26] [INFO] PROCESSING COMPLETE
[08:45:26] [INFO] ================================================================================
[08:45:26] [INFO] Prepared 47 parent rows (inserts to cargo_chemical)
[08:45:26] [INFO] Prepared 103 child rows (synonyms for separate processing)
[08:45:26] [INFO] Skipped 0 rows
[08:45:26] [INFO] ================================================================================
[08:45:26] [INFO] INSERTING INTO DATABASE
[08:45:26] [INFO] ================================================================================
[08:45:26] [INFO] Columns to insert: [52 columns...]
[08:45:26] [INFO] Building INSERT statement for 52 columns, 47 rows
[08:45:26] [INFO] Executing batch insert...
[08:45:26] [INFO] ✓ Batch insert completed
[08:45:26] [INFO] Committing transaction...
[08:45:26] [INFO] ✓ Committed
[08:45:26] [INFO] Inserted 47 rows into 'cargo_chemical'
[08:45:26] [INFO] Note: 103 synonyms extracted. Run cargo_synonym.py to link them.
[08:45:26] [INFO] ================================================================================
[08:45:26] [INFO] ETL COMPLETE
[08:45:26] [INFO] ================================================================================
```

---

## Data Type Conversions

### Boolean Handling

```python
TRUE_SET = {"true", "t", "yes", "y", "1"}
FALSE_SET = {"false", "f", "no", "n", "0"}

Examples:
"Y" → True
"No" → False
"TRUE" → True
"n" → False
"invalid" → None (logs warning, doesn't fail)
```

### Numeric Handling

```python
Integer:    "12.5 kg" → 12
Float:      "12.5 kg" → 12.5
Invalid:    "abc" → None

Regex extraction:
Integer:    r"-?\d+"
Float:      r"-?\d+(?:\.\d+)?"
```

### Empty Values

```python
None → None
nan → None
"" → None
"   " → None
```

---

## Database Operations

### Transaction Management

```python
try:
    with conn.cursor() as cur:
        # Detect format
        # Process rows
        # Build INSERT
        execute_values(cur, insert, rows)
        conn.commit()  # ← Success
except Exception:
    conn.rollback()  # ← Rollback on error
    raise
finally:
    conn.close()
```

### Batch Insert (Efficient)

```python
from psycopg2.extras import execute_values

execute_values(cur, insert_stmt, row_tuples, page_size=1000)
```

---

## Post-Processing (Synonyms)

### For LARS Format Only

Child rows (synonyms) are extracted but NOT inserted directly. Instead:

1. **Synonyms** are inserted via `import_synonyms.py`
2. **Links** are created via `cargo_synonym.py`

This maintains referential integrity and allows deduplication of synonyms across multiple cargoes.

**Workflow**:
```bash
# Step 1: Load cargo data (this script)
python3 cargo_chemicals.py data/lars.xlsx

# Step 2: Import synonyms
python3 import_synonyms.py data/lars.xlsx

# Step 3: Link cargoes to synonyms
python3 cargo_synonym.py data/lars.xlsx
```

---

## Architecture Decisions

### Why Separate Files?

1. **Modularity**: Each script has single responsibility
2. **Idempotency**: Each can run independently  
3. **Deduplication**: Synonyms shared across cargoes
4. **Flexibility**: Can add new sources without reprocessing

### Why No Direct Prisma ORM?

1. **Batch Performance**: Direct SQL is 10-100× faster
2. **Flexibility**: Dynamic columns based on schema
3. **Transaction Control**: Lower-level control via psycopg2
4. **Existing Architecture**: Maintains compatibility

---

## Error Handling

### Validation

- File exists check
- Column detection required
- Source resolution required
- Key column (canonical_name) validation per row

### Recovery

- Database errors roll back entire transaction
- Dry-run mode for safe validation
- Detailed logging of all skipped rows
- Preserves database consistency

### Warnings (Non-fatal)

```
✗ row 5 SKIP (empty canonical_name)
Warning: Cannot interpret '???' as boolean → storing NULL
Ignoring columns not in 'cargo_chemical': [list]
Source 'Unknown' not found → skipping load
```

---

## Performance

### Batch Insertion

- Uses `execute_values()` with page_size=1000
- Typical rate: 1000-5000 rows/second
- Memory efficient streaming

### Type Coercion

- Per-column type conversion
- Regex-based numeric extraction
- Early validation during processing phase

---

## Troubleshooting

### Format Not Detected

```
Error: Could not detect file format. Expected LARS [columns...] or IBC [columns...]
```

**Solution**: Verify file has required columns:
- LARS: Needs at least 2 of {Unnamed: 0, COMMODITIES, SpGr, Temp}
- IBC: Needs at least 2 of {product_name, pollution_category, hazards}

### Source Not Found

```
Source 'Unknown Source' not found in source table → skipping load.
```

**Solution**: 
1. Check `source` table has matching name
2. Or fuzzy match logic (year-agnostic, case-insensitive)
3. Add source record if missing

### Column Mismatch

```
Error: none of the file's columns match the table.
```

**Solution**:
1. Check file column names vs mapping dictionaries
2. Update mappings if new format detected
3. Use `--dry-run` to preview without error

### No Rows Inserted

```
No rows to insert.
```

**Solution**:
1. Check dry-run output to verify row processing
2. Verify source_id was resolved
3. Check for all-empty key column (canonical_name)

---

## Code Structure

### Module Functions

| Function | Purpose |
|----------|---------|
| `read_file()` | Load CSV/XLSX into DataFrame |
| `detect_format()` | Auto-detect LARS vs IBC |
| `get_table_columns()` | Query database schema |
| `derive_source_name()` | Extract name from filename |
| `normalize_source_name()` | Fuzzy-match logic |
| `get_source_id()` | Look up source in DB |
| `normalize_value()` | Handle empties & NaN |
| `coerce_value()` | Convert to target type |
| `is_parent_row()` | Classify parent vs child |
| `normalize_row_to_json()` | Convert to database JSON |
| `extract_synonyms_from_row()` | Extract child synonyms |
| `main()` | Orchestrate ETL workflow |

---

## Testing

### Dry-Run Mode

```bash
# Verify without writing
python3 cargo_chemicals.py data/cargo.xlsx --dry-run

# Check output in logs for:
# - Correct format detection
# - Expected row counts  
# - Data normalization correctness
# - Source ID resolution
```

### Test with Sample Data

```bash
# Create small test file (10 rows)
# Run with --dry-run first
python3 cargo_chemicals.py test_data.xlsx --dry-run

# Run with --truncate to clear old data
python3 cargo_chemicals.py test_data.xlsx --truncate

# Verify in database
psql -U user -d db -c "SELECT COUNT(*) FROM cargo_chemical;"
```

---

## Maintenance

### Adding New Format

To support a new format:

1. Add mapping dictionary: `NEW_FORMAT_MAPPING`
2. Add detector columns: `NEW_FORMAT_DETECTOR`  
3. Add config to `FILE_FORMAT_CONFIG`
4. Update detection logic in `detect_format()`
5. Test with sample file

### Updating Mappings

```python
# If new column added to source file:
LARS_MAPPING["New Column Name"] = "database_column_name"

# If mapping changed:
LARS_MAPPING["Old Name"] = "new_db_column_name"  # Update existing
```

---

## Summary

The multi-format ETL loader:

✓ Automatically detects file format (LARS or IBC)  
✓ Normalizes all data (NaN, Y/N, numeric, whitespace)  
✓ Classifies parent/child rows (LARS only)  
✓ Maintains transaction integrity  
✓ Provides detailed logging  
✓ Supports dry-run validation  
✓ Handles 1000s of rows efficiently  
✓ Integrates with existing Python ETL scripts  

