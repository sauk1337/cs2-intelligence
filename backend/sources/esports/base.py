"""Common match model shared by every competitive source."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

TIER1_EVENTS = re.compile(r"\b(major|iem|intel extreme masters|blast (premier|open|austin|world final)|blast|"
                          r"esl pro league|pgl|cologne|katowice|esports world cup|thunderpick|starladder|"
                          r"fissure|perfect world|esl one)\b", re.I)
PLAYOFF_STAGE = re.compile(r"\b(playoffs?|quarter-?finals?|semi-?finals?|grand final|final|upper bracket final|"
                           r"lower bracket final|decider)\b", re.I)


@dataclass
class MapState:
    name: str
    s1: int | None = None
    s2: int | None = None
    status: str = "pending"   # pending | live | done | skipped

    def as_dict(self) -> dict:
        return {"name": self.name, "s1": self.s1, "s2": self.s2, "status": self.status}


@dataclass
class LiveMatch:
    source: str
    source_id: str
    team1: str
    team2: str
    event: str = ""
    stage: str = ""
    best_of: int = 1
    status: str = "upcoming"          # upcoming | live | finished
    start_time: float | None = None
    score1: int = 0                   # maps won (BO1: 1/0 when finished)
    score2: int = 0
    maps: list[MapState] = field(default_factory=list)
    current_map: str | None = None
    tier: int | None = None
    url: str | None = None
    stats: dict = field(default_factory=dict)
    fetched_at: float = field(default_factory=time.time)
    inferred: bool = False            # state inferred (e.g. from the schedule), not confirmed by the source
    has_live_score: bool = False      # the source provides live round scores

    @property
    def live_map(self) -> MapState | None:
        return next((m for m in self.maps if m.status == "live"), None)


def guess_tier(event: str) -> int:
    if re.search(r"\bmajor\b", event, re.I):
        return 0
    return 1 if TIER1_EVENTS.search(event or "") else 2


def bo_from(text: str) -> int | None:
    m = re.search(r"bo\s*(\d)", text or "", re.I) or re.search(r"best of\s*(\d)", text or "", re.I)
    return int(m.group(1)) if m else None
