# CeraTyOnt → SKOS Terminology

Python pipeline that converts the CeraTyOnt CSV lookup tables into a
SKOS-compliant Turtle terminology **and** validates it against SHACL shapes
(SkoHub SHACL as primary, plus CeraTyOnt-specific rules). Build + validation
run in one script.

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
│   ├── run.py                     # ← ONE script: builds + validates
│   ├── config.yaml                # all paths, URIs, column mappings
│   └── shapes/
│       ├── skohub_shacl.ttl       # primary: SkoHub SKOS SHACL shapes
│       └── ceratyont_shapes.ttl   # secondary: CeraTyOnt-specific rules
├── output/
│   ├── ceratyont_skos.ttl         # generated SKOS terminology
│   ├── validation_report.ttl      # machine-readable SHACL report
│   └── validation_report.md       # human-readable summary ← read this one
└── requirements.txt
```

## Setup (Windows + VS Code)

Open a PowerShell terminal in the project root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks `Activate.ps1`:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

In VS Code: `Ctrl+Shift+P` → *Python: Select Interpreter* → pick `.venv`.

## Usage

From the `py/` folder (so relative paths in `config.yaml` resolve):

```powershell
cd py
python run.py
```

That's it — one command builds the Turtle and validates it.

### Flags

| Flag | Effect |
|------|--------|
| `-v`, `--verbose`       | Debug logging |
| `--skip-build`          | Only validate an existing `ceratyont_skos.ttl` |
| `--skip-validation`     | Only build; don't run SHACL |
| `--strict`              | Exit with code 1 if SHACL reports any violation (useful for CI) |
| `--config path.yaml`    | Use a different config file |

## Outputs

### `output/ceratyont_skos.ttl`
The generated SKOS terminology in Turtle. Contents after a successful build:

- 1 `skos:ConceptScheme`
- **4 facet concepts as top-concepts of the scheme** (`Generic Potforms`,
  `Traditions`, `Services`, `Publishers`) — each acts as the root of its
  own branch
- 60 member `skos:Concept`s attached to their facet via
  `skos:broader` / `skos:narrower` (32 Generic + 3 Tradition + 10 Service + 15 Publisher)
- 866 Potform `skos:Concept`s, each attached to its Publisher, GenericPotform,
  and Tradition via `skos:broader` (multi-parent hierarchy), giving you
  four browseable axes: *by publisher, by form, by tradition, by service*
- Cross-potform relations from the connections CSV:
  - `skos:exactMatch` for *"is same form as"* pairs (symmetric)
  - `skos:related` for *"has service member"* pairs (symmetric)
  - `skos:related` + `lado:hasSame{Rim,Footring,Roulette,Groove,Flute}`
    for feature-similarity relations (both directions)
- Roughly: 2600 `skos:broader` edges, 390 `skos:related` edges,
  180 `skos:exactMatch` edges, 359 `foaf:depiction` statements

The 16 potforms with `publisher = NULL` in the source data are still
included as concepts in the scheme but sit outside the facet hierarchy.

> **Note:** `skos:Collection` objects are intentionally omitted. SKOS viewers
> like [SKOS-Play](https://skos-play.sparna.fr/) render Collections as
> separate branches parallel to top-concepts, which would duplicate the
> facet tree visually. The facet-concept hierarchy alone provides the same
> grouping without this redundancy.

### `output/validation_report.md` — **start here**
Human-readable Markdown summary with:
- Data-graph statistics (triple counts, concept counts, etc.)
- Overall conforms/not-conforms flag
- Breakdown by **severity** (Violation / Warning / Info)
- Breakdown by **SHACL constraint component** (MinCount, Datatype, NodeKind…)
- Breakdown by **message**, each with a table of the first few offending
  focus nodes and their problem values

Example snippet from a report with issues:

> ### Breakdown by message
>
> #### Each skos:Concept has to provide a skos:prefLabel in a unique language — 3 results
>
> | Focus node | Path | Offending value |
> |---|---|---|
> | `ceratyont:broken_test_concept` | `skos:prefLabel` | — |
> | `ceratyont:broken_test_concept_2` | `skos:prefLabel` | `"no language tag"` |

### `output/validation_report.ttl`
Full machine-readable SHACL `sh:ValidationReport` graph. Use this if you want
to process the report programmatically or in SHACL-aware tooling.

## Hierarchy structure

Every Potform has **three `skos:broader` links** (publisher, generic form,
tradition) — so it lives in three facet branches simultaneously. SKOS allows
this multi-parent hierarchy, and SKOS viewers will show the potform under
each parent.

```
ConceptScheme: ceratyont-terminology
│
├── [Top] Generic Potforms (facet)
│         ├── Bowl ─┬─ Bowl Decorated
│         │        └─ Bowl Flanged
│         ├── Cup ──── Cup Decorated ── (also broader: Decorated)
│         ├── Dish ─── Dish Rouletted ── (also broader: Rouletted)
│         ├── Varia ─┬─ Poinçon, Pyxis, Lid, Patera, …
│         │         └─ (sub-categories from the Generic→Generic edges)
│         └── … plus all 866 Potforms link here via skos:broader
│
├── [Top] Traditions (facet)
│         ├── italian       ← potforms + Services I, II
│         ├── Gaulish-Germanic-Raetian ← potforms + Services A–F
│         └── African
│
├── [Top] Services (facet)
│         └── Service I, II, A, B, C, D, E, F, III, IV
│             (linked to potforms via skos:related)
│
└── [Top] Publishers (facet)
          ├── Dragendorff ─── potform_1 (Drag. 15), potform_2 (15/17), …
          ├── Conspectus, Curle, Déchelette, Hermet, Knorr, …
          └── (15 publishers, 850 potforms attached)

