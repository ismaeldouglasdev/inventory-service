"""Cross-reference parsed note lines against the OSPOS catalog.

Three-level matching, best first:

1. ``ean``      — the note carried a barcode and an active item already
                  has it → exact match.
2. ``learned``  — the supplier's internal ref was seen on a previous note
                  and is mapped to an item in ``ospos_supplier_item_map``
                  → exact match.
3. ``fuzzy``    — normalized-name similarity against the active catalog,
                  boosted when the note unit price is close to the item's
                  current cost price. High score → auto; medium → review;
                  low → suggest creating a new product.

The learned map grows with every confirmed note, so recurring purchases
become fully automatic.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUTO_THRESHOLD = 0.82
REVIEW_THRESHOLD = 0.58


def normalize(text: str) -> str:
    """Uppercase, strip accents/punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Z0-9 ]+", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) > 2}


@dataclass
class CatalogEntry:
    item_id: int
    name: str
    item_number: Optional[str]
    unit_price: float
    cost_price: float


@dataclass
class MatchResult:
    kind: str            # "ean" | "learned" | "fuzzy" | "none"
    item_id: Optional[int] = None
    item_name: Optional[str] = None
    score: float = 0.0   # confidence 0..1 (1 for exact kinds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "item_id": self.item_id,
            "item_name": self.item_name,
            "score": round(self.score, 3),
        }


class _Catalog:
    """In-memory catalog + token index built once per parse call."""

    def __init__(self, rows: list[tuple]):
        self.entries: list[CatalogEntry] = []
        self.by_barcode: dict[str, CatalogEntry] = {}
        self.token_index: dict[str, list[CatalogEntry]] = {}
        for item_id, name, item_number, unit_price, cost_price in rows:
            entry = CatalogEntry(
                item_id=item_id,
                name=name or "",
                item_number=item_number,
                unit_price=float(unit_price or 0),
                cost_price=float(cost_price or 0),
            )
            self.entries.append(entry)
            if item_number:
                self.by_barcode.setdefault(str(item_number).strip(), entry)
            for tok in tokens(entry.name):
                self.token_index.setdefault(tok, []).append(entry)


def _name_score(note_norm: str, entry: CatalogEntry) -> float:
    ratio = SequenceMatcher(None, note_norm, normalize(entry.name)).ratio()
    # Token overlap bonus: rewards shared meaningful words even when the
    # order/wording differs (e.g. "COPO PLAST 300ML" vs "COPO PLÁSTICO CRISTAL 300ML").
    note_toks = set(note_norm.split())
    entry_toks = tokens(entry.name)
    if note_toks and entry_toks:
        overlap = len(note_toks & entry_toks) / max(len(note_toks), len(entry_toks))
        ratio = max(ratio, overlap * 0.92)
    return ratio


def _price_bonus(note_unit: float, entry: CatalogEntry) -> float:
    """Small bonus when the note price corroborates the candidate."""
    reference = entry.cost_price or entry.unit_price
    if note_unit > 0 and reference > 0:
        delta = abs(note_unit - reference) / reference
        if delta <= 0.05:
            return 0.10
        if delta <= 0.20:
            return 0.05
    return 0.0


def match_items(
    parsed_items: list[dict[str, Any]],
    catalog_rows: list[tuple],
    learned_map: dict[str, int],
) -> list[MatchResult]:
    """Match every parsed line; returns one MatchResult per input line."""
    catalog = _Catalog(catalog_rows)
    results: list[MatchResult] = []

    for line in parsed_items:
        # 1. Exact barcode present in the note itself
        ean = str(line.get("ean") or "").strip()
        if ean and ean in catalog.by_barcode:
            entry = catalog.by_barcode[ean]
            results.append(MatchResult("ean", entry.item_id, entry.name, 1.0))
            continue

        # 2. Learned supplier-ref map from previous notes
        ref = str(line.get("ref") or "").strip()
        if ref and ref in learned_map:
            entry = next((e for e in catalog.entries if e.item_id == learned_map[ref]), None)
            if entry:
                results.append(MatchResult("learned", entry.item_id, entry.name, 1.0))
                continue

        # 3. Fuzzy name match
        norm = normalize(line.get("name") or "")
        note_unit = float(line.get("unit_price") or 0)
        candidates: list[CatalogEntry] = []
        for tok in set(norm.split()):
            if len(tok) > 2:
                candidates.extend(catalog.token_index.get(tok, []))
        candidates = list({id(c): c for c in candidates}.values())

        best: Optional[MatchResult] = None
        if not candidates and catalog.entries:
            # Rare: no shared token at all — brute force with cheap gate.
            for entry in catalog.entries:
                if SequenceMatcher(None, norm, normalize(entry.name)).quick_ratio() > REVIEW_THRESHOLD:
                    candidates.append(entry)

        for entry in candidates:
            score = _name_score(norm, entry) + _price_bonus(note_unit, entry)
            score = min(score, 1.0)
            if best is None or score > best.score:
                best = MatchResult(
                    "fuzzy",
                    entry.item_id,
                    entry.name,
                    score,
                )

        if best and best.score >= AUTO_THRESHOLD:
            results.append(best)
        elif best and best.score >= REVIEW_THRESHOLD:
            results.append(MatchResult("review", best.item_id, best.item_name, best.score))
        else:
            results.append(MatchResult("none"))

    return results
