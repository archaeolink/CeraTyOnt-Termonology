"""
abbreviations.py — Publisher abbreviations (edit this file to correct guesses)
==============================================================================

This is the single source of truth for publisher abbreviations used as
skos:notation and as the first token of skos:altLabel on every Potform.

Example: a Potform with prefLabel "15" published by "Dragendorff" will get:
    skos:prefLabel "15"@en
    skos:notation  "Drag. 15"
    skos:altLabel  "Drag. 15"@en

If the prefLabel already starts with the abbreviation (e.g. "Consp. 51.1"
from publisher "Conspectus"), no duplication is emitted — notation stays as
the label itself and no altLabel is added.

**How to use:**
- Every entry has a status comment: # CONFIRMED (matches actual label usage
  in the source data) or # GUESSED (Claude's guess, please verify).
- Review the GUESSED ones and either:
    - correct the abbreviation string, and flip the comment to # CONFIRMED, or
    - flip the comment to # CONFIRMED if the guess is actually right.
- When you add a new publisher, just append a new line.
- Publishers not listed here fall back to their full name (no abbreviation).

The CONFIRMED entries below were derived from a scan of actual potform labels
in v_ceratyont_potforms_distinct.csv. Re-run that scan whenever the source
data changes substantially.
"""

PUBLISHER_ABBREVIATIONS: dict[str, str] = {
    # Abbreviations that match the actual label prefixes in the source data.
    "Bet":         "Bet",         # CONFIRMED (200/200 labels start with "Bet")
    "Conspectus":  "Consp.",      # CONFIRMED (310/310 labels start with "Consp.")
    "Curle":       "Curle",       # CONFIRMED (4/4 labels start with "Curle")
    "Déchelette":  "Déch.",       # CONFIRMED (8/9 labels start with "Déch.")
    "Hayes":       "Hayes",       # CONFIRMED (174/174 labels start with "Hayes")
    "Hermet":      "Hermet",      # CONFIRMED (6/6 labels start with "Hermet")
    "Loeschke":    "Loes.",       # CONFIRMED — matches "Loes. 1a" etc. (note: some labels start with "Haltern" instead)
    "Vernhet":     "Vernhet",     # CONFIRMED (21/21 labels start with "Vernhet")

    # Guessed — source labels use only form numbers (e.g. "15", "15/17"),
    # no publisher prefix. Review and correct in-place if needed.
    "Bushe-Fox":   "Bushe-Fox",   # GUESSED (not usually abbreviated)
    "Dragendorff": "Drag.",       # GUESSED (classical terra sigillata abbreviation)
    "Knorr":       "Knorr",       # GUESSED
    "Ludowici":    "Ludow.",      # GUESSED
    "Ritterling":  "Ritt.",       # CONFIRMED ("Ritt. R. 8g" is correct — the "R." in source labels stands for "Rouletted" (a form attribute), not a duplicate publisher abbreviation)
    "Stanfield":   "Stanf.",      # GUESSED
    "Walters":     "Walters",     # GUESSED
}


def abbreviate(publisher_label: str) -> str:
    """Return the abbreviation for a publisher, or the full label if unknown."""
    return PUBLISHER_ABBREVIATIONS.get(publisher_label, publisher_label)
