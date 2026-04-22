"""
build_skos.py — CeraTyOnt → SKOS Turtle converter
==================================================

Reads 5 CSV lookup/data tables and produces a SKOS-compliant terminology
(Turtle serialization) for the CeraTyOnt ceramic typology.

Usage (from the py/ folder):
    python build_skos.py
    python build_skos.py --config config.yaml
    python build_skos.py --verbose

Modelling overview:
- One skos:ConceptScheme (the terminology itself)
- One skos:Collection per class (Generic, Tradition, Service, Publisher, Potform)
  acting as a FACET grouping the concepts of that class via skos:member
- Each row in each table becomes one skos:Concept
- Potforms are linked to their Publisher via skos:broader (per user spec)
- Images → foaf:depiction (URL = image_base_url + image filename)
- Publisher "NULL" (sentinel) → Potform keeps no publisher link, gets a skos:note instead
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from rdflib import DCTERMS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DC, FOAF, RDF, RDFS, SKOS, XSD

log = logging.getLogger("build_skos")


# --- Config loading --------------------------------------------------------

def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    log.debug("Loaded config from %s", path)
    return cfg


# --- Helpers ---------------------------------------------------------------

def is_null(val: Any, markers: list[str]) -> bool:
    """True if the CSV cell is empty or matches a null-marker sentinel."""
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    return str(val).strip() in markers


def read_csv(path: Path) -> pd.DataFrame:
    """Read CSV with UTF-8 & handle Windows \\r\\n line endings."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    log.info("  %s: %d rows, columns=%s", path.name, len(df), list(df.columns))
    return df


def make_color_note(row: pd.Series, color_col: str | None) -> str | None:
    """Extract a color attribute as a human-readable note (if present)."""
    if color_col and color_col in row and row[color_col].strip():
        return f"display color: {row[color_col].strip()}"
    return None


# --- Main build routines ---------------------------------------------------

