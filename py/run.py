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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pyshacl import validate
from rdflib import DCTERMS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DC, FOAF, RDF, RDFS, SKOS, XSD

log = logging.getLogger("run")

# SHACL + VANN namespaces (not predefined in rdflib)
SH = Namespace("http://www.w3.org/ns/shacl#")
VANN = Namespace("http://purl.org/vocab/vann/")


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

        self.graph = Graph()
        self.graph.bind("skos", SKOS)
        self.graph.bind("foaf", FOAF)
        self.graph.bind("dct", DCTERMS)
        self.graph.bind("dc", DC)
        self.graph.bind("rdfs", RDFS)
        self.graph.bind("xsd", XSD)
        self.graph.bind("vann", VANN)
        self.graph.bind("ceratyont", self.BASE)

        self.publisher_by_label: dict[str, URIRef] = {}
        # populated in build_facets(); each kind → its facet-concept IRI
        self.facet_by_kind: dict[str, URIRef] = {}

    def concept_iri(self, kind: str, identifier: str) -> URIRef:
        return URIRef(f"{self.BASE}{self.cfg['iri_prefixes'][kind]}{identifier}")

    def build_scheme(self) -> None:
        g, s, lang = self.graph, self.SCHEME, self.lang
        meta = self.cfg["scheme"]
        g.add((s, RDF.type, SKOS.ConceptScheme))
        # Language-tagged literals (SkoHub SHACL requires rdf:langString)
        g.add((s, DCTERMS.title, Literal(meta["title"], lang=lang)))
        g.add((s, DCTERMS.description, Literal(meta["description"], lang=lang)))
        g.add((s, SKOS.prefLabel, Literal(meta["title"], lang=lang)))
        # Plain-string metadata
        g.add((s, DCTERMS.creator, Literal(meta["creator"])))
        g.add((s, DCTERMS.contributor, Literal(meta["contributor"])))
        g.add((s, DCTERMS.publisher, Literal(meta["publisher"])))
        g.add((s, DCTERMS.rights, Literal(meta["rights"])))
        g.add((s, DCTERMS.hasVersion, Literal(str(meta["version"]))))
        # SkoHub requires these:
        g.add((s, DCTERMS.license, URIRef(meta["license"])))
        g.add((s, VANN.preferredNamespaceUri,
               Literal(meta["preferred_namespace_uri"], datatype=XSD.string)))
        log.info("ConceptScheme: %s", s)

    def build_facets(self) -> None:
        """Create one skos:Concept per facet (acts as a branch root / Top-Concept)."""
        for kind, meta in self.cfg["facets"].items():
            iri = URIRef(f"{self.BASE}{meta['local_name']}")
            self.facet_by_kind[kind] = iri
            self.graph.add((iri, RDF.type, SKOS.Concept))
            self.graph.add((iri, SKOS.inScheme, self.SCHEME))
            self.graph.add((iri, SKOS.prefLabel, Literal(meta["label"], lang=self.lang)))
            self.graph.add((iri, RDFS.label, Literal(meta["label"], lang=self.lang)))
            if meta.get("definition"):
                self.graph.add((iri, SKOS.definition,
                                Literal(meta["definition"], lang=self.lang)))
            # Facet concepts are the top concepts of the scheme
            self.graph.add((iri, SKOS.topConceptOf, self.SCHEME))
            self.graph.add((self.SCHEME, SKOS.hasTopConcept, iri))
            log.info("Facet concept: %s (%s)", meta["label"], iri)

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

            self.graph.add((concept, RDF.type, SKOS.Concept))
            self.graph.add((concept, SKOS.inScheme, self.SCHEME))
            self.graph.add((concept, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((concept, RDFS.label, Literal(label, lang=self.lang)))

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

            self.graph.add((concept, RDF.type, SKOS.Concept))
            self.graph.add((concept, SKOS.inScheme, self.SCHEME))
            self.graph.add((concept, SKOS.prefLabel, Literal(label, lang=self.lang)))
            self.graph.add((concept, RDFS.label, Literal(label, lang=self.lang)))

            img = str(row[image_col]).strip() if image_col in row else ""
            if img and not is_null(img, self.null_markers):
                self.graph.add((concept, FOAF.depiction, URIRef(f"{self.IMG_BASE}{img}")))

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


def build_skos(cfg: dict, project_root: Path) -> tuple[Path, int]:
    """Build the SKOS graph, serialize to Turtle, return (path, triple count)."""
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
    b = SkosBuilder(cfg)
    b.build_scheme()
    b.build_facets()                      # facet top-concepts first
    b.build_generics(df_generic)
    b.build_traditions(df_tradition)
    b.build_services(df_service)
    b.build_publishers(df_publisher)      # must come before potforms (broader lookup)
    b.build_potforms(df_potforms)

    out_path = output_dir / cfg["output"]["skos_file"]
    b.graph.serialize(destination=str(out_path), format="turtle")
    log.info("✓ Wrote %d triples to %s", len(b.graph), out_path)
    return out_path, len(b.graph)


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
    return {
        "triples": len(data_graph),
        "concepts": len(concepts),
        "collections": len(collections),
        "schemes": len(schemes),
        "depictions": depictions,
        "broader_edges": broader,
    }


def write_markdown_report(
    md_path: Path,
    skos_path: Path,
    shape_paths: list[Path],
    stats: dict,
    summary: dict,
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
        "",
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
    cfg: dict, project_root: Path, skos_path: Path, verbose: bool = False
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
    write_markdown_report(md_report_path, skos_path, shape_paths, stats, summary)
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
        if args.skip_build:
            skos_path = (project_root / cfg["output"]["dir"] / cfg["output"]["skos_file"]).resolve()
            log.info("--skip-build: validating existing %s", skos_path)
        else:
            skos_path, _ = build_skos(cfg, project_root)

        # Step 2: validate
        if args.skip_validation:
            log.info("--skip-validation: done.")
            return 0

        conforms = validate_skos(cfg, project_root, skos_path, verbose=args.verbose)
        return 0 if (conforms or not args.strict) else 1

    except FileNotFoundError as e:
        log.error("File not found: %s", e)
        return 2
    except Exception as e:
        log.exception("Run failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
