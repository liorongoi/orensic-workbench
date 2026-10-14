"""Validation lab: measure every detector on a corpus with ground truth and publish detector cards.

    python -m forensic.validation run                      # synthetic corpus, writes cards
    python -m forensic.validation run --pristine DIR       # also derive test cases from real camera JPEGs
    python -m forensic.validation run --quick              # small corpus (CI)

A card records what was measured, on what, with which thresholds, and the known
failure modes, and is shown next to the detector in the app and in reports.
"""

from .cards import load_card, load_cards

__all__ = ["load_card", "load_cards"]
