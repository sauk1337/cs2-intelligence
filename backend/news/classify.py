"""News categorisation, CS2 filter, relevance scoring and de-duplication."""
from __future__ import annotations

import re
import time

from ..utils.text import norm

CATEGORIES = {
    "torneos": "TOURNAMENT", "equipos": "TEAMS", "jugadores": "PLAYERS", "fichajes": "TRANSFER",
    "actualizacion": "UPDATE", "parche": "PATCH", "mapas": "MAPS", "economia": "SKIN ECONOMY",
    "cajas": "CASES", "operacion": "OPERATION", "mercado": "STEAM MARKET", "escena": "SCENE",
}
GROUPS = {  # !news filters
    "esports": {"torneos", "equipos", "jugadores", "fichajes", "escena"},
    "market": {"economia", "cajas", "mercado"},
    "updates": {"actualizacion", "parche", "mapas", "operacion"},
}

# (category, pattern, weight) — summed per match; the heaviest category wins
RULES: list[tuple[str, str, float]] = [
    ("parche", r"\b(patch notes?|hotfix|notas del parche|parche)\b", 3),
    ("actualizacion", r"\b(counter-strike 2 update|cs2 update|release notes|update|actualizaci[oó]n)\b", 2),
    ("mapas", r"\b(map pool|mapa|maps?|mirage|inferno|nuke|ancient|anubis|dust ?2|train|overpass|vertigo|cache)\b", 1),
    ("operacion", r"\b(operation|operaci[oó]n|armory|arsenal pass)\b", 3),
    ("cajas", r"\b(case|cases|caja|cajas|crate|capsule|c[aá]psula|case opening|unboxing|drop pool)\b", 2.5),
    ("economia", r"\b(skin|skins|knife|knives|gloves|guantes|float|pattern|sticker|stickers|trade[- ]?up|collection|colecci[oó]n|souvenir)\b", 2),
    ("mercado", r"\b(steam market|community market|mercado de steam|trade (hold|protection|ban)|marketplace|price|prices|precio)\b", 2),
    ("fichajes", r"\b(sign|signs|signed|signing|joins?|joined|parts ways|benched|bench|transfer|ficha|fichaje|fichado|loan|stand-?in|releases?|departs?|leaves|leave|replac\w+|roster|lineup|line-up)\b", 2.5),
    ("torneos", r"\b(major|iem|blast|esl pro league|pgl|starladder|playoffs?|grand final|final|qualifier|clasificatori\w+|group stage|swiss|tournament|torneo|champions|stage)\b", 2),
    ("jugadores", r"\b(mvp|rating|interview|entrevista|player|jugador|retire\w*|award|top 20)\b", 1.5),
    ("equipos", r"\b(team|equipo|organi[sz]ation|org)\b", 0.7),
    ("escena", r"\b(esports|vrs|ranking|standings|valve regional|invite|esl|hltv)\b", 0.8),
]
CS_TERMS = re.compile(r"(counter-?strike|\bcs2\b|\bcs:?go\b|\bcs\b|hltv|\bmajor\b|\biem\b|\bblast\b|\bpgl\b|esl pro league|"
                      r"faceit|valve)", re.I)
BOOSTS: list[tuple[str, float]] = [
    (r"\bmajor\b", 15), (r"\bvalve\b", 10), (r"\b(update|patch|actualizaci[oó]n)\b", 8), (r"\boperation\b", 15),
    (r"\b(new case|nueva caja|case)\b", 8), (r"\b(sign|joins?|ficha|benched|parts ways)\b", 8),
    (r"\b(grand final|champions?|win|wins|campe[oó]n)\b", 6), (r"\b(vac|ban|banned|cheat)\b", 5),
    (r"\b(official|oficial|confirmed|confirmado)\b", 5), (r"\b(trade protection|trade hold|market)\b", 8),
]


def is_cs_related(title: str, summary: str, tags: list | None = None) -> bool:
    text = f"{title} {summary} {' '.join(tags or [])}"
    return bool(CS_TERMS.search(text))


def classify(title: str, summary: str = "", tags: list | None = None, official: bool = False) -> tuple[str, list[str]]:
    text = f"{title} {title} {summary} {' '.join(tags or [])}".lower()  # the headline counts twice
    scores: dict[str, float] = {}
    matched: list[str] = []
    for cat, pat, w in RULES:
        n = len(re.findall(pat, text))
        if n:
            scores[cat] = scores.get(cat, 0) + w * min(n, 3)
            matched.append(cat)
    if official and re.search(r"\bupdate\b", title.lower()):
        scores["actualizacion"] = scores.get("actualizacion", 0) + 10
    if not scores:
        return "escena", []
    best = max(scores, key=scores.get)
    return best, sorted(set(matched))


def relevance(title: str, summary: str, category: str, source_weight: float, published_at: float,
              top_teams: set[str] | None = None, official: bool = False, llm_importance: float | None = None) -> float:
    text = f"{title} {summary}".lower()
    cat_w = {"actualizacion": 25, "parche": 25, "operacion": 25, "fichajes": 20, "cajas": 18, "torneos": 15,
             "mercado": 15, "economia": 12, "mapas": 15, "jugadores": 8, "equipos": 8, "escena": 5}
    score = source_weight + cat_w.get(category, 5)
    for pat, w in BOOSTS:
        if re.search(pat, text):
            score += w
    if top_teams:
        words = set(norm(title).split())
        if any(t in words for t in top_teams):
            score += 10
    if official:
        score += 15
    age_h = (time.time() - published_at) / 3600
    if age_h > 12:
        score -= min(30, (age_h - 12) * 1.5)
    if llm_importance is not None:
        score = score * 0.6 + llm_importance * 4  # the LLM's 0-10 importance adds up to 40
    return max(0.0, min(100.0, score))


def title_tokens(title: str) -> set[str]:
    stop = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "at", "vs", "with", "after", "de", "la", "el",
            "en", "y", "los", "las", "por", "con", "is", "as", "from", "their"}
    return {t for t in norm(title).split() if t not in stop and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
