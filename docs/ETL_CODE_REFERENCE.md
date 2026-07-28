# Multi-Format ETL - Code Reference

## Quick Reference Guide

### 1. Column Mappings

**LARS Format (Legacy with parent-child)**
```python
LARS_MAPPING = {
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

**IBC Format (Standard tabular)**
```python
IBC_MAPPING = {
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

### 2. Format Detection

```python
# File format is auto-detected based on column names
LARS_DETECTOR = {"Unnamed: 0", "COMMODITIES", "SpGr", "Temp"}
IBC_DETECTOR = {"product_name", "pollution_category", "hazards"}

# Detection requires ≥2 matches from detector set
file_format, format_config = detect_format(df.columns)
# Returns: ("lars" or "ibc", CONFIG_DICT)
```

---

### 3. Data Normalization

**Step 1: Normalize (clean empty values)**
```python
def normalize_value(value):
    if value is None or pd.isna(value):
        return None
    s = str(value).strip()
    if s == "":
        return None
    return s
```

**Step 2: Coerce to Type**
```python
def coerce_value(value, data_type):
    norm = normalize_value(value)
    if norm is None:
        return None
    
    if data_type == "boolean":
        return True if norm.lower() in TRUE_SET else False
    
    if data_type in ("integer", "bigint"):
        m = re.search(r"-?\d+", norm)
        return int(m.group()) if m else None
    
    if data_type in ("numeric", "double precision"):
        m = re.search(r"-?\d+(?:\.\d+)?", norm)
        return float(m.group()) if m else None
    
    return norm  # text/varchar/etc
```

---

### 4. Row Classification (Parent vs Child)

**LARS Format Only**
```python
def is_parent_row(record, format_config):
    """Determine if row is parent (cargo) or child (synonym)"""
    
    parent_col = format_config.get("parent_column")  # "Unnamed: 0"
    synonym_col = format_config.get("synonym_column")  # "COMMODITIES"
    
    parent_name = str(record.get(parent_col, "")).strip()
    synonym_text = str(record.get(synonym_col, "")).strip()
    
    # Parent: has name + properties
    if parent_name and not synonym_text:
        return True
    
    # Child: has synonym only
    if synonym_text and not parent_name:
        return False
    
    # Both: treat as parent
    if parent_name and synonym_text:
        return True
    
    return True  # Neither: empty row
```

---

### 5. Row to JSON Normalization

```python
def normalize_row_to_json(record, format_config, table_cols):
    """Convert DataFrame row to normalized JSON object"""
    
    mapping = format_config.get("mapping")
    result = {}
    
    for file_col, file_val in record.items():
        # Map column name
        db_col = mapping.get(file_col, file_col)
        
        # Skip unknown columns
        if db_col not in table_cols:
            continue
        
        # Skip synonym column (handled separately)
        if file_col == format_config.get("synonym_column"):
            continue
        
        # Normalize & coerce value
        data_type = table_cols[db_col]
        coerced = coerce_value(file_val, data_type)
        
        # Only include non-None values
        if coerced is not None:
            result[db_col] = coerced
    
    return result if result else None
```

**Example Output:**
```python
{
    "canonical_name": "Absolute alcohol",
    "density_g_cm3": 0.79,
    "boiling_point_c": 78,
    "flash_point_c": 12,
    "un_number": "1170",
    "canonical_name_source_id": 5
}
```

---

### 6. Synonym Extraction (LARS)

```python
def extract_synonyms_from_row(record, format_config):
    """Extract synonym names from row (LARS only)"""
    
    if not format_config.get("has_parent_child"):
        return []  # IBC: no synonyms
    
    synonym_col = format_config.get("synonym_column")
    synonym_text = str(record.get(synonym_col, "")).strip()
    
    if not synonym_text:
        return []
    
    return [{"synonym_text": synonym_text}]
```

---

### 7. Main Workflow

**High-level steps:**
```python
# 1. Read & detect format
df = read_file(path, sheet)
file_format, config = detect_format(df.columns)

# 2. Connect & get schema
conn = psycopg2.connect(db_url)
table_cols = get_table_columns(cur, table_name)

# 3. Resolve source
source_id = get_source_id(cur, derive_source_name(path))

# 4. Process rows
parent_rows = []
child_rows = []
for idx, record in df.iterrows():
    if is_parent_row(record, config):
        normalized = normalize_row_to_json(record, config, table_cols)
        parent_rows.append(normalized)
    else:
        child_rows.extend(extract_synonyms_from_row(record, config))

# 5. Insert to database
if parent_rows:
    # Convert dicts to tuples
    db_cols = sorted(set().union(*[r.keys() for r in parent_rows]))
    row_tuples = [tuple(r.get(c) for c in db_cols) for r in parent_rows]
    
    insert = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(c) for c in db_cols)
    )
    execute_values(cur, insert, row_tuples)
    conn.commit()
```

---

### 8. Boolean & Numeric Conversions

```python
# Boolean conversions
TRUE_SET = {"true", "t", "yes", "y", "1"}
FALSE_SET = {"false", "f", "no", "n", "0"}

# Usage:
"Y" → True
"n" → False
"TRUE" → True
"no" → False
"invalid" → None (logs warning)

# Numeric conversions (regex extraction)
Integer:
  "12.5 kg" → 12
  Pattern: r"-?\d+"

Float:
  "12.5 kg" → 12.5
  Pattern: r"-?\d+(?:\.\d+)?"
```

---

### 9. Error Handling

```python
try:
    # Load & process
    ...
except ValueError as e:
    log.error("Format detection failed: %s", e)
    sys.exit(str(e))
except Exception as e:
    log.exception("ETL failed - rolling back transaction")
    conn.rollback()
    raise
finally:
    conn.close()
```

---

### 10. Usage Examples

**Basic**
```bash
python3 cargo_chemicals.py /path/to/file.xlsx
```

**Specify sheet**
```bash
python3 cargo_chemicals.py data/cargo.xlsx --sheet "Sheet2"
```

**Dry-run (no database writes)**
```bash
python3 cargo_chemicals.py data/cargo.xlsx --dry-run
```

**Clear and reload**
```bash
python3 cargo_chemicals.py data/cargo.xlsx --truncate
```

**Custom target table**
```bash
python3 cargo_chemicals.py data/file.csv --table my_table
```

---

### 11. Logging Output

Sample log output showing processing:
```
[INFO] ✓ Format detected: LARS (parent-child with synonyms)
[INFO] ✓ Retrieved 52 columns from cargo_chemical
[INFO] ✓ Source matched: id=5
[INFO] ✓ row 2 PARENT: {"canonical_name": "Absolute alcohol", ...}
[INFO] ✓ row 3 CHILD (SYNONYM): {"synonym_text": "Ethyl alcohol"}
[INFO] Prepared 47 parent rows (inserts to cargo_chemical)
[INFO] Prepared 103 child rows (synonyms for separate processing)
[INFO] Inserted 47 rows into 'cargo_chemical'
```

---

### 12. Integration with Other Scripts

**Complete workflow:**
```bash
# 1. Load cargo data
python3 cargo_chemicals.py data/lars.xlsx

# 2. Import synonyms
python3 import_synonyms.py data/lars.xlsx

# 3. Link synonyms to cargoes
python3 cargo_synonym.py data/lars.xlsx

# 4. (Optional) Import field definitions
python3 field_definition.py
```

---

### 13. Database Schema (Relevant Fields)

**cargo_chemical table:**
```
id                      INT PRIMARY KEY
canonical_name          TEXT (parent chemical name)
canonical_name_source_id INT (foreign key → source.id)
density_g_cm3           FLOAT
boiling_point_c         FLOAT
melting_point_c         FLOAT
flash_point_c           FLOAT
un_number               TEXT
water_solubility        TEXT
... (50+ additional fields)
```

---

### 14. Configuration Structure

```python
FILE_FORMAT_CONFIG = {
    "lars": {
        "mapping": LARS_MAPPING,
        "detector": LARS_DETECTOR,
        "key_column": "canonical_name",
        "has_parent_child": True,
        "synonym_column": "COMMODITIES",
        "parent_column": "Unnamed: 0",
    },
    "ibc": {
        "mapping": IBC_MAPPING,
        "detector": IBC_DETECTOR,
        "key_column": "canonical_name",
        "has_parent_child": False,
        "synonym_column": None,
        "parent_column": None,
    },
}
```

---

## Testing Checklist

- [ ] Format detection works for LARS files
- [ ] Format detection works for IBC files
- [ ] Parent/child classification works correctly
- [ ] Data normalization handles NaN/empty values
- [ ] Boolean conversions work (Y/N, TRUE/FALSE)
- [ ] Numeric conversions work (integers & floats)
- [ ] Source ID resolution works
- [ ] Dry-run mode prevents database writes
- [ ] Database inserts succeed with proper types
- [ ] Transaction rollback on error
- [ ] Logging shows correct row counts
- [ ] Synonym extraction works for LARS

---

