"""News summarisation/translation into the bot language (settings.yaml -> bot.language).

Providers (settings.yaml -> news.summarizer.provider):
- `anthropic`      : Claude via the official SDK (ANTHROPIC_API_KEY). Translates, summarises, classifies.
                     It only summarises the given text; it never adds facts or predictions.
- `libretranslate` : translation with your own/authorised LibreTranslate server (no AI summary).
- `none`           : no translation; extractive summary (first sentences) in the original language.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..core.config import Config
from ..core.http import HttpClient, SourceError
from ..utils.text import first_sentences
from .classify import CATEGORIES

log = logging.getLogger("cs2.summarize")

LANG_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German"}


def system_prompt(lang: str) -> str:
    name = LANG_NAMES.get(lang, "English")
    return (
        "You are the news editor of an IRC bot about Counter-Strike 2. You will receive news items as JSON. "
        f"For each one return: the headline in {name} (natural wording; keep proper names of teams, players, "
        f"tournaments and items; if it is already in {name}, keep it), a summary in {name} of at most 200 "
        "characters based ONLY on the given text (do not add facts, figures or predictions that are not there), "
        "the most suitable category and an importance from 0 to 10 for the CS2 competitive and market community "
        "(10 = Major, big transfer or a Valve update affecting the economy). If the text is not about CS2, "
        "importance 0."
    )

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title_es": {"type": "string"},
                    "summary_es": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "importance": {"type": "integer"},
                },
                "required": ["id", "title_es", "summary_es", "category", "importance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass
class Summary:
    title_es: str | None
    summary_es: str | None
    category: str | None = None
    importance: float | None = None
    provider: str = "heuristic"


class Summarizer:
    def __init__(self, cfg: Config, http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http
        self._client = None

    @property
    def conf(self) -> dict:
        return self.cfg.get("settings.news.summarizer", {}) or {}

    @property
    def lang(self) -> str:
        return str(self.cfg.get("settings.bot.language", "en")).lower()

    @property
    def provider(self) -> str:
        p = str(self.conf.get("provider", "none")).lower()
        if p == "anthropic" and not self.conf.get("anthropic_api_key"):
            return "none"
        return p

    async def summarize(self, items: list[dict]) -> dict[int, Summary]:
        """items: [{id, title, summary, lang}] → {id: Summary}"""
        if not items:
            return {}
        try:
            if self.provider == "anthropic":
                return await self._anthropic(items)
            if self.provider == "libretranslate":
                return await self._libre(items)
        except Exception as e:  # summarising must never block news posting
            log.warning("Summary with %s failed (%s); using the extractive summary", self.provider, e)
        return {i["id"]: Summary(title_es=i["title"] if i.get("lang") == self.lang else None,
                                 summary_es=first_sentences(i.get("summary", ""), 220) if i.get("lang") == self.lang else None)
                for i in items}

    # ------------------------------------------------------------ Claude
    async def _anthropic(self, items: list[dict]) -> dict[int, Summary]:
        import anthropic

        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.conf.get("anthropic_api_key"), timeout=90,
                                                    max_retries=2)
        payload = [{"id": i["id"], "title": i["title"], "text": (i.get("summary") or "")[:1200]} for i in items]
        kwargs = dict(
            model=self.conf.get("model", "claude-opus-5-5"),
            max_tokens=int(self.conf.get("max_tokens", 4000)),
            system=system_prompt(self.lang),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"effort": self.conf.get("effort", "low"),
                           "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if self.conf.get("fallbacks", True):
            # If the safety classifiers decline the request, the API retries it on the recommended
            # fallback model (server-side fallback).
            resp = await self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                           fallbacks="default", **kwargs)
        else:
            resp = await self._client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise RuntimeError("the request was declined by the model")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("truncated response (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        out = {}
        for r in data.get("items", []):
            out[int(r["id"])] = Summary(title_es=r["title_es"].strip()[:300], summary_es=r["summary_es"].strip()[:260],
                                        category=r.get("category"), importance=float(r.get("importance", 0)),
                                        provider="llm")
        return out

    # ----------------------------------------------------- LibreTranslate
    async def _libre(self, items: list[dict]) -> dict[int, Summary]:
        url = str(self.conf.get("libretranslate_url", "")).rstrip("/") + "/translate"
        out = {}
        for i in items:
            short = first_sentences(i.get("summary", ""), 220)
            body = {"q": [i["title"], short], "source": "auto", "target": self.lang, "format": "text"}
            if self.conf.get("libretranslate_api_key"):
                body["api_key"] = self.conf["libretranslate_api_key"]
            try:
                r = await self.http.request("libretranslate", "POST", url, json_body=body)
                tr = r.json().get("translatedText")
            except (SourceError, ValueError) as e:
                log.info("LibreTranslate: %s", e)
                continue
            if isinstance(tr, list) and len(tr) == 2:
                out[i["id"]] = Summary(title_es=tr[0], summary_es=tr[1], provider="translate")
        return out
