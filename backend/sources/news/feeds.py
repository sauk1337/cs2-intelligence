"""News adapters: generic RSS/Atom and the Steam News API (official Valve feed)."""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field

import feedparser

from ...core.config import Config
from ...core.http import HttpClient
from ...utils.text import clean_html

STEAM_NEWS = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


@dataclass
class RawNews:
    source: str
    title: str
    url: str
    summary: str
    published_at: float
    body: str = ""
    lang: str = "en"
    tags: list = field(default_factory=list)
    official: bool = False


def parse_feed(source: str, text: str, lang: str = "en", official: bool = False) -> list[RawNews]:
    feed = feedparser.parse(text)
    out = []
    for e in feed.entries:
        title = clean_html(e.get("title", "")).strip()
        link = e.get("link") or ""
        if not title or not link:
            continue
        ts = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                ts = calendar.timegm(e[k])
                break
        content = ""
        if e.get("content"):
            content = e["content"][0].get("value", "")
        summary = clean_html(e.get("summary", "") or content, 600)
        tags = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        out.append(RawNews(source=source, title=title, url=link, summary=summary, published_at=ts or time.time(),
                           body=clean_html(content, 4000) if content else "", lang=lang, tags=tags, official=official))
    return out


def parse_steam_news(source: str, data: dict) -> list[RawNews]:
    out = []
    for n in (data or {}).get("appnews", {}).get("newsitems", []) or []:
        contents = n.get("contents") or ""
        out.append(RawNews(source=source, title=clean_html(n.get("title", "")), url=n.get("url", ""),
                           summary=clean_html(contents, 600), body=_steam_body(contents),
                           published_at=float(n.get("date") or time.time()), lang="en",
                           tags=list(n.get("tags") or []), official=n.get("feedname") == "steam_community_announcements"))
    return out


def _steam_body(contents: str) -> str:
    """Keep the patch-notes structure: [*] → '- ' and [ SECTION ] headers on their own lines."""
    t = contents.replace("[*]", "\n- ").replace("[list]", "\n").replace("[/list]", "\n")
    t = t.replace("[p]", "\n").replace("[/p]", "\n")
    return clean_html(t, 6000)


class NewsFetcher:
    def __init__(self, cfg: Config, http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http

    def feeds(self) -> dict[str, dict]:
        return {k: v for k, v in (self.cfg.get("sources.news", {}) or {}).items() if isinstance(v, dict)}

    async def fetch(self, name: str) -> list[RawNews]:
        f = self.feeds()[name]
        kind = f.get("type", "rss")
        if kind == "steam_news":
            data = await self.http.get_json(name, STEAM_NEWS, params={
                "appid": f.get("appid", 730), "count": f.get("count", 10), "maxlength": 0, "format": "json",
                **({"feeds": f["feeds"]} if f.get("feeds") else {})})
            return parse_steam_news(name, data)
        r = await self.http.get(name, f["url"], conditional=True)
        if r.not_modified:
            return []  # nothing new since the last fetch (ETag/Last-Modified)
        return parse_feed(name, r.text, f.get("lang", "en"), bool(f.get("official", False)))
