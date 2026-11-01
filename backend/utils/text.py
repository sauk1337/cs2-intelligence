"""Text, number, date and IRC formatting utilities."""
from __future__ import annotations

import hashlib
import html
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "CNY": "¥", "RUB": "₽", "BRL": "R$", "PLN": "zł"}
SPARK = "▁▂▃▄▅▆▇█"
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid"}

# mIRC formatting codes
C_BOLD, C_COLOR, C_RESET, C_ITALIC = "\x02", "\x03", "\x0f", "\x1d"
COLORS = {"white": 0, "black": 1, "blue": 2, "green": 3, "red": 4, "brown": 5, "purple": 6, "orange": 7,
          "yellow": 8, "lgreen": 9, "cyan": 10, "lcyan": 11, "lblue": 12, "pink": 13, "grey": 14, "lgrey": 15}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    """Aggressive normalisation to compare names (teams, items)."""
    s = strip_accents(s or "").lower().replace("★", " ").replace("™", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def team_norm(s: str) -> str:
    s = norm(s)
    s = re.sub(r"^(team|clan)\s+", "", s)
    s = re.sub(r"\s+(esports|esport|gaming|team|club|cs|gg)$", "", s)
    return s.replace(" ", "")


def clean_html(text: str, limit: int | None = None) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\[/?[a-z0-9*]+(=[^\]]*)?\]", " ", text)  # Steam BBCode
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def normalize_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING_PARAMS]
        path = p.path.rstrip("/") or "/"
        return urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, urlencode(q), ""))
    except ValueError:
        return url.strip()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def first_sentences(text: str, max_chars: int = 220) -> str:
    text = clean_html(text)
    text = text.replace("\n", " ")
    if len(text) <= max_chars:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for p in parts:
        if len(out) + len(p) + 1 > max_chars:
            break
        out = f"{out} {p}".strip()
    return out or text[: max_chars - 1].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- numbers
def money(value: float | None, currency: str = "EUR") -> str:
    """English format: €15.20 · $1,234.56 · ¥120.50"""
    if value is None:
        return "n/a"
    sym = CURRENCY_SYMBOLS.get(currency, currency + " ")
    return f"{sym}{value:,.2f}"


def pct(value: float | None, signed: bool = True, decimals: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{decimals}f}%" if signed else f"{value:.{decimals}f}%"


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def sparkline(values: list[float]) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    return "".join(SPARK[min(len(SPARK) - 1, int((v - lo) / span * (len(SPARK) - 1)))] for v in vals)


def downsample(values: list, n: int) -> list:
    if len(values) <= n:
        return values
    step = len(values) / n
    return [values[int(i * step)] for i in range(n)]


# ------------------------------------------------------------------ dates
def fmt_dt(ts: float | None, tz: str = "Europe/Madrid", fmt: str = "%d %b %H:%M") -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime(fmt)


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = max(0, time.time() - ts)
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def until(ts: float | None) -> str:
    if not ts:
        return "?"
    d = ts - time.time()
    if d <= 0:
        return "now"
    if d < 3600:
        return f"in {int(d // 60)}m"
    if d < 86400:
        return f"in {int(d // 3600)}h {int(d % 3600 // 60)}m"
    return f"in {int(d // 86400)}d"


# -------------------------------------------------------------------- IRC
def irc_safe(text: str) -> str:
    """Strip CR/LF and control chars, keeping formatting codes (prevents IRC command injection)."""
    text = (text or "").replace("\r", " ").replace("\n", " ").replace("\x00", "")
    return re.sub(r"[\x01\x04-\x0e\x10-\x1c\x1e\x1f]", "", text)


# A colour must always close safely: "\x03" followed by a digit would start a NEW colour
# ("\x03" + "5 sales" would become colour 5). An empty bold pair is an invisible separator.
_SAFE_END = C_COLOR + C_BOLD + C_BOLD


def color(text: str, name: str, enabled: bool = True, bg: str | None = None) -> str:
    if not enabled:
        return text
    code = f"{COLORS[name]:02d}" + (f",{COLORS[bg]:02d}" if bg else "")
    return f"{C_COLOR}{code}{text}{_SAFE_END}"


def bold(text: str, enabled: bool = True) -> str:
    return f"{C_BOLD}{text}{C_BOLD}" if enabled else text


def badge(text: str, fg: str, bg: str, enabled: bool = True) -> str:
    """Coloured block label, e.g. white-on-red ' LIVE '."""
    if not enabled:
        return f"[{text}]"
    return f"{C_BOLD}{C_COLOR}{COLORS[fg]:02d},{COLORS[bg]:02d} {text} {_SAFE_END}{C_BOLD}"


def signed_color(value: float | None, text: str | None = None, enabled: bool = True,
                 invert: bool = False) -> str:
    """Green when positive, red when negative (invert=True when lower is better)."""
    t = text if text is not None else pct(value)
    if value is None or not enabled or abs(value) < 0.05:
        return t
    good = (value > 0) != invert
    return color(t, "green" if good else "red", enabled)


def strip_codes(text: str) -> str:
    return re.sub(r"\x03\d{0,2}(,\d{1,2})?|[\x02\x0f\x1d\x1f\x16]", "", text)


def split_irc(text: str, max_bytes: int = 400) -> list[str]:
    """Split a long line into chunks that fit an IRC PRIVMSG (by UTF-8 bytes)."""
    text = irc_safe(text)
    out: list[str] = []
    while len(text.encode("utf-8")) > max_bytes:
        cut = max_bytes
        while len(text[:cut].encode("utf-8")) > max_bytes:
            cut -= 1
        space = text.rfind(" ", 0, cut)
        if space > cut * 0.5:
            cut = space
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out
