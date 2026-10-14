"""Detector cards: published accuracy for each detector, read by the app and the report."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CARD_DIR = Path(__file__).parent / "cards"


@lru_cache(maxsize=1)
def load_cards() -> dict[str, dict]:
    cards = {}
    for p in sorted(CARD_DIR.glob("*.json")):
        card = json.loads(p.read_text())
        cards[card["detector"]] = card
    return cards


def load_card(detector_id: str) -> dict | None:
    return load_cards().get(detector_id)
