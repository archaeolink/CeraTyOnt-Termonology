# SHACL Validation Report

*Generated: 2026-04-23 08:32 UTC*

## Inputs

- **Data graph:** `C:\git\CeraTyOnt-Termonology\output\ceratyont_skos.ttl`
- **Shape graphs:**
  - `C:\git\CeraTyOnt-Termonology\py\shapes\skohub_shacl.ttl`
  - `C:\git\CeraTyOnt-Termonology\py\shapes\ceratyont_shapes.ttl`

## Data graph statistics

- Total triples: **14907**
- skos:ConceptScheme: **1**
- skos:Concept: **930**
- skos:Collection: **0**
- foaf:depiction statements: **359**
- skos:broader edges: **2625**
- skos:related edges: **390**
- skos:exactMatch edges: **182**
- skos:definition statements: **930**
- skos:altLabel statements: **120**
- skos:notation statements: **850**
- skos:historyNote statements: **1**

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
- **1** Generic→Generic edge(s) were **auto-flipped** because the *from* label looked more generic than the *to* label (see next section).

#### Unresolved connections (first 20)

| Edge label | from-id | to-id | Reason |
|---|---|---|---|
| `has generic form` | `544` | `381941` | from-id not in ['potform', 'generic'] |
| `has tradition` | `544` | `2002` | from-id not in ['potform', 'service'] |

#### Auto-flipped Generic→Generic edges

These edges were in the source data as `from → skos:broader → to`, but the *from* label looked more generic than the *to* label, so the edge was flipped on build. Each flipped concept carries a `skos:historyNote` documenting the change.

| Original (source CSV) | Emitted (flipped) |
|---|---|
| `Cup` → `Cup Rouletted` | `Cup Rouletted` → `Cup` |

## Overall result

✅ **Conforms: True** — no violations or warnings found.

---

The machine-readable report (full SHACL `sh:ValidationReport`) is in `validation_report.ttl`.
