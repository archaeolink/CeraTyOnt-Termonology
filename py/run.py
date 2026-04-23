"""
run.py — CeraTyOnt CSV → SKOS Turtle → SHACL validation (all-in-one)
===================================================================

Reads 5 CSV lookup/data tables and produces a SKOS-compliant terminology
(Turtle), then validates it against one or more SHACL shape graphs
(SkoHub SHACL + CeraTyOnt-specific rules).

Outputs (in ../output/):
  - ceratyont_skos.ttl         — the generated SKOS terminology
  - validation_report.ttl      — machine-readable SHACL report
  - validation_report.md       — human-readable Markdown summary

Usage (from the py/ folder):
    python run.py
    python run.py --verbose
    python run.py --skip-validation
    python run.py --skip-build              # only validate an existing .ttl
    python run.py --strict                  # exit non-zero on SHACL violation
    python run.py --config config.yaml
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pyshacl import validate
from rdflib import DCTERMS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DC, FOAF, OWL, RDF, RDFS, SKOS, XSD

from abbreviations import abbreviate

log = logging.getLogger("run")

# SHACL + VANN + PROV namespaces (not predefined in rdflib)
SH = Namespace("http://www.w3.org/ns/shacl#")
VANN = Namespace("http://purl.org/vocab/vann/")
PROV = Namespace("http://www.w3.org/ns/prov#")


# =============================================================================
# Config
# =============================================================================

def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    log.debug("Loaded config from %s", path)
    return cfg


def is_null(val: Any, markers: list[str]) -> bool:
    """True if the CSV cell is empty or matches a null-marker sentinel."""
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    return str(val).strip() in markers


def read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    log.info("  %s: %d rows, columns=%s", path.name, len(df), list(df.columns))
    return df


def make_color_note(row: pd.Series, color_col: str | None) -> str | None:
    if color_col and color_col in row and row[color_col].strip():
        return f"display color: {row[color_col].strip()}"
    return None


# =============================================================================
# SKOS builder
# =============================================================================

class SkosBuilder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lang = cfg["language"]
        self.null_markers = cfg["null_markers"]
        self.BASE = Namespace(cfg["namespaces"]["base_uri"])
        self.SCHEME = URIRef(cfg["namespaces"]["scheme_iri"])
        self.IMG_BASE = cfg["namespaces"]["image_base_url"]
        self.LADO = Namespace(cfg["namespaces"]["lado_uri"])

        self.graph = Graph()
        self.graph.bind("skos", SKOS)
        self.graph.bind("foaf", FOAF)
        self.graph.bind("dct", DCTERMS)
        self.graph.bind("dc", DC)
        self.graph.bind("rdfs", RDFS)
        self.graph.bind("xsd", XSD)
        self.graph.bind("owl", OWL)
        self.graph.bind("prov", PROV)
        self.graph.bind("vann", VANN)
        self.graph.bind("lado", self.LADO)
        self.graph.bind("ceratyont", self.BASE)

        self.publisher_by_label: dict[str, URIRef] = {}
        # populated in build_facets(); each kind → its facet-concept IRI
        self.facet_by_kind: dict[str, URIRef] = {}
        # populated as each class is built; (kind, id) → concept IRI
        # Used by build_connections() to resolve from_id/to_id from the edge CSV.
        self.id_index: dict[tuple[str, str], URIRef] = {}
        # Per-kind dct:created timestamps (ISO 8601 with time)
        # Populated in build_skos() from CSV mtimes before builders run.
        self.created_by_kind: dict[str, Literal] = {}
        # Creator IRI (foaf:Person, ORCID) — resolved once, reused everywhere
        cm = cfg.get("concept_metadata", {})
        self.creator_iri = URIRef(cm["creator_iri"]) if cm.get("creator_iri") else None
        self.creator_name = cm.get("creator_name")

    def concept_iri(self, kind: str, identifier: str) -> URIRef:
        return URIRef(f"{self.BASE}{self.cfg['iri_prefixes'][kind]}{identifier}")

    def _add_concept_common_metadata(self, concept: URIRef, kind: str, label: str) -> None:
        """Attach metadata common to every skos:Concept:
           - dct:creator (ORCID IRI for Allard Mees)
           - dct:created (from source CSV mtime)
           - skos:scopeNote (from template)
           - skos:definition is added separately, since it may or may not be
             present depending on whether the kind has a template.
        """
        # Creator — the IRI is the value; the foaf:Person describing that IRI
        # is added once centrally in build_scheme() so we don't duplicate it
        # for every concept.
        if self.creator_iri is not None:
            self.graph.add((concept, DCTERMS.creator, self.creator_iri))

        # dct:created per kind
        created = self.created_by_kind.get(kind)
        if created is not None:
            self.graph.add((concept, DCTERMS.created, created))

        # skos:scopeNote from template
        scope_tpl = self.cfg.get("scope_note_templates", {}).get(kind)
        if scope_tpl:
            self.graph.add((
                concept, SKOS.scopeNote,
                Literal(scope_tpl.format(label=label), lang=self.lang),
            ))

    def _add_creator_person(self) -> None:
        """Describe the creator as a foaf:Person once, at scheme level.
        Allows any dct:creator link to be dereferenced to a name + ORCID."""
        if self.creator_iri is None:
            return
        g = self.graph
        g.add((self.creator_iri, RDF.type, FOAF.Person))
        if self.creator_name:
            g.add((self.creator_iri, FOAF.name, Literal(self.creator_name)))
            g.add((self.creator_iri, RDFS.label, Literal(self.creator_name)))

    def build_scheme(self) -> None:
        g, s, lang = self.graph, self.SCHEME, self.lang
        meta = self.cfg["scheme"]
        g.add((s, RDF.type, SKOS.ConceptScheme))
        # Language-tagged literals (SkoHub SHACL requires rdf:langString on these)
        g.add((s, DCTERMS.title, Literal(meta["title"], lang=lang)))
        g.add((s, DCTERMS.description, Literal(meta["description"], lang=lang)))
        g.add((s, SKOS.prefLabel, Literal(meta["title"], lang=lang)))
        g.add((s, RDFS.label, Literal(meta["title"], lang=lang)))

        # Creator — use ORCID IRI if configured, fall back to plain string
        if self.creator_iri is not None:
            g.add((s, DCTERMS.creator, self.creator_iri))
            self._add_creator_person()
        else:
            g.add((s, DCTERMS.creator, Literal(meta["creator"])))
        # Contributor and publisher stay as strings
        g.add((s, DCTERMS.contributor, Literal(meta["contributor"])))
        g.add((s, DCTERMS.publisher, Literal(meta["publisher"])))
        g.add((s, DCTERMS.rights, Literal(meta["rights"])))

        # Versioning — both dct:hasVersion (string) and owl:versionInfo (string)
        version = str(meta["version"])
        g.add((s, DCTERMS.hasVersion, Literal(version)))
        g.add((s, OWL.versionInfo, Literal(version)))

        # Dates — dct:created, dct:issued are fixed (from config);
        # dct:modified is the build timestamp (auto).
        g.add((s, DCTERMS.created, Literal(meta["created"], datatype=XSD.date)))
        g.add((s, DCTERMS.issued,  Literal(meta["issued"],  datatype=XSD.date)))
        g.add((s, DCTERMS.modified,
               Literal(date.today().isoformat(), datatype=XSD.date)))

        # License + namespace info
        g.add((s, DCTERMS.license, URIRef(meta["license"])))
        g.add((s, VANN.preferredNamespaceUri,
               Literal(meta["preferred_namespace_uri"], datatype=XSD.string)))
        if meta.get("preferred_namespace_prefix"):
            g.add((s, VANN.preferredNamespacePrefix,
                   Literal(meta["preferred_namespace_prefix"], datatype=XSD.string)))

        # Link back to source ontology
        if meta.get("source_ontology"):
            g.add((s, RDFS.seeAlso, URIRef(meta["source_ontology"])))
            g.add((s, DCTERMS.source, URIRef(meta["source_ontology"])))
            # PROV: this scheme was derived from the source ontology
            g.add((s, PROV.wasDerivedFrom, URIRef(meta["source_ontology"])))

        log.info("ConceptScheme: %s", s)

    def build_facets(self) -> None:
        """Create one skos:Concept per facet (acts as a branch root / Top-Concept)."""
        for kind, meta in self.cfg["facets"].items():
            iri = URIRef(f"{self.BASE}{meta['local_name']}")
            self.facet_by_kind[kind] = iri
            label = meta["label"]
            self.graph.add((iri, RDF.type, SKOS.Concept))
            self.graph.add((iri, SKOS.inScheme, self.SCHEME))
            self.graph.add((iri, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((iri, RDFS.label, Literal(label, lang=self.lang)))
            if meta.get("definition"):
                self.graph.add((iri, SKOS.definition,
                                Literal(meta["definition"], lang=self.lang)))
            # Facet concepts are the top concepts of the scheme
            self.graph.add((iri, SKOS.topConceptOf, self.SCHEME))
            self.graph.add((self.SCHEME, SKOS.hasTopConcept, iri))
            # Common concept metadata (creator, created, scopeNote)
            self._add_concept_common_metadata(iri, kind, label)
            log.info("Facet concept: %s (%s)", label, iri)

    def _build_simple_concepts(
        self,
        kind: str,
        df: pd.DataFrame,
        color_col_key: str | None = None,
    ) -> dict[str, URIRef]:
        """Build concepts for a simple class (generic/tradition/service/publisher).

        Each concept becomes a narrower of the facet concept for this kind.
        Facet concepts themselves are the scheme's top concepts (built in
        build_facets, which must run before this).
        """
        col = self.cfg["columns"][kind]
        id_col, label_col = col["id"], col["label"]
        color_col = col.get(color_col_key) if color_col_key else None
        facet_iri = self.facet_by_kind[kind]
        built: dict[str, URIRef] = {}

        for _, row in df.iterrows():
            if is_null(row[id_col], self.null_markers):
                log.warning("  skipping %s row with empty id: %s", kind, row.to_dict())
                continue
            ident = str(row[id_col]).strip()
            label = str(row[label_col]).strip()
            concept = self.concept_iri(kind, ident)
            self.id_index[(kind, ident)] = concept

            self.graph.add((concept, RDF.type, SKOS.Concept))
            self.graph.add((concept, SKOS.inScheme, self.SCHEME))
            self.graph.add((concept, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((concept, RDFS.label, Literal(label, lang=self.lang)))

            # Generic definition from template
            template = self.cfg.get("definition_templates", {}).get(kind)
            if template:
                self.graph.add((
                    concept, SKOS.definition,
                    Literal(template.format(label=label), lang=self.lang),
                ))

            # Common concept metadata (creator, created, scopeNote)
            self._add_concept_common_metadata(concept, kind, label)

            # Hierarchy: member → facet concept (both directions, per SKOS convention)
            self.graph.add((concept, SKOS.broader, facet_iri))
            self.graph.add((facet_iri, SKOS.narrower, concept))

            note = make_color_note(row, color_col)
            if note:
                self.graph.add((concept, SKOS.note, Literal(note, lang=self.lang)))

            built[label] = concept

        log.info("  built %d %s concepts", len(built), kind)
        return built

    def build_generics(self, df):    return self._build_simple_concepts("generic", df)
    def build_traditions(self, df):  return self._build_simple_concepts("tradition", df, "color")
    def build_services(self, df):    return self._build_simple_concepts("service", df, "color")
    def build_publishers(self, df):
        built = self._build_simple_concepts("publisher", df, "color")
        self.publisher_by_label = dict(built)
        return built

    def build_potforms(self, df: pd.DataFrame) -> dict[str, URIRef]:
        col = self.cfg["columns"]["potforms"]
        id_col, label_col = col["id"], col["label"]
        image_col, publisher_col = col["image"], col["publisher"]
        built: dict[str, URIRef] = {}
        unresolved: set[str] = set()
        orphans: list[str] = []  # potforms with no publisher link — no broader at all

        for _, row in df.iterrows():
            if is_null(row[id_col], self.null_markers):
                log.warning("  skipping potform row with empty id: %s", row.to_dict())
                continue
            ident = str(row[id_col]).strip()
            label = str(row[label_col]).strip()
            concept = self.concept_iri("potform", ident)
            self.id_index[("potform", ident)] = concept

            self.graph.add((concept, RDF.type, SKOS.Concept))
            self.graph.add((concept, SKOS.inScheme, self.SCHEME))
            self.graph.add((concept, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((concept, RDFS.label, Literal(label, lang=self.lang)))

            # Generic definition from template
            template = self.cfg.get("definition_templates", {}).get("potform")
            if template:
                self.graph.add((
                    concept, SKOS.definition,
                    Literal(template.format(label=label), lang=self.lang),
                ))

            # Common concept metadata (creator, created, scopeNote)
            self._add_concept_common_metadata(concept, "potform", label)

            img = str(row[image_col]).strip() if image_col in row else ""
            if img and not is_null(img, self.null_markers):
                self.graph.add((concept, FOAF.depiction, URIRef(f"{self.IMG_BASE}{img}")))

            # Notation + alt-label via publisher abbreviation (e.g. "Drag. 15").
            # Only meaningful if we have a non-NULL publisher; added in the publisher
            # branch below so we have the label in scope.

            # Hierarchy: Potforms live under their Publisher (which lives under
            # the Publishers facet). No dedicated Potforms facet.
            pub_label = str(row[publisher_col]).strip() if publisher_col in row else ""
            if is_null(pub_label, self.null_markers):
                self.graph.add((
                    concept, SKOS.note,
                    Literal("publisher: unknown (NULL in source data)", lang=self.lang),
                ))
                orphans.append(ident)
            elif pub_label in self.publisher_by_label:
                pub_iri = self.publisher_by_label[pub_label]
                self.graph.add((concept, SKOS.broader, pub_iri))
                self.graph.add((pub_iri, SKOS.narrower, concept))

                # skos:notation + skos:altLabel as "<PublisherAbbrev> <FormLabel>"
                # e.g. prefLabel "15" + publisher Dragendorff → notation "Drag. 15".
                # Skip if the label already embeds the publisher (avoids "Hayes Hayes 199").
                if self.cfg.get("build", {}).get("emit_potform_notation", True):
                    abbrev = abbreviate(pub_label)
                    label_lc = label.lower()
                    already_embeds_publisher = (
                        label_lc.startswith(abbrev.lower())
                        or label_lc.startswith(pub_label.lower())
                    )
                    if already_embeds_publisher:
                        # Use the label itself as the notation (it's already identifying)
                        notation_str = label
                    else:
                        notation_str = f"{abbrev} {label}"
                    self.graph.add((concept, SKOS.notation,
                                    Literal(notation_str, datatype=XSD.string)))
                    # Only add altLabel if it actually differs from prefLabel
                    if notation_str != label:
                        self.graph.add((concept, SKOS.altLabel,
                                        Literal(notation_str, lang=self.lang)))
            else:
                unresolved.add(pub_label)
                self.graph.add((
                    concept, SKOS.note,
                    Literal(f"publisher '{pub_label}' not found in publisher lookup table",
                            lang=self.lang),
                ))

            built[ident] = concept

        if unresolved:
            log.warning("  unknown publishers referenced: %s", sorted(unresolved))
        if orphans:
            log.info(
                "  %d potform(s) have no publisher link (NULL in source) — "
                "they live in the scheme but outside the facet hierarchy",
                len(orphans),
            )
        log.info("  built %d potform concepts", len(built))
        return built

    # ------------------------------------------------------------------
    # Connections / cross-class relations from v_ceratyont_connections.csv
    # ------------------------------------------------------------------

    # Map RDF property strings from config to actual URIRefs
    _PROPERTY_MAP = {
        "skos:broader":    SKOS.broader,
        "skos:narrower":   SKOS.narrower,
        "skos:related":    SKOS.related,
        "skos:exactMatch": SKOS.exactMatch,
        "skos:closeMatch": SKOS.closeMatch,
        "skos:member":     SKOS.member,
    }

    # Which SKOS properties are symmetric (emit in both directions)?
    _SYMMETRIC = {SKOS.related, SKOS.exactMatch, SKOS.closeMatch}

    def _resolve_id(self, ident: str, allowed_kinds: list[str]) -> tuple[str, URIRef] | None:
        """Find which class an ID belongs to by checking id_index.

        Returns (kind, concept_iri) or None if the ID isn't found.
        If multiple kinds match (shouldn't happen with current data), returns the first.
        """
        for k in allowed_kinds:
            hit = self.id_index.get((k, ident))
            if hit is not None:
                return k, hit
        return None

    def build_connections(self, df: pd.DataFrame) -> dict[str, int]:
        """Apply cross-class relations from the connections CSV.

        Returns a stats dict: {edgelabel: n_edges_applied} plus 'skipped',
        'unresolved', 'suspicious'.
        """
        col = self.cfg["columns"]["connections"]
        edge_col, from_col, to_col = col["edgelabel"], col["from_id"], col["to_id"]
        mapping = self.cfg["edge_mapping"]
        build_cfg = self.cfg.get("build", {})
        auto_flip_enabled = build_cfg.get("auto_flip_suspicious_generic_edges", False)
        auto_flip_max = int(build_cfg.get("auto_flip_max", 5))

        stats: dict[str, int] = {}
        skipped_unknown_label = 0
        unresolved: list[dict] = []
        # "Suspicious" = Generic-to-Generic where the from is a less-specific label
        # than the to (e.g. Cup → Cup Rouletted). Logged for review.
        suspicious_generic_hierarchy: list[tuple[str, str]] = []
        flipped: list[tuple[str, str]] = []  # records of actually-flipped pairs
        flipped_count = 0

        # Safety net: count suspicious edges first; if the heuristic suddenly
        # flags way more than before, refuse to auto-flip silently.
        if auto_flip_enabled:
            preview_count = sum(
                1 for _, r in df.iterrows()
                if str(r[edge_col]).strip() == "has generic form"
                and (self.id_index.get(("generic", str(r[from_col]).strip())) is not None)
                and (self.id_index.get(("generic", str(r[to_col]).strip())) is not None)
                and (
                    str(self.graph.value(
                        self.id_index[("generic", str(r[to_col]).strip())], SKOS.prefLabel) or ""
                    ).lower().startswith(
                        str(self.graph.value(
                            self.id_index[("generic", str(r[from_col]).strip())], SKOS.prefLabel) or ""
                        ).lower() + " "
                    )
                )
            )
            if preview_count > auto_flip_max:
                log.error(
                    "Auto-flip would affect %d Generic→Generic edges (limit: %d). "
                    "Refusing to flip silently — review the source data and raise "
                    "auto_flip_max in config.yaml if you're sure, or set "
                    "auto_flip_suspicious_generic_edges: false to just flag them.",
                    preview_count, auto_flip_max,
                )
                raise RuntimeError(
                    f"auto_flip_max ({auto_flip_max}) would be exceeded "
                    f"({preview_count} edges would be flipped)"
                )

        for _, row in df.iterrows():
            label = str(row[edge_col]).strip()
            from_id = str(row[from_col]).strip()
            to_id = str(row[to_col]).strip()

            if label not in mapping:
                skipped_unknown_label += 1
                continue
            rule = mapping[label]
            if rule.get("skip"):
                continue

            if is_null(from_id, self.null_markers) or is_null(to_id, self.null_markers):
                unresolved.append({"label": label, "from": from_id, "to": to_id,
                                   "reason": "empty id"})
                continue

            # Resolve from_id (can be one of several kinds)
            resolved_from = self._resolve_id(from_id, rule["from_kinds"])
            if resolved_from is None:
                unresolved.append({"label": label, "from": from_id, "to": to_id,
                                   "reason": f"from-id not in {rule['from_kinds']}"})
                continue
            from_kind, from_iri = resolved_from

            # Resolve to_id (single kind)
            to_iri = self.id_index.get((rule["to_kind"], to_id))
            if to_iri is None:
                unresolved.append({"label": label, "from": from_id, "to": to_id,
                                   "reason": f"to-id not in {rule['to_kind']}"})
                continue

            # Don't self-link (defensive; CSV shouldn't have these)
            if from_iri == to_iri:
                unresolved.append({"label": label, "from": from_id, "to": to_id,
                                   "reason": "self-link"})
                continue

            prop = self._PROPERTY_MAP[rule["property"]]
            inverse = self._PROPERTY_MAP.get(rule.get("inverse")) if rule.get("inverse") else None

            # Detect potentially reversed Generic→Generic edges.
            # Heuristic: if the *from* label is a prefix of the *to* label,
            # the edge is probably reversed (e.g. "Cup" → "Cup Rouletted" should
            # semantically be "Cup Rouletted → Cup").
            is_suspicious = False
            if label == "has generic form" and from_kind == "generic":
                from_label = str(self.graph.value(from_iri, SKOS.prefLabel) or "?")
                to_label = str(self.graph.value(to_iri, SKOS.prefLabel) or "?")
                if to_label.lower().startswith(from_label.lower() + " "):
                    is_suspicious = True
                    suspicious_generic_hierarchy.append((from_label, to_label))

            # Decide whether to flip or emit as-is
            if is_suspicious and auto_flip_enabled:
                # Flip: the more-specific (to) becomes narrower of the less-specific (from)
                # Emit: to → broader → from   and   from → narrower → to
                self.graph.add((to_iri, prop, from_iri))
                if inverse is not None:
                    self.graph.add((from_iri, inverse, to_iri))
                # skos:historyNote on the flipped concept documents the change
                self.graph.add((
                    to_iri, SKOS.historyNote,
                    Literal(
                        f"Edge '{from_label} → {to_label}' from the source data was "
                        f"auto-flipped to '{to_label} → {from_label}' on build because "
                        "the from-label looked more generic than the to-label (build "
                        "setting: auto_flip_suspicious_generic_edges).",
                        lang=self.lang,
                    ),
                ))
                flipped.append((from_label, to_label))
                flipped_count += 1
            else:
                # Emit the edge as-is
                self.graph.add((from_iri, prop, to_iri))
                if inverse is not None:
                    self.graph.add((to_iri, inverse, from_iri))
                elif prop in self._SYMMETRIC:
                    self.graph.add((to_iri, prop, from_iri))

            # Optional LADO sub-property (preserves feature-similarity semantics)
            sub = rule.get("lado_subproperty")
            if sub:
                sub_prop = URIRef(f"{self.LADO}{sub}")
                self.graph.add((from_iri, sub_prop, to_iri))
                self.graph.add((to_iri, sub_prop, from_iri))

            stats[label] = stats.get(label, 0) + 1

        stats["_skipped_unknown_label"] = skipped_unknown_label
        stats["_unresolved"] = len(unresolved)
        stats["_suspicious_generic_hierarchy"] = suspicious_generic_hierarchy
        stats["_flipped"] = flipped
        stats["_flipped_count"] = flipped_count

        log.info("Connections applied:")
        for k, v in stats.items():
            if not k.startswith("_"):
                log.info("  %-28s  %d", k, v)
        if skipped_unknown_label:
            log.warning("  %d row(s) had an unknown edgelabel (skipped)", skipped_unknown_label)
        if unresolved:
            log.warning("  %d row(s) could not be resolved — see validation_report.md",
                        len(unresolved))
            self._connection_unresolved = unresolved
        else:
            self._connection_unresolved = []
        if flipped_count:
            log.info("  %d Generic→Generic edge(s) auto-flipped (see validation_report.md)",
                     flipped_count)
        elif suspicious_generic_hierarchy:
            log.warning("  %d possibly reversed Generic→Generic edges flagged "
                        "(auto-flip disabled) — see validation_report.md",
                        len(suspicious_generic_hierarchy))

        return stats


def build_skos(cfg: dict, project_root: Path) -> tuple[Path, int]:
    """Build the SKOS graph, serialize to Turtle, return (path, triple count)."""
    data_dir = (project_root / cfg["input"]["data_dir"]).resolve()
    output_dir = (project_root / cfg["output"]["dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("Reading CSVs from %s", data_dir)
    files = cfg["input"]["files"]
    df_generic     = read_csv(data_dir / files["generic"])
    df_tradition   = read_csv(data_dir / files["tradition"])
    df_service     = read_csv(data_dir / files["service"])
    df_publisher   = read_csv(data_dir / files["publisher"])
    df_potforms    = read_csv(data_dir / files["potforms"])
    df_connections = read_csv(data_dir / files["connections"])

    log.info("Building SKOS graph…")
    b = SkosBuilder(cfg)

    # Resolve per-kind dct:created from CSV mtimes (if enabled)
    use_mtime = cfg.get("concept_metadata", {}).get("use_csv_mtime_for_created", False)
    if use_mtime:
        def csv_mtime(rel_name: str) -> Literal:
            """Return the CSV file's mtime as an xsd:dateTime literal (UTC)."""
            p = data_dir / rel_name
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            # Truncate microseconds for tidier Turtle
            iso = mtime.replace(microsecond=0).isoformat()
            return Literal(iso, datatype=XSD.dateTime)

        b.created_by_kind = {
            "generic":   csv_mtime(files["generic"]),
            "tradition": csv_mtime(files["tradition"]),
            "service":   csv_mtime(files["service"]),
            "publisher": csv_mtime(files["publisher"]),
            "potform":   csv_mtime(files["potforms"]),
        }
        log.info("  dct:created per kind (from CSV mtime):")
        for k, v in sorted(b.created_by_kind.items()):
            log.info("    %-12s  %s", k, v)

    b.build_scheme()
    b.build_facets()                      # facet top-concepts first
    b.build_generics(df_generic)
    b.build_traditions(df_tradition)
    b.build_services(df_service)
    b.build_publishers(df_publisher)      # must come before potforms (broader lookup)
    b.build_potforms(df_potforms)
    conn_stats = b.build_connections(df_connections)

    out_path = output_dir / cfg["output"]["skos_file"]
    b.graph.serialize(destination=str(out_path), format="turtle")
    log.info("✓ Wrote %d triples to %s", len(b.graph), out_path)
    return out_path, len(b.graph), conn_stats, b._connection_unresolved


# =============================================================================
# SHACL validation + human-readable Markdown report
# =============================================================================

def _short(node, nm) -> str:
    """Turn a URIRef/Literal into a readable short form (prefixed if possible)."""
    try:
        return node.n3(nm)
    except Exception:
        return str(node)


def parse_report(results_graph: Graph) -> dict:
    """Extract a structured summary from the SHACL validation report graph."""
    nm = results_graph.namespace_manager
    # Bind common namespaces so shortening works even if the report doesn't include them
    nm.bind("sh", SH)
    nm.bind("skos", SKOS)
    nm.bind("ceratyont", Namespace("http://www.w3id.org/archlink/terms/ceratyont-terminology/"))

    # Conforms flag
    conforms = None
    for _, _, o in results_graph.triples((None, SH.conforms, None)):
        conforms = bool(o)
        break

    violations = []
    for vr in results_graph.subjects(RDF.type, SH.ValidationResult):
        def one(p):
            vals = list(results_graph.objects(vr, p))
            return vals[0] if vals else None

        sev = one(SH.resultSeverity)
        node = one(SH.focusNode)
        path = one(SH.resultPath)
        msg = one(SH.resultMessage)
        val = one(SH.value)
        comp = one(SH.sourceConstraintComponent)

        violations.append({
            "severity": _short(sev, nm) if sev else "sh:Violation",
            "focus": _short(node, nm) if node else "",
            "path": _short(path, nm) if path else "",
            "message": str(msg) if msg else "",
            "value": _short(val, nm) if val else "",
            "component": _short(comp, nm) if comp else "",
        })

    # Group for summary
    by_severity = Counter(v["severity"] for v in violations)
    by_component = Counter(v["component"] for v in violations)
    by_path = Counter(v["path"] for v in violations if v["path"])
    by_message = Counter(v["message"] for v in violations if v["message"])

    return {
        "conforms": conforms,
        "violations": violations,
        "by_severity": by_severity,
        "by_component": by_component,
        "by_path": by_path,
        "by_message": by_message,
    }


def data_graph_stats(data_graph: Graph) -> dict:
    """Factual overview of what we validated."""
    concepts = set(data_graph.subjects(RDF.type, SKOS.Concept))
    collections = set(data_graph.subjects(RDF.type, SKOS.Collection))
    schemes = set(data_graph.subjects(RDF.type, SKOS.ConceptScheme))
    depictions = sum(1 for _ in data_graph.subject_objects(FOAF.depiction))
    broader = sum(1 for _ in data_graph.subject_objects(SKOS.broader))
    related = sum(1 for _ in data_graph.subject_objects(SKOS.related))
    exact_match = sum(1 for _ in data_graph.subject_objects(SKOS.exactMatch))
    definitions = sum(1 for _ in data_graph.subject_objects(SKOS.definition))
    notations = sum(1 for _ in data_graph.subject_objects(SKOS.notation))
    alt_labels = sum(1 for _ in data_graph.subject_objects(SKOS.altLabel))
    history_notes = sum(1 for _ in data_graph.subject_objects(SKOS.historyNote))
    return {
        "triples": len(data_graph),
        "concepts": len(concepts),
        "collections": len(collections),
        "schemes": len(schemes),
        "depictions": depictions,
        "broader_edges": broader,
        "related_edges": related,
        "exact_match_edges": exact_match,
        "definitions": definitions,
        "notations": notations,
        "alt_labels": alt_labels,
        "history_notes": history_notes,
    }


def write_markdown_report(
    md_path: Path,
    skos_path: Path,
    shape_paths: list[Path],
    stats: dict,
    summary: dict,
    conn_stats: dict | None = None,
    conn_unresolved: list | None = None,
    max_examples_per_group: int = 5,
) -> None:
    lines: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines += [
        "# SHACL Validation Report",
        "",
        f"*Generated: {ts}*",
        "",
        "## Inputs",
        "",
        f"- **Data graph:** `{skos_path}`",
        "- **Shape graphs:**",
    ]
    for sp in shape_paths:
        lines.append(f"  - `{sp}`")
    lines += [
        "",
        "## Data graph statistics",
        "",
        f"- Total triples: **{stats['triples']}**",
        f"- skos:ConceptScheme: **{stats['schemes']}**",
        f"- skos:Concept: **{stats['concepts']}**",
        f"- skos:Collection: **{stats['collections']}**",
        f"- foaf:depiction statements: **{stats['depictions']}**",
        f"- skos:broader edges: **{stats['broader_edges']}**",
        f"- skos:related edges: **{stats['related_edges']}**",
        f"- skos:exactMatch edges: **{stats['exact_match_edges']}**",
        f"- skos:definition statements: **{stats['definitions']}**",
        f"- skos:altLabel statements: **{stats['alt_labels']}**",
        f"- skos:notation statements: **{stats['notations']}**",
        f"- skos:historyNote statements: **{stats['history_notes']}**",
        "",
    ]

    # --- Connections section (from v_ceratyont_connections.csv) ---
    if conn_stats:
        lines += ["## Connections applied", ""]
        lines.append("| Edge label | Count |")
        lines.append("|---|---:|")
        for k, v in conn_stats.items():
            if k.startswith("_"):
                continue
            lines.append(f"| `{k}` | {v} |")
        lines.append("")

        total_unresolved = conn_stats.get("_unresolved", 0)
        total_skipped = conn_stats.get("_skipped_unknown_label", 0)
        suspicious = conn_stats.get("_suspicious_generic_hierarchy", []) or []
        flipped = conn_stats.get("_flipped", []) or []
        flipped_count = conn_stats.get("_flipped_count", 0) or 0

        if total_skipped or total_unresolved or suspicious:
            lines += ["### Issues found during connection import", ""]
            if total_skipped:
                lines.append(f"- **{total_skipped}** row(s) had an unrecognised `edgelabel` and were skipped.")
            if total_unresolved:
                lines.append(f"- **{total_unresolved}** row(s) could not be resolved (ID not in lookup tables or wrong class).")
            if suspicious:
                if flipped_count:
                    lines.append(
                        f"- **{flipped_count}** Generic→Generic edge(s) were **auto-flipped** "
                        "because the *from* label looked more generic than the *to* label "
                        "(see next section)."
                    )
                else:
                    lines.append(
                        f"- **{len(suspicious)}** Generic→Generic `skos:broader` edges look "
                        "possibly reversed (the *from* label is a prefix of the *to* label). "
                        "Auto-flip is disabled in `config.yaml`, so these were kept as-is."
                    )
            lines.append("")

        if conn_unresolved:
            lines += [
                "#### Unresolved connections (first 20)",
                "",
                "| Edge label | from-id | to-id | Reason |",
                "|---|---|---|---|",
            ]
            for u in conn_unresolved[:20]:
                lines.append(
                    f"| `{u['label']}` | `{u['from']}` | `{u['to']}` | {u['reason']} |"
                )
            if len(conn_unresolved) > 20:
                lines.append(f"| *…and {len(conn_unresolved) - 20} more* | | | |")
            lines.append("")

        if flipped:
            lines += [
                "#### Auto-flipped Generic→Generic edges",
                "",
                "These edges were in the source data as `from → skos:broader → to`, but the "
                "*from* label looked more generic than the *to* label, so the edge was "
                "flipped on build. Each flipped concept carries a `skos:historyNote` "
                "documenting the change.",
                "",
                "| Original (source CSV) | Emitted (flipped) |",
                "|---|---|",
            ]
            for f, t in flipped[:20]:
                lines.append(f"| `{f}` → `{t}` | `{t}` → `{f}` |")
            if len(flipped) > 20:
                lines.append(f"| *…and {len(flipped) - 20} more* | |")
            lines.append("")
        elif suspicious:
            lines += [
                "#### Possibly reversed Generic→Generic edges (not flipped)",
                "",
                "These edges go `from → skos:broader → to`, but the *from* label looks "
                "more specific than the *to* label. Flip in the source data if intended, "
                "or enable `build.auto_flip_suspicious_generic_edges` in `config.yaml`.",
                "",
                "| From (now broader of To) | To |",
                "|---|---|",
            ]
            for f, t in suspicious[:20]:
                lines.append(f"| {f} | {t} |")
            if len(suspicious) > 20:
                lines.append(f"| *…and {len(suspicious) - 20} more* | |")
            lines.append("")

    lines += [
        "## Overall result",
        "",
    ]

    if summary["conforms"] is True and not summary["violations"]:
        lines += ["✅ **Conforms: True** — no violations or warnings found.", ""]
    elif summary["conforms"] is True:
        lines += [
            "✅ **Conforms: True** (SHACL-wise), but the report contains "
            f"{len(summary['violations'])} informational result(s) below.",
            "",
        ]
    else:
        lines += [
            f"❌ **Conforms: False** — {len(summary['violations'])} result(s) found.",
            "",
        ]

    # Breakdown by severity
    if summary["by_severity"]:
        lines += ["### Breakdown by severity", ""]
        lines.append("| Severity | Count |")
        lines.append("|---|---:|")
        for sev, n in summary["by_severity"].most_common():
            lines.append(f"| {sev} | {n} |")
        lines.append("")

    # Breakdown by constraint component
    if summary["by_component"]:
        lines += ["### Breakdown by SHACL constraint component", ""]
        lines.append("| Constraint | Count |")
        lines.append("|---|---:|")
        for comp, n in summary["by_component"].most_common():
            lines.append(f"| `{comp}` | {n} |")
        lines.append("")

    # Breakdown by message (most informative — tells you *what* failed)
    if summary["by_message"]:
        lines += [
            "### Breakdown by message",
            "",
            "Grouped by the human-readable message from the shape. "
            f"Showing up to {max_examples_per_group} example focus nodes per group.",
            "",
        ]
        # group violations by message for examples
        examples: dict[str, list[dict]] = defaultdict(list)
        for v in summary["violations"]:
            examples[v["message"]].append(v)

        for msg, n in summary["by_message"].most_common():
            lines.append(f"#### {msg or '(no message)'} — {n} result(s)")
            lines.append("")
            lines.append("| Focus node | Path | Offending value |")
            lines.append("|---|---|---|")
            for v in examples[msg][:max_examples_per_group]:
                lines.append(
                    f"| `{v['focus']}` | `{v['path']}` | "
                    f"{'`' + v['value'] + '`' if v['value'] else '—'} |"
                )
            if n > max_examples_per_group:
                lines.append(f"| *…and {n - max_examples_per_group} more* | | |")
            lines.append("")

    # Footer note about where full data lives
    lines += [
        "---",
        "",
        "The machine-readable report (full SHACL `sh:ValidationReport`) is in "
        "`validation_report.ttl`.",
        "",
    ]

    md_path.write_text("\n".join(lines), encoding="utf-8")


def validate_skos(
    cfg: dict, project_root: Path, skos_path: Path, verbose: bool = False,
    conn_stats: dict | None = None, conn_unresolved: list | None = None,
) -> bool:
    output_dir = (project_root / cfg["output"]["dir"]).resolve()
    ttl_report_path = output_dir / cfg["output"]["validation_report_ttl"]
    md_report_path  = output_dir / cfg["output"]["validation_report_md"]

    if not skos_path.exists():
        log.error("SKOS file not found: %s", skos_path)
        return False

    # Resolve shape paths
    shape_paths: list[Path] = []
    for rel in cfg["shapes"]:
        p = (project_root / rel).resolve()
        if not p.exists():
            log.error("Shape file not found: %s", p)
            return False
        shape_paths.append(p)

    log.info("Loading data graph:   %s", skos_path)
    data_graph = Graph().parse(str(skos_path), format="turtle")
    log.info("  %d triples", len(data_graph))

    # Merge all shape files into one shapes graph
    shapes_graph = Graph()
    for sp in shape_paths:
        log.info("Loading shapes graph: %s", sp)
        shapes_graph.parse(str(sp), format="turtle")
    log.info("  %d shape triples total", len(shapes_graph))

    log.info("Running SHACL validation (this can take a moment on 900+ concepts)…")
    conforms, results_graph, results_text = validate(
        data_graph=data_graph,
        shacl_graph=shapes_graph,
        inference="rdfs",
        abort_on_first=False,
        allow_warnings=True,   # Warnings do not make conforms=False
        meta_shacl=False,
        advanced=True,
        debug=verbose,
    )

    # 1) TTL report — machine-readable
    results_graph.serialize(destination=str(ttl_report_path), format="turtle")
    log.info("TTL report → %s", ttl_report_path)

    # 2) Human-readable Markdown report
    stats = data_graph_stats(data_graph)
    summary = parse_report(results_graph)
    write_markdown_report(
        md_report_path, skos_path, shape_paths, stats, summary,
        conn_stats=conn_stats, conn_unresolved=conn_unresolved,
    )
    log.info("Markdown report → %s", md_report_path)

    # 3) Console output: summary + pyshacl's built-in text
    print()
    print("=" * 70)
    print("SHACL VALIDATION SUMMARY")
    print("=" * 70)
    print(f"Conforms:   {conforms}")
    print(f"Results:    {len(summary['violations'])}")
    if summary["by_severity"]:
        for sev, n in summary["by_severity"].most_common():
            print(f"  - {sev:20s} {n}")
    if summary["by_message"]:
        print("\nTop messages:")
        for msg, n in summary["by_message"].most_common(5):
            print(f"  - [{n}] {msg}")
    print("=" * 70)
    print(f"Full Markdown report: {md_report_path}")
    print(f"Full TTL report:      {ttl_report_path}")
    print("=" * 70)

    return bool(conforms)


# =============================================================================
# Orchestration
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build CeraTyOnt SKOS Turtle and validate via SHACL."
    )
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml (default: alongside this script)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--skip-build", action="store_true",
                        help="Only validate an existing SKOS Turtle file.")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Only build; don't run SHACL.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if SHACL reports any violation.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    try:
        cfg = load_config(args.config)
        project_root = args.config.parent

        # Step 1: build
        conn_stats: dict | None = None
        conn_unresolved: list | None = None
        if args.skip_build:
            skos_path = (project_root / cfg["output"]["dir"] / cfg["output"]["skos_file"]).resolve()
            log.info("--skip-build: validating existing %s", skos_path)
        else:
            skos_path, _, conn_stats, conn_unresolved = build_skos(cfg, project_root)

        # Step 2: validate
        if args.skip_validation:
            log.info("--skip-validation: done.")
            return 0

        conforms = validate_skos(
            cfg, project_root, skos_path,
            verbose=args.verbose,
            conn_stats=conn_stats, conn_unresolved=conn_unresolved,
        )
        return 0 if (conforms or not args.strict) else 1

    except FileNotFoundError as e:
        log.error("File not found: %s", e)
        return 2
    except Exception as e:
        log.exception("Run failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