Cross-references (non-hierarchical):
  skos:exactMatch  — "is same form as" (92 pairs)
  skos:related     — "has service member" + feature similarities
  lado:hasSameRim / hasSameFootring / hasSameRoulette /
        hasSameGroove / hasSameFlute — refined feature semantics
```

## SHACL shapes

Two shape graphs are loaded and evaluated together (configured in
`config.yaml` under `shapes:`):

### 1. SkoHub SHACL — `shapes/skohub_shacl.ttl`
Generic SKOS structural constraints from <https://github.com/skohub-io/skohub-shacl>.
Enforces, among other things:

- `skos:ConceptScheme` must have a language-tagged `dct:title`, `dct:description`, `dct:license` (IRI), `vann:preferredNamespaceUri` (string), and at least one `skos:hasTopConcept`.
- Every `skos:Concept` must have a `skos:prefLabel` with a unique language tag.
- All label/note properties (`prefLabel`, `altLabel`, `definition`, `scopeNote`, `note`, `example`…) must carry language tags.
- All SKOS relational properties (`broader`, `narrower`, `related`, `inScheme`, `topConceptOf`…) must point to the correct target class.

### 2. CeraTyOnt-specific — `shapes/ceratyont_shapes.ttl`
Project-specific additions:

- `foaf:depiction` must be an IRI, not a literal.
- `skos:broader`/`skos:narrower` targets must be `skos:Concept`s.
- A concept may not be its own `skos:broader` (no reflexive hierarchy).
- `skos:Collection` objects (if any are ever added) must have at least one
  member. Currently unused because Collections are intentionally omitted
  from the model, but the constraint remains active as a safety net.

### Fixes applied to the SkoHub SHACL file
The upstream `skohub_shacl.ttl` had four small Turtle-syntax typos
(`sh:message:` / `sh:severity:` with a stray colon). These were corrected
in this repo's copy so pyshacl actually evaluates the affected constraints.

## Connections from `v_ceratyont_connections.csv`

This CSV defines cross-class relations between concepts via an `edgelabel`
column. Each label maps to RDF as follows:

| `edgelabel` value       | Modelled as                                      |
|-------------------------|--------------------------------------------------|
| `has tradition`         | `skos:broader` (Potform *or* Service → Tradition) |
| `has generic form`      | `skos:broader` (Potform *or* Generic → Generic)   |
| `has service member`    | `skos:related` (Potform ↔ Service, symmetric)     |
| `has publisher`         | *skipped* — already modelled from potforms CSV    |
| `is same form as`       | `skos:exactMatch` (Potform ↔ Potform, symmetric)  |
| `has same rim as`       | `skos:related` + `lado:hasSameRim`                |
| `has same footring as`  | `skos:related` + `lado:hasSameFootring`           |
| `has same roulette as`  | `skos:related` + `lado:hasSameRoulette`           |
| `has same groove as`    | `skos:related` + `lado:hasSameGroove`             |
| `has same flute as`     | `skos:related` + `lado:hasSameFlute`              |

The `lado:*` sub-properties preserve the specific feature semantics while
keeping `skos:related` present for SKOS-aware tools that don't know LADO.
LADO = *Linked Archaeological Data Ontology* — see <http://www.w3id.org/lado/>.

The edge mapping lives in `config.yaml` under `edge_mapping:` — you can add
or change labels there without touching Python code.

### Quality checks during import

The run logs and the Markdown report flag:

- **Unresolved rows** — `id_fromlookupform` or `id_tolookupform` that don't
  match any entry in the lookup tables (data quality issue in the source DB)
- **Possibly reversed Generic→Generic edges** — heuristic: if the *from*
  label is a prefix of the *to* label (e.g. `Cup → Cup Rouletted`), the edge
  may be inverted in the source CSV

These are informational — the build still succeeds, and SHACL validation
runs regardless. Review the Markdown report to decide if the source data
needs correcting.

## Handling of NULL values

- `publisher = "NULL"` in `v_ceratyont_potforms_distinct.csv` → no
  `skos:broader`; a `skos:note` records *"publisher: unknown (NULL in source
  data)"*.
- `image = "NULL"` → no `foaf:depiction` triple.
- Unknown publisher names (referenced in potforms but missing from
  `tbllookuppublisher.csv`) → warning in log, recorded as `skos:note`.

## Configuration

Everything tweakable lives in `py/config.yaml` — no Python edits needed:

- Base URI and scheme IRI
- Language tag
- Image base URL (used for `foaf:depiction`)
- CSV column names (if schema evolves)
- IRI prefixes (e.g. `potform_` → `pf_`)
- **Facet concept** local names, labels, and definitions (under `facets:`)
- List of SHACL shape files (add more as needed — they're merged into one graph)
- Concept-scheme metadata (title, description, license, preferred namespace URI)

## Dependencies

```
pandas>=2.0
PyYAML>=6.0
rdflib>=7.0
pyshacl>=0.25
```

## Next steps (not yet implemented)

The remaining CeraTyOnt relations (`hasSameRim`, `hasSameFootring`,
`partiallyCoincidentWith`, `generalisedAs` → GenericPotform, `hasType` →
Tradition, …) will be added once the corresponding mapping CSVs are provided.
