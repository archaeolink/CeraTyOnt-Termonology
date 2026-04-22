"""
validate_skos.py — Load generated SKOS Turtle + SHACL shapes and validate.

Usage (from the py/ folder):
    python validate_skos.py
    python validate_skos.py --config config.yaml
    python validate_skos.py --strict      # exit non-zero on any violation

Produces a validation report (Turtle) in the output directory.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from pyshacl import validate
from rdflib import Graph

log = logging.getLogger("validate_skos")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(cfg_path: Path, strict: bool = False, verbose: bool = False) -> bool:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    cfg = load_config(cfg_path)
    project_root = cfg_path.parent
    output_dir = (project_root / cfg["output"]["dir"]).resolve()
    skos_path   = output_dir / cfg["output"]["skos_file"]
    shapes_path = (project_root / cfg["output"]["shapes_file"]).resolve()
    report_path = output_dir / cfg["output"]["validation_report"]

    if not skos_path.exists():
        log.error("SKOS file not found: %s — run build_skos.py first.", skos_path)
        return False
    if not shapes_path.exists():
        log.error("SHACL shapes file not found: %s", shapes_path)
        return False

    log.info("Loading data graph:   %s", skos_path)
    data_graph = Graph().parse(str(skos_path), format="turtle")
    log.info("  %d triples loaded", len(data_graph))

    log.info("Loading shapes graph: %s", shapes_path)
    shapes_graph = Graph().parse(str(shapes_path), format="turtle")
    log.info("  %d triples loaded", len(shapes_graph))

    log.info("Running SHACL validation…")
    conforms, results_graph, results_text = validate(
        data_graph=data_graph,
        shacl_graph=shapes_graph,
        inference="rdfs",         # enable rdfs:subClassOf reasoning
        abort_on_first=False,
        allow_warnings=True,
        meta_shacl=False,
        advanced=True,            # enable SPARQL-based constraints
        debug=verbose,
    )

    # Always write the machine-readable report
    results_graph.serialize(destination=str(report_path), format="turtle")
    log.info("Report written to:    %s", report_path)

    # Human summary
    print()
    print("=" * 70)
    print("SHACL VALIDATION RESULT")
    print("=" * 70)
    print(results_text)
    print("=" * 70)
    if conforms:
        log.info("✓ Graph conforms to all shapes.")
    else:
        log.warning("✗ Graph has validation issues — see report above.")

    return bool(conforms) or not strict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SHACL validation for generated SKOS.")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml (default: alongside this script)",
    )
    parser.add_argument("--strict", action="store_true",
                        help="Exit with code 1 if any SHACL violation is found.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    try:
        ok = run(args.config, strict=args.strict, verbose=args.verbose)
    except Exception as e:
        log.exception("Validation failed: %s", e)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