class SkosBuilder:
    """Assembles the SKOS graph step by step. Each build_* method is idempotent
    on its own concepts and only modifies `self.graph`."""

    def __init__(self, cfg: dict, data_dir: Path):
        self.cfg = cfg
        self.data_dir = data_dir
        self.lang = cfg["language"]
        self.null_markers = cfg["null_markers"]

        # Namespaces
        self.BASE = Namespace(cfg["namespaces"]["base_uri"])
        self.SCHEME = URIRef(cfg["namespaces"]["scheme_iri"])
        self.IMG_BASE = cfg["namespaces"]["image_base_url"]

        # Graph + standard binds
        self.graph = Graph()
        self.graph.bind("skos", SKOS)
        self.graph.bind("foaf", FOAF)
        self.graph.bind("dct", DCTERMS)
        self.graph.bind("dc", DC)
        self.graph.bind("rdfs", RDFS)
        self.graph.bind("xsd", XSD)
        self.graph.bind("ceratyont", self.BASE)

        # Cache: publisher label (string) → URIRef, populated in build_publishers()
        self.publisher_by_label: dict[str, URIRef] = {}

    # ---- IRI helpers ----
    def concept_iri(self, kind: str, identifier: str) -> URIRef:
        prefix = self.cfg["iri_prefixes"][kind]
        return URIRef(f"{self.BASE}{prefix}{identifier}")

    def collection_iri(self, kind: str) -> URIRef:
        local = self.cfg["collections"][kind]["local_name"]
        return URIRef(f"{self.BASE}{local}")

    # ---- Scheme + collections ----
    def build_scheme(self) -> None:
        g, s = self.graph, self.SCHEME
        meta = self.cfg["scheme"]
        g.add((s, RDF.type, SKOS.ConceptScheme))
        g.add((s, DCTERMS.title, Literal(meta["title"], lang=self.lang)))
        g.add((s, DCTERMS.description, Literal(meta["description"], lang=self.lang)))
        g.add((s, DCTERMS.creator, Literal(meta["creator"])))
        g.add((s, DCTERMS.contributor, Literal(meta["contributor"])))
        g.add((s, DCTERMS.publisher, Literal(meta["publisher"])))
        g.add((s, DCTERMS.rights, Literal(meta["rights"])))
        g.add((s, DCTERMS.hasVersion, Literal(str(meta["version"]))))
        log.info("ConceptScheme: %s", s)

    def build_collection(self, kind: str) -> URIRef:
        iri = self.collection_iri(kind)
        label = self.cfg["collections"][kind]["label"]
        self.graph.add((iri, RDF.type, SKOS.Collection))
        self.graph.add((iri, SKOS.prefLabel, Literal(label, lang=self.lang)))
        self.graph.add((iri, RDFS.label, Literal(label, lang=self.lang)))
        return iri

    # ---- Concept builders (one per table) ----
    def _build_simple_concepts(
        self,
        kind: str,
        df: pd.DataFrame,
        top_concept: bool,
        color_col_key: str | None = None,
    ) -> dict[str, URIRef]:
        """Shared logic for Generic/Tradition/Service/Publisher (no cross-refs)."""
        col = self.cfg["columns"][kind]
        id_col = col["id"]
        label_col = col["label"]
        color_col = col.get(color_col_key) if color_col_key else None

        collection_iri = self.build_collection(kind)
        built: dict[str, URIRef] = {}

        for _, row in df.iterrows():
            if is_null(row[id_col], self.null_markers):
                log.warning("  skipping %s row with empty id: %s", kind, row.to_dict())
                continue
            ident = str(row[id_col]).strip()
            label = str(row[label_col]).strip()
            concept = self.concept_iri(kind, ident)

            # core SKOS
            self.graph.add((concept, RDF.type, SKOS.Concept))
            self.graph.add((concept, SKOS.inScheme, self.SCHEME))
            self.graph.add((concept, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((concept, RDFS.label, Literal(label, lang=self.lang)))

            if top_concept:
                self.graph.add((concept, SKOS.topConceptOf, self.SCHEME))
                self.graph.add((self.SCHEME, SKOS.hasTopConcept, concept))

            # facet membership
            self.graph.add((collection_iri, SKOS.member, concept))

            # optional color annotation
            note = make_color_note(row, color_col)
            if note:
                self.graph.add((concept, SKOS.note, Literal(note, lang=self.lang)))

            built[label] = concept

        log.info("  built %d %s concepts", len(built), kind)
        return built

    def build_generics(self, df: pd.DataFrame) -> dict[str, URIRef]:
        # Generic Potforms are top concepts (per user: Generic as facet, Potform narrower)
        return self._build_simple_concepts("generic", df, top_concept=True)

    def build_traditions(self, df: pd.DataFrame) -> dict[str, URIRef]:
        return self._build_simple_concepts(
            "tradition", df, top_concept=True, color_col_key="color"
        )

    def build_services(self, df: pd.DataFrame) -> dict[str, URIRef]:
        return self._build_simple_concepts(
            "service", df, top_concept=True, color_col_key="color"
        )

    def build_publishers(self, df: pd.DataFrame) -> dict[str, URIRef]:
        built = self._build_simple_concepts(
            "publisher", df, top_concept=True, color_col_key="color"
        )
        # cache for potform lookup (by publisher label)
        self.publisher_by_label = dict(built)
        return built

    def build_potforms(self, df: pd.DataFrame) -> dict[str, URIRef]:
        col = self.cfg["columns"]["potforms"]
        id_col = col["id"]
        label_col = col["label"]
        image_col = col["image"]
        publisher_col = col["publisher"]

        collection_iri = self.build_collection("potform")
        built: dict[str, URIRef] = {}
        unresolved_publishers: set[str] = set()

        for _, row in df.iterrows():
            if is_null(row[id_col], self.null_markers):
                log.warning("  skipping potform row with empty id: %s", row.to_dict())
                continue
            ident = str(row[id_col]).strip()
            label = str(row[label_col]).strip()
            concept = self.concept_iri("potform", ident)

            # core SKOS
            self.graph.add((concept, RDF.type, SKOS.Concept))
            self.graph.add((concept, SKOS.inScheme, self.SCHEME))
            self.graph.add((concept, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((concept, RDFS.label, Literal(label, lang=self.lang)))

            # facet membership
            self.graph.add((collection_iri, SKOS.member, concept))

            # image → foaf:depiction
            img = str(row[image_col]).strip() if image_col in row else ""
            if img and not is_null(img, self.null_markers):
                self.graph.add((concept, FOAF.depiction, URIRef(f"{self.IMG_BASE}{img}")))

            # publisher link → skos:broader (per user spec)
            pub_label = str(row[publisher_col]).strip() if publisher_col in row else ""
            if is_null(pub_label, self.null_markers):
                self.graph.add((
                    concept,
                    SKOS.note,
                    Literal("publisher: unknown (NULL in source data)", lang=self.lang),
                ))
            elif pub_label in self.publisher_by_label:
                pub_iri = self.publisher_by_label[pub_label]
                self.graph.add((concept, SKOS.broader, pub_iri))
                self.graph.add((pub_iri, SKOS.narrower, concept))
            else:
                unresolved_publishers.add(pub_label)
                self.graph.add((
                    concept,
                    SKOS.note,
                    Literal(f"publisher '{pub_label}' not found in publisher lookup table",
                            lang=self.lang),
                ))

            built[ident] = concept

        if unresolved_publishers:
            log.warning(
                "  %d potform(s) reference unknown publishers: %s",
                sum(1 for _, row in df.iterrows()
                    if str(row[publisher_col]).strip() in unresolved_publishers),
                sorted(unresolved_publishers),
            )
        log.info("  built %d potform concepts", len(built))
        return built


# --- Orchestration ---------------------------------------------------------

def run(cfg_path: Path, verbose: bool = False) -> Path:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    cfg = load_config(cfg_path)
    project_root = cfg_path.parent  # py/ folder
    data_dir = (project_root / cfg["input"]["data_dir"]).resolve()
    output_dir = (project_root / cfg["output"]["dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("Reading CSVs from %s", data_dir)
    files = cfg["input"]["files"]
    df_generic   = read_csv(data_dir / files["generic"])
    df_tradition = read_csv(data_dir / files["tradition"])
    df_service   = read_csv(data_dir / files["service"])
    df_publisher = read_csv(data_dir / files["publisher"])
    df_potforms  = read_csv(data_dir / files["potforms"])

    log.info("Building SKOS graph…")
    builder = SkosBuilder(cfg, data_dir)
    builder.build_scheme()
    # Order matters: publishers must exist before potforms reference them.
    builder.build_generics(df_generic)
    builder.build_traditions(df_tradition)
    builder.build_services(df_service)
    builder.build_publishers(df_publisher)
    builder.build_potforms(df_potforms)

    out_path = output_dir / cfg["output"]["skos_file"]
    builder.graph.serialize(destination=str(out_path), format="turtle")
    log.info("✓ Wrote %d triples to %s", len(builder.graph), out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build SKOS Turtle from CeraTyOnt CSVs.")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml (default: alongside this script)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    try:
        run(args.config, verbose=args.verbose)
    except FileNotFoundError as e:
        log.error("File not found: %s", e)
        return 2
    except Exception as e:
        log.exception("Build failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
