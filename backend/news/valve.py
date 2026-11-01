"""Valve update analysis: facts extracted from the patch notes + possible market impact.

FACTS are literal lines from the official notes. IMPACT is a rule-based hypothesis (always
flagged as speculation) pointing at what to watch in the market; it is not a prediction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..market.items import WEAPONS

IMPACT_RULES: list[tuple[str, str]] = [
    (r"\b(new|added|introduc\w+)\b.{0,40}\b(case|terminal)\b|\b(case|terminal)\b.{0,40}\b(added|released)\b",
     "new case/terminal: usually increases supply of its skins and the case gets cheaper after launch"),
    (r"\bdrop pool\b|\bremoved from\b.{0,30}\b(drop|pool|rotation)\b|\b(rare|prime) drop\b",
     "drop pool changes: cases leaving the pool tend to become scarcer and pricier"),
    (r"\btrade (protection|hold|reversal|cooldown)\b|\btrading\b|\btrade\b",
     "trading changes: may affect liquidity and resale timing"),
    (r"\bmarket\b|\bcommunity market\b",
     "Market changes: check fees, limits and reference prices"),
    (r"\b(sticker|capsule|souvenir)\b",
     "stickers/capsules/souvenirs: possible pressure on the tournament or collection items mentioned"),
    (r"\boperation\b|\barmory\b|\bpass\b",
     "operation/armory: new collection; possible downward pressure on previous reward collections"),
    (r"\btrade-?up\b|\bcontract\b",
     "trade-up contracts: may change demand for lower-rarity skins used as inputs"),
    (r"\bmap pool\b|\bactive duty\b",
     "map pool change: may affect collections and souvenir packages of the maps involved"),
]
WEAPON_CHANGE = re.compile(r"\b(damage|price|cost|recoil|accuracy|armor penetration|fire rate|reload|nerf|buff|"
                           r"spread|movement speed|kill reward)\b", re.I)


@dataclass
class UpdateAnalysis:
    facts: list[str] = field(default_factory=list)
    impacts: list[str] = field(default_factory=list)
    weapons: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    market_relevant: bool = False


def analyze_update(title: str, body: str) -> UpdateAnalysis:
    text = body or ""
    a = UpdateAnalysis()
    a.sections = [s.strip().title() for s in re.findall(r"\[\s*([A-Z][A-Z &/-]{2,30})\s*\]", text)][:8]
    lines = [ln.strip(" -•*\t") for ln in re.split(r"\n", text)]
    a.facts = [ln for ln in lines if 12 <= len(ln) <= 220 and not ln.startswith("[")][:20]
    low = f"{title}\n{text}".lower()
    for pat, msg in IMPACT_RULES:
        if re.search(pat, low) and msg not in a.impacts:
            a.impacts.append(msg)
    for w in WEAPONS:
        for ln in a.facts:
            if w.lower() in ln.lower() and WEAPON_CHANGE.search(ln):
                if w not in a.weapons:
                    a.weapons.append(w)
    if a.weapons:
        a.impacts.append("weapon changes (" + ", ".join(a.weapons[:4]) + "): demand for their skins may shift")
    a.market_relevant = bool(a.impacts)
    return a
