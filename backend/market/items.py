"""Normalisation of CS2 items from Steam's `market_hash_name`.

Goal: never mix items with different characteristics. An item's unique key is its
market_hash_name (+ the variant/phase when the source distinguishes it, e.g. Doppler Phase 2).
StatTrak™, Souvenir and every wear are distinct items with distinct prices.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..utils.text import norm

EXTERIORS = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
EXTERIOR_ABBR = {"Factory New": "FN", "Minimal Wear": "MW", "Field-Tested": "FT", "Well-Worn": "WW",
                 "Battle-Scarred": "BS"}
ABBR_EXTERIOR = {v.lower(): k for k, v in EXTERIOR_ABBR.items()}

WEAPONS = {
    "AK-47": "rifle", "M4A4": "rifle", "M4A1-S": "rifle", "AWP": "sniper", "SSG 08": "sniper", "SCAR-20": "sniper",
    "G3SG1": "sniper", "AUG": "rifle", "SG 553": "rifle", "FAMAS": "rifle", "Galil AR": "rifle",
    "Desert Eagle": "pistol", "USP-S": "pistol", "Glock-18": "pistol", "P250": "pistol", "Five-SeveN": "pistol",
    "Tec-9": "pistol", "CZ75-Auto": "pistol", "Dual Berettas": "pistol", "P2000": "pistol", "R8 Revolver": "pistol",
    "MAC-10": "smg", "MP9": "smg", "MP7": "smg", "MP5-SD": "smg", "UMP-45": "smg", "P90": "smg", "PP-Bizon": "smg",
    "Nova": "heavy", "XM1014": "heavy", "Sawed-Off": "heavy", "MAG-7": "heavy", "M249": "heavy", "Negev": "heavy",
    "Zeus x27": "equipment",
}
GLOVE_WORDS = ("Gloves", "Hand Wraps")

# Common IRC search aliases → normalised canonical term
ALIASES = {
    "ak": "ak 47", "ak47": "ak 47", "m4a1s": "m4a1 s", "m4a1": "m4a1 s", "m4": "m4a4", "deagle": "desert eagle",
    "usp": "usp s", "usps": "usp s", "glock": "glock 18", "five7": "five seven", "fiveseven": "five seven",
    "cz": "cz75 auto", "cz75": "cz75 auto", "tec9": "tec 9", "dualies": "dual berettas", "r8": "r8 revolver",
    "mac10": "mac 10", "ump": "ump 45", "bizon": "pp bizon", "galil": "galil ar", "sg": "sg 553", "ssg": "ssg 08",
    "scout": "ssg 08", "kara": "karambit", "bayo": "bayonet", "m9": "m9 bayonet", "dlore": "dragon lore",
    "hyperbeast": "hyper beast", "neonoir": "neo noir", "mp5": "mp5 sd", "xm": "xm1014", "zeus": "zeus x27",
}
FLAG_TOKENS = {"st": "stattrak", "stattrak": "stattrak", "stat": "stattrak", "souvenir": "souvenir",
               "sv": "souvenir"}


@dataclass
class ParsedItem:
    market_hash_name: str
    base_name: str
    item_type: str
    weapon: str | None = None
    skin: str | None = None
    exterior: str | None = None
    stattrak: bool = False
    souvenir: bool = False
    phase: str | None = None
    tournament: str | None = None

    @property
    def key(self) -> str:
        return f"{self.market_hash_name} [{self.phase}]" if self.phase else self.market_hash_name

    @property
    def short(self) -> str:
        """Compact name for IRC: 'ST AK-47 | Redline (FT)'."""
        name = self.base_name
        if self.exterior:
            name += f" ({EXTERIOR_ABBR[self.exterior]})"
        if self.phase:
            name += f" [{self.phase}]"
        pre = ("ST " if self.stattrak else "") + ("Souv. " if self.souvenir else "")
        return pre + name


_TOURNAMENT_RE = re.compile(r"\b((?:Katowice|Cologne|Atlanta|Boston|London|Berlin|Stockholm|Antwerp|Rio|Paris|"
                            r"Copenhagen|Shanghai|Austin|Budapest|Cluj-Napoca|Columbus|MLG Columbus|DreamHack|"
                            r"Krakow|Kraków|Singapore|Cologne)\s+20\d\d)\b")


def parse_market_name(name: str, version: str | None = None) -> ParsedItem:
    original = name.strip()
    s = original
    stattrak = "StatTrak™" in s
    souvenir = s.startswith("Souvenir ") and "Package" not in s
    star = s.startswith("★")
    s = s.replace("★", "").replace("StatTrak™", "").strip()
    if souvenir:
        s = s[len("Souvenir "):].strip()
    exterior = None
    m = re.search(r"\((Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred)\)\s*$", s)
    if m:
        exterior = m.group(1)
        s = s[: m.start()].strip()
    tour = _TOURNAMENT_RE.search(original)
    tournament = tour.group(1) if tour else None
    phase = (version or "").strip() or None

    weapon = skin = None
    parts = [p.strip() for p in s.split("|")]
    lower = original.lower()
    if star and any(g in parts[0] for g in GLOVE_WORDS):
        item_type, weapon = "gloves", parts[0]
        skin = parts[1] if len(parts) > 1 else None
    elif star:
        item_type, weapon = "knife", parts[0]
        skin = parts[1] if len(parts) > 1 else "Vanilla"
    elif parts[0] in WEAPONS:
        item_type, weapon = "weapon", parts[0]
        skin = parts[1] if len(parts) > 1 else None
    elif lower.startswith("sticker |") or lower.startswith("sticker slab |"):
        item_type = "sticker"
    elif lower.startswith("sealed graffiti") or lower.startswith("graffiti |"):
        item_type = "graffiti"
    elif lower.startswith("music kit"):
        item_type = "music_kit"
    elif lower.startswith("patch |"):
        item_type = "patch"
    elif lower.startswith("charm |"):
        item_type = "charm"
    elif "souvenir package" in lower:
        item_type = "souvenir_package"
    elif "capsule" in lower or "sticker collection" in lower or "autograph" in lower and "capsule" in lower:
        item_type = "capsule"
    elif lower.endswith(" case") or "weapon case" in lower or lower.endswith(" terminal"):
        item_type = "case"
    elif lower.endswith(" key") or lower.endswith(" pass") or "case key" in lower:
        item_type = "key"
    elif len(parts) == 2 and not exterior:
        item_type = "agent"
    else:
        item_type = "other"

    base = s if item_type in ("weapon", "knife", "gloves") else original.replace("StatTrak™ ", "")
    if item_type in ("weapon", "knife", "gloves"):
        base = f"★ {s}" if star else s
    return ParsedItem(market_hash_name=original, base_name=base, item_type=item_type, weapon=weapon, skin=skin,
                      exterior=exterior, stattrak=stattrak, souvenir=souvenir, phase=phase, tournament=tournament)


def search_key(name: str) -> str:
    return norm(name.replace("StatTrak™", "stattrak"))


@dataclass
class Query:
    terms: list[str]
    exterior: str | None
    stattrak: bool | None
    souvenir: bool | None
    raw: str


def parse_query(text: str) -> Query:
    """Parse a free-text IRC search: 'ak redline ft st' → terms + filters."""
    raw = text.strip()
    t = raw
    exterior = None
    for ext in EXTERIORS:
        if ext.lower() in t.lower():
            exterior = ext
            t = re.sub(re.escape(ext), " ", t, flags=re.I)
    t = re.sub(r"[()\[\]|]", " ", t)
    tokens = norm(t).split()
    terms: list[str] = []
    stattrak = souvenir = None
    for tok in tokens:
        if tok in ABBR_EXTERIOR and exterior is None:
            exterior = ABBR_EXTERIOR[tok]
            continue
        flag = FLAG_TOKENS.get(tok)
        if flag == "stattrak":
            stattrak = True
            continue
        if flag == "souvenir":
            souvenir = True
            continue
        terms.extend(ALIASES.get(tok, tok).split())
    return Query(terms=terms, exterior=exterior, stattrak=stattrak, souvenir=souvenir, raw=raw)
