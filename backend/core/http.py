"""Centralised HTTP client for every source.

Responsibilities:
- Outbound traffic through the authorised public IP (`OUTBOUND_IP` → httpx local_address) or an authorised proxy.
- Per-source profile: User-Agent, headers, own session cookies, timeouts.
- Per-source rate limit (minimum interval between requests + maximum concurrency).
- Retries with exponential backoff; honours `Retry-After` on 429.
- TTL cache and conditional GET (ETag / Last-Modified) for incremental fetching.
- robots.txt when the source requires it.
- Block detection (403/401/challenge): the source is paused and the incident is recorded.
  Captchas, challenges and authentication are NEVER bypassed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .cache import TTLCache
from .config import Config
from .health import HealthRegistry

log = logging.getLogger("cs2.http")

CHALLENGE_MARKERS = ("<title>just a moment", "challenge-platform", "cf-chl-", "captcha", "attention required! | cloudflare")


class SourceError(Exception):
    """Generic source error."""


class SourceDisabled(SourceError):
    pass


class SourceBlocked(SourceError):
    """The source refused access (403/401/challenge) or is paused after a previous refusal."""


class RobotsDisallowed(SourceError):
    pass


@dataclass
class Response:
    status: int
    text: str
    headers: dict
    url: str
    from_cache: bool = False
    not_modified: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class SourceProfile:
    name: str
    user_agent: str
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    timeout: float = 20.0
    min_interval: float = 1.0
    max_concurrency: int = 2
    retries: int = 2
    cache_ttl: float = 0.0
    respect_robots: bool = False
    block_cooldown: float = 900.0
    detect_challenge: bool = True


class _Limiter:
    def __init__(self, min_interval: float, concurrency: int) -> None:
        self.min_interval = min_interval
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def __aenter__(self):
        await self.sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(now, self._next) + self.min_interval
        return self

    async def __aexit__(self, *exc):
        self.sem.release()


class HttpClient:
    def __init__(self, cfg: Config, health: HealthRegistry, cache: TTLCache | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.health = health
        self.cache = cache or TTLCache()
        self._limiters: dict[str, _Limiter] = {}
        self._validators: dict[str, tuple[str | None, str | None, Response]] = {}
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        egress = cfg.get("sources.egress", {}) or {}
        self.default_ua = egress.get("user_agent") or "CS2IntelligenceBot/1.0"
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "http2": bool(egress.get("http2", False)),
            "limits": httpx.Limits(max_connections=int(egress.get("max_connections", 20)),
                                   max_keepalive_connections=10),
            "timeout": httpx.Timeout(float(egress.get("timeout", 20))),
        }
        if transport is not None:
            kwargs["transport"] = transport
        else:
            local_ip = (egress.get("outbound_ip") or "").strip() or None
            proxy = (egress.get("proxy") or "").strip() or None
            # local_address forces the source IP (servers with several IPs: use the authorised one)
            kwargs["transport"] = httpx.AsyncHTTPTransport(local_address=local_ip, retries=0,
                                                           http2=kwargs["http2"], proxy=proxy)
        self._client = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    # --------------------------------------------------------------- profiles
    def profile(self, source: str) -> SourceProfile:
        defaults = self.cfg.get("sources.http_defaults", {}) or {}
        sc = self.cfg.source(source)
        http_cfg = {**defaults, **(sc.get("http") or {})}
        headers = dict(http_cfg.get("headers") or {})
        # arbitrary authorisation header "Name: value" (e.g. the one agreed with a provider)
        raw_auth = (sc.get("auth_header") or "").strip()
        if ":" in raw_auth:
            k, v = raw_auth.split(":", 1)
            headers[k.strip()] = v.strip()
        cookies = {}
        raw_cookie = (sc.get("cookie") or "").strip()
        if raw_cookie:
            for part in raw_cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
        return SourceProfile(
            name=source,
            user_agent=sc.get("user_agent") or http_cfg.get("user_agent") or self.default_ua,
            headers=headers,
            cookies=cookies,
            timeout=float(http_cfg.get("timeout", 20)),
            min_interval=float(http_cfg.get("min_interval", 1.0)),
            max_concurrency=int(http_cfg.get("max_concurrency", 2)),
            retries=int(http_cfg.get("retries", 2)),
            cache_ttl=float(http_cfg.get("cache_ttl", 0)),
            respect_robots=bool(http_cfg.get("respect_robots", False)),
            block_cooldown=float(http_cfg.get("block_cooldown", 900)),
            detect_challenge=bool(http_cfg.get("detect_challenge", True)),
        )

    def _limiter(self, p: SourceProfile) -> _Limiter:
        lim = self._limiters.get(p.name)
        if lim is None or lim.min_interval != p.min_interval:
            lim = self._limiters[p.name] = _Limiter(p.min_interval, p.max_concurrency)
        return lim

    # ----------------------------------------------------------------- robots
    async def _robots_allowed(self, p: SourceProfile, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(base)
        if not cached or cached[0] < time.time():
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                r = await self._client.get(base + "/robots.txt", headers={"User-Agent": p.user_agent}, timeout=10)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt → allowed
            except httpx.HTTPError:
                rp = None
            cached = self._robots[base] = (time.time() + 86400, rp)
        rp = cached[1]
        return True if rp is None else rp.can_fetch(p.user_agent, url)

    # ---------------------------------------------------------------- request
    async def request(self, source: str, method: str, url: str, *, params: dict | None = None,
                      headers: dict | None = None, data: Any = None, json_body: Any = None,
                      cache_ttl: float | None = None, conditional: bool = False,
                      ok_statuses: tuple[int, ...] = (200,)) -> Response:
        if not self.health.is_available(source):
            h = self.health.get(source)
            if h.status == "disabled":
                raise SourceDisabled(f"{source} disabled")
            raise SourceBlocked(f"{source} paused until {time.strftime('%H:%M:%S', time.localtime(h.blocked_until))}")
        p = self.profile(source)
        full_url = url + ("?" + urlencode(params, doseq=True) if params else "")
        ttl = p.cache_ttl if cache_ttl is None else cache_ttl
        cache_key = f"{source}|{method}|{full_url}|{json.dumps(json_body, sort_keys=True) if json_body else ''}"
        if method == "GET" and ttl > 0:
            hit = await self.cache.get(cache_key)
            if hit is not None:
                return Response(**{**hit, "from_cache": True})
        if p.respect_robots and not await self._robots_allowed(p, full_url):
            raise RobotsDisallowed(f"robots.txt disallows {full_url}")

        hdrs = {"User-Agent": p.user_agent, "Accept-Encoding": "gzip, deflate, br", **p.headers, **(headers or {})}
        validator = self._validators.get(full_url) if conditional else None
        if validator:
            if validator[0]:
                hdrs["If-None-Match"] = validator[0]
            if validator[1]:
                hdrs["If-Modified-Since"] = validator[1]

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= p.retries:
            attempt += 1
            t0 = time.monotonic()
            try:
                async with self._limiter(p):
                    r = await self._client.request(method, url, params=params, headers=hdrs, cookies=p.cookies or None,
                                                   data=data, json=json_body, timeout=p.timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                self.health.error(source, f"{type(e).__name__}: {e}")
                await asyncio.sleep(min(30, 2 ** attempt + random.random()))
                continue
            latency = (time.monotonic() - t0) * 1000
            body = r.text
            if r.status_code == 304 and validator:
                self.health.ok(source, latency)
                return Response(**{**validator[2].__dict__, "not_modified": True})
            low = body[:4000].lower() if p.detect_challenge else ""
            challenged = p.detect_challenge and any(m in low for m in CHALLENGE_MARKERS) and r.status_code in (403, 429, 503)
            if r.status_code in (401, 403) or challenged:
                until = time.time() + p.block_cooldown
                msg = (f"HTTP {r.status_code}{' (anti-bot challenge)' if challenged else ''}: access refused. "
                       "Check the authorisation/credentials; no retries until the cooldown expires.")
                self.health.blocked(source, msg, until)
                log.warning("[%s] %s url=%s", source, msg, url)
                raise SourceBlocked(msg)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(120, 5 * 2 ** attempt)
                self.health.error(source, f"HTTP 429 (rate limit); waiting {wait:.0f}s")
                if attempt > p.retries or wait > 120:
                    self.health.blocked(source, "persistent HTTP 429", time.time() + max(wait, 60))
                    raise SourceBlocked(f"{source}: rate limit (429)")
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500:
                last_exc = SourceError(f"HTTP {r.status_code}")
                self.health.error(source, f"HTTP {r.status_code}")
                await asyncio.sleep(min(30, 2 ** attempt + random.random()))
                continue
            if r.status_code not in ok_statuses:
                self.health.error(source, f"HTTP {r.status_code}")
                raise SourceError(f"{source}: HTTP {r.status_code} on {url}")
            self.health.ok(source, latency)
            resp = Response(status=r.status_code, text=body, headers=dict(r.headers), url=str(r.url))
            if conditional:
                self._validators[full_url] = (r.headers.get("ETag"), r.headers.get("Last-Modified"), resp)
            if method == "GET" and ttl > 0:
                await self.cache.set(cache_key, {k: v for k, v in resp.__dict__.items() if k != "from_cache"}, ttl)
            return resp
        raise SourceError(f"{source}: retries exhausted ({last_exc})")

    async def get(self, source: str, url: str, **kw) -> Response:
        return await self.request(source, "GET", url, **kw)

    async def get_json(self, source: str, url: str, **kw) -> Any:
        r = await self.get(source, url, **kw)
        try:
            return r.json()
        except ValueError as e:
            self.health.error(source, "invalid JSON")
            raise SourceError(f"{source}: invalid JSON response") from e
