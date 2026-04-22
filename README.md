# CeraTyOnt → SKOS Terminology

Python pipeline that converts the CeraTyOnt CSV lookup tables into a
SKOS-compliant Turtle terminology and validates it with SHACL.

## Project layout

```
root/
├── data/                          # input CSVs
│   ├── tbllookupformsgeneric.csv
│   ├── tbllookupformstradition.csv
│   ├── tbllookupformsservices.csv
│   ├── tbllookuppublisher.csv
│   └── v_ceratyont_potforms_distinct.csv
├── py/
│   ├── build_skos.py              # CSV → SKOS Turtle
│   ├── validate_skos.py           # loads Turtle + runs SHACL
│   ├── config.yaml                # all paths, URIs, column mappings
│   └── shapes/
│       └── ceratyont_shapes.ttl   # SHACL shapes
├── output/
│   ├── ceratyont_skos.ttl         # generated terminology
│   └── validation_report.ttl      # SHACL report
└── requirements.txt
```

## Setup (Windows + VS Code)

Open a PowerShell terminal in the project root and run:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks `Activate.ps1` with an execution-policy error:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

In VS Code, open the command palette (`Ctrl+Shift+P`) → *Python: Select
Interpreter* → pick `.venv`.

## Usage

Run **from the `py/` folder** (so relative paths in `config.yaml` resolve):

```powershell
cd py

# 1) Build SKOS Turtle
python build_skos.py

# 2) Validate it
python validate_skos.py
```

Flags:

- `--verbose` / `-v` — debug logging
- `--config path/to/other.yaml` — override config
- `--strict` (validate only) — exit non-zero if SHACL finds issues

## What gets built

| Input table                       | Rows | Modelled as                                                           |
|-----------------------------------|-----:|-----------------------------------------------------------------------|
| tbllookupformsgeneric.csv         |   32 | skos:Concept (top concepts, member of *Generic Potforms* collection)  |
| tbllookupformstradition.csv       |    3 | skos:Concept (top concepts, member of *Traditions* collection)        |
| tbllookupformsservices.csv        |   10 | skos:Concept (top concepts, member of *Services* collection)          |
| tbllookuppublisher.csv            |   15 | skos:Concept (top concepts, member of *Publishers* collection)        |
| v_ceratyont_potforms_distinct.csv |  866 | skos:Concept, member of *Potforms* collection, `skos:broader` → Publisher, `foaf:depiction` → image URL |

All concepts live in one `skos:ConceptScheme` at
`http://www.w3id.org/archlink/terms/ceratyont-terminology/scheme`.

## Handling of NULL values

- `publisher = "NULL"` in `v_ceratyont_potforms_distinct.csv` → no `skos:broader`
  link; a `skos:note` records "publisher: unknown (NULL in source data)".
- `image = "NULL"` → no `foaf:depiction` triple.
- Unknown publisher names (referenced in potforms but missing from the publisher
  lookup) → logged as warning, recorded as `skos:note`.

## Changing configuration

Edit `py/config.yaml` — no Python code changes needed. You can change:
- the base URI
- language tag
- image base URL
- CSV column names (if schema evolves)
- IRI prefixes (e.g. `potform_` → `pf_`)
- collection labels

## Next steps (not yet implemented)

The rest of the CeraTyOnt relations (`hasSameRim`, `hasSameFootring`,
`partiallyCoincidentWith`, `generalisedAs` → GenericPotform, `hasType` →
Tradition, etc.) will be added once the corresponding CSVs are provided.
