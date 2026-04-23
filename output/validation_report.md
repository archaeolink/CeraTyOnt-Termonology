# SHACL Validation Report

*Generated: 2026-04-23 07:42 UTC*

## Inputs

- **Data graph:** `C:\git\CeraTyOnt-Termonology\output\ceratyont_skos.ttl`
- **Shape graphs:**
  - `C:\git\CeraTyOnt-Termonology\py\shapes\skohub_shacl.ttl`
  - `C:\git\CeraTyOnt-Termonology\py\shapes\ceratyont_shapes.ttl`

## Data graph statistics

- Total triples: **10208**
- skos:ConceptScheme: **1**
- skos:Concept: **930**
- skos:Collection: **0**
- foaf:depiction statements: **359**
- skos:broader edges: **2625**
- skos:related edges: **390**
- skos:exactMatch edges: **182**

## Connections applied

| Edge label | Count |
|---|---:|
| `has generic form` | 852 |
| `has tradition` | 876 |
| `has same footring as` | 26 |
| `has same rim as` | 26 |
| `has service member` | 80 |
| `has same groove as` | 2 |
| `has same roulette as` | 66 |
| `has same flute as` | 1 |
| `is same form as` | 92 |

### Issues found during connection import

- **2** row(s) could not be resolved (ID not in lookup tables or wrong class).
- **1** Generic→Generic `skos:broader` edges look possibly reversed (the *from* label is a prefix of the *to* label).

#### Unresolved connections (first 20)

| Edge label | from-id | to-id | Reason |
|---|---|---|---|
| `has generic form` | `544` | `381941` | from-id not in ['potform', 'generic'] |
| `has tradition` | `544` | `2002` | from-id not in ['potform', 'service'] |

#### Possibly reversed Generic→Generic edges

These edges go `from → skos:broader → to`, but the *from* label looks more specific than the *to* label. Review and flip in the source data if needed.

| From (now broader of To) | To |
|---|---|
| Cup | Cup Rouletted |

## Overall result

✅ **Conforms: True** — no violations or warnings found.

---

The machine-readable report (full SHACL `sh:ValidationReport`) is in `validation_report.ttl`.
