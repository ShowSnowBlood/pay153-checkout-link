from __future__ import annotations

import ipaddress
import os
import random
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

from curl_cffi.requests import Session as CffiSession


_SESSION_MARKERS = ("__rotate__", "{session}", "%7bsession%7d")
_SESSION_WORD_RE = re.compile(r"(?i)(-session-)rotate(?=-)")
_LIFETIME_RE = re.compile(r"(?i)(?:^|-)lifetime-(\d+)(?:-|$)")


@dataclass(frozen=True)
class ProxyProbe:
    proxy_url: str
    exit_ip: str
    country: str
    region: str
    city: str
    currency: str
    openai_ok: bool
    stripe_ok: bool
    geo_ok: bool
    latency_ms: int
    score: float
    error: str = ""
    expires_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.openai_ok and self.stripe_ok and self.geo_ok

    def geo(self) -> dict[str, str]:
        return {
            "country": self.country,
            "region": self.region,
            "city": self.city,
            "currency": self.currency,
            "ip": self.exit_ip,
        }


@dataclass
class _Health:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""


def mask_proxy_url(proxy_url: str) -> str:
    try:
        parsed = urlsplit(proxy_url)
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        username = unquote(parsed.username or "")
        if username:
            username = username[:5] + "***"
            return f"{parsed.scheme}://{username}:***@{host}{port}"
        return f"{parsed.scheme}://{host}{port}"
    except Exception:
        return "proxy://***"


def is_dynamic_template(proxy_url: str) -> bool:
    lowered = unquote(str(proxy_url or "")).lower()
    return any(marker in lowered for marker in _SESSION_MARKERS) or bool(_SESSION_WORD_RE.search(lowered))


def materialize_proxy_url(proxy_url: str) -> tuple[str, float]:
    raw = str(proxy_url or "")
    if not is_dynamic_template(raw):
        return raw, 0.0

    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12].lower()
    parsed = urlsplit(raw)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    for marker in ("__rotate__", "{session}"):
        username = username.replace(marker, token)
    username = _SESSION_WORD_RE.sub(lambda match: f"{match.group(1)}{token}", username)

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc += f":{parsed.port}"
    if username:
        from urllib.parse import quote

        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        netloc = f"{auth}@{netloc}"

    lifetime = 0
    match = _LIFETIME_RE.search(username)
    if match:
        lifetime = max(1, int(match.group(1))) * 60
    expires_at = time.time() + lifetime if lifetime else 0.0
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)), expires_at


def _valid_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return not any((address.is_private, address.is_loopback, address.is_link_local, address.is_unspecified))
    except ValueError:
        return False


def _parse_cloudflare_trace(text: str) -> dict[str, str]:
    values = {}
    for line in str(text or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    ip = values.get("ip", "")
    country = values.get("loc", "").upper()
    if not _valid_public_ip(ip) or not re.fullmatch(r"[A-Z]{2}", country):
        return {}
    return {"ip": ip, "country": country, "region": "", "city": "", "currency": ""}


class ProxyPoolOptimizer:
    def __init__(self) -> None:
        self.probe_count = max(1, min(int(os.getenv("PAY153_PROXY_PROBE_COUNT", "6")), 12))
        self.timeout = max(2.0, min(float(os.getenv("PAY153_PROXY_PROBE_TIMEOUT", "8")), 20.0))
        self.cache_ttl = max(30, min(int(os.getenv("PAY153_PROXY_PROBE_TTL", "300")), 1800))
        self._lock = threading.RLock()
        self._health: dict[str, _Health] = {}
        self._cache: dict[str, tuple[float, ProxyProbe]] = {}

    def _history_adjustment(self, proxy_url: str) -> float:
        with self._lock:
            health = self._health.get(proxy_url)
            if not health:
                return 0.0
            return min(5.0, health.successes * 0.75) - min(18.0, health.consecutive_failures * 6.0)

    def _in_cooldown(self, proxy_url: str) -> bool:
        with self._lock:
            return bool((self._health.get(proxy_url) or _Health()).cooldown_until > time.time())

    def report(self, proxy_urls: Iterable[str], *, success: bool, error: str = "") -> None:
        now = time.time()
        with self._lock:
            for proxy_url in set(proxy_urls):
                if not proxy_url:
                    continue
                health = self._health.setdefault(proxy_url, _Health())
                if success:
                    health.successes += 1
                    health.consecutive_failures = 0
                    health.cooldown_until = 0.0
                    health.last_error = ""
                else:
                    health.failures += 1
                    health.consecutive_failures += 1
                    cooldown = min(900, 30 * (2 ** min(health.consecutive_failures - 1, 5)))
                    health.cooldown_until = now + cooldown
                    health.last_error = str(error or "")[:240]

    def _probe_geo(self, http: CffiSession, deadline: float) -> tuple[dict[str, str], int, str]:
        started = time.monotonic()
        errors: list[str] = []
        remaining = max(0.5, deadline - time.monotonic())
        try:
            response = http.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=min(remaining, 4.0))
            if int(getattr(response, "status_code", 0) or 0) == 200:
                parsed = _parse_cloudflare_trace(response.text)
                if parsed:
                    return parsed, int((time.monotonic() - started) * 1000), ""
            errors.append(f"cf_{getattr(response, 'status_code', 0)}")
        except Exception as exc:
            errors.append(type(exc).__name__)

        remaining = max(0.5, deadline - time.monotonic())
        try:
            response = http.get("https://ipapi.co/json/", timeout=min(remaining, 4.0))
            payload = response.json() if int(getattr(response, "status_code", 0) or 0) == 200 else {}
            ip = str(payload.get("ip") or "")
            country = str(payload.get("country_code") or payload.get("country") or "").upper()
            if _valid_public_ip(ip) and re.fullmatch(r"[A-Z]{2}", country):
                return {
                    "ip": ip,
                    "country": country,
                    "region": str(payload.get("region") or ""),
                    "city": str(payload.get("city") or ""),
                    "currency": str(payload.get("currency") or "").upper(),
                }, int((time.monotonic() - started) * 1000), ""
            errors.append(f"ipapi_{getattr(response, 'status_code', 0)}")
        except Exception as exc:
            errors.append(type(exc).__name__)
        return {}, int((time.monotonic() - started) * 1000), "/".join(errors[-2:])

    @staticmethod
    def _probe_openai(http: CffiSession, timeout: float) -> tuple[bool, str]:
        budget = max(1.0, timeout)
        try:
            response = http.get(
                "https://auth.openai.com/api/auth/csrf",
                headers={"Accept": "application/json"},
                timeout=max(0.5, budget * 0.55),
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            payload = response.json() if status == 200 else {}
            ok = status == 200 and bool(payload.get("csrfToken"))
            if ok:
                return True, ""
        except Exception as exc:
            status = 0
            first_error = type(exc).__name__
        else:
            first_error = str(status or "no_response")

        # Checkout already has a bearer token, so auth.openai.com is not a hard
        # dependency. A structured 401 from the real ChatGPT backend proves the
        # route is reachable without mistaking a Cloudflare 403 for success.
        try:
            response = http.get(
                "https://chatgpt.com/backend-api/models",
                headers={"Accept": "application/json"},
                timeout=max(0.5, budget * 0.45),
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            text = str(getattr(response, "text", "") or "").lower()
            backend_reachable = status == 401 and any(
                marker in text for marker in ("unauthorized", "authentication", "bearer", "credentials")
            )
            if backend_reachable:
                return True, ""
            return False, f"openai_{first_error}/chatgpt_{status or 'no_response'}"
        except Exception as exc:
            return False, f"openai_{first_error}/chatgpt_{type(exc).__name__}"

    @staticmethod
    def _probe_stripe(http: CffiSession, timeout: float) -> tuple[bool, str]:
        try:
            response = http.get("https://api.stripe.com/", timeout=max(0.5, timeout), allow_redirects=False)
            status = int(getattr(response, "status_code", 0) or 0)
            ok = status in {200, 301, 302, 401, 404}
            return ok, "" if ok else f"stripe_{status or 'no_response'}"
        except Exception as exc:
            return False, f"stripe_{type(exc).__name__}"

    def probe(self, proxy_url: str, *, expected_country: str = "", expires_at: float = 0.0) -> ProxyProbe:
        now = time.time()
        with self._lock:
            cached = self._cache.get(proxy_url)
            if cached and now - cached[0] <= self.cache_ttl:
                probe = cached[1]
                if not probe.expires_at or probe.expires_at > now + 15:
                    return probe

        started = time.monotonic()
        geo_deadline = started + min(self.timeout, 5.0)
        http = CffiSession(impersonate="chrome136")
        http.trust_env = False
        http.proxies = {"http": proxy_url, "https": proxy_url}
        errors: list[str] = []
        try:
            geo, _geo_latency, geo_error = self._probe_geo(http, geo_deadline)
            if geo_error:
                errors.append(geo_error)
            openai_ok, openai_error = self._probe_openai(http, min(self.timeout, 6.0))
            if openai_error:
                errors.append(openai_error)
            stripe_ok, stripe_error = self._probe_stripe(http, min(self.timeout, 4.0))
            if stripe_error:
                errors.append(stripe_error)
        finally:
            try:
                http.close()
            except Exception:
                pass

        latency_ms = int((time.monotonic() - started) * 1000)
        country = str(geo.get("country") or "").upper()
        geo_ok = bool(geo.get("ip") and country)
        country_match = not expected_country or country == expected_country.upper()
        score = 0.0
        score += 38.0 if openai_ok else 0.0
        score += 24.0 if stripe_ok else 0.0
        score += 16.0 if geo_ok else 0.0
        score += 16.0 if expected_country and country_match else (5.0 if not expected_country else 0.0)
        score += max(0.0, 6.0 * (1.0 - min(latency_ms, 12000) / 12000))
        score += self._history_adjustment(proxy_url)
        if expires_at and expires_at <= time.time() + 30:
            score -= 40.0
            errors.append("session_expiring")
        probe = ProxyProbe(
            proxy_url=proxy_url,
            exit_ip=str(geo.get("ip") or ""),
            country=country,
            region=str(geo.get("region") or ""),
            city=str(geo.get("city") or ""),
            currency=str(geo.get("currency") or "").upper(),
            openai_ok=openai_ok,
            stripe_ok=stripe_ok,
            geo_ok=geo_ok,
            latency_ms=latency_ms,
            score=round(max(0.0, min(score, 100.0)), 2),
            error="/".join(errors)[:240],
            expires_at=expires_at,
        )
        with self._lock:
            self._cache[proxy_url] = (time.time(), probe)
        return probe

    def _materialize_candidates(self, pool: list[str], count: int) -> list[tuple[str, float]]:
        candidates: list[tuple[str, float]] = []
        seen: set[str] = set()
        templates = list(dict.fromkeys(pool))
        if not templates:
            return []
        random.SystemRandom().shuffle(templates)
        cursor = 0
        attempts = 0
        while len(candidates) < count and attempts < count * 8:
            template = templates[cursor % len(templates)]
            cursor += 1
            attempts += 1
            concrete, expires_at = materialize_proxy_url(template)
            if concrete in seen or self._in_cooldown(concrete):
                continue
            seen.add(concrete)
            candidates.append((concrete, expires_at))
            if not any(is_dynamic_template(item) for item in templates) and len(seen) >= len(templates):
                break
        if not candidates:
            for template in templates:
                concrete, expires_at = materialize_proxy_url(template)
                if concrete not in seen:
                    candidates.append((concrete, expires_at))
        return candidates

    def select(
        self,
        pool: list[str],
        *,
        role: str,
        provider: str,
        expected_country: str = "",
        log: Callable[[str], None] = lambda _message: None,
    ) -> ProxyProbe:
        candidate_count = min(self.probe_count, max(1, len(pool)))
        if any(is_dynamic_template(item) for item in pool):
            candidate_count = self.probe_count
        candidates = self._materialize_candidates(pool, candidate_count)
        if not candidates:
            raise RuntimeError(f"{role}代理池没有可用候选")

        probes: list[ProxyProbe] = []
        with ThreadPoolExecutor(max_workers=min(6, len(candidates)), thread_name_prefix="proxy-probe") as executor:
            future_map = {
                executor.submit(self.probe, proxy, expected_country=expected_country, expires_at=expires_at): proxy
                for proxy, expires_at in candidates
            }
            for future in as_completed(future_map):
                try:
                    probes.append(future.result())
                except Exception as exc:
                    log(f"IP 池探测异常：{type(exc).__name__}")

        if not probes:
            raise RuntimeError(f"{role}代理池深度探测全部失败")
        unique: dict[str, ProxyProbe] = {}
        for probe in sorted(probes, key=lambda item: (item.ok, item.score), reverse=True):
            key = probe.exit_ip or probe.proxy_url
            unique.setdefault(key, probe)
        ranked = list(unique.values())
        country_matches = [item for item in ranked if not expected_country or item.country == expected_country]
        payment_path = role != "入口" or provider in {"hosted", "pix"}
        usable = [
            item for item in country_matches
            if item.geo_ok and item.openai_ok and (item.stripe_ok or not payment_path)
        ]
        for item in ranked:
            network_ok = item.geo_ok and item.openai_ok and (item.stripe_ok or not payment_path)
            if not network_ok:
                self.report([item.proxy_url], success=False, error=item.error or "deep_probe_failed")
        if not usable:
            countries = "/".join(dict.fromkeys(item.country or "?" for item in ranked[:8]))
            errors = "; ".join(dict.fromkeys(item.error or "probe_failed" for item in ranked[:4]))
            requirement = f"，要求出口 {expected_country}" if expected_country else ""
            raise RuntimeError(
                f"{role}代理池没有同时通过深度验证的候选{requirement}；"
                f"已测地区={countries or '?'}；{errors[:320]}"
            )
        selected = usable[0]
        log(
            f"IP 池优选[{role}/{provider}]：{mask_proxy_url(selected.proxy_url)}，"
            f"出口={selected.exit_ip or '?'}/{selected.country or '?'}，"
            f"OpenAI={'OK' if selected.openai_ok else 'FAIL'}，Stripe={'OK' if selected.stripe_ok else 'FAIL'}，"
            f"延迟={selected.latency_ms}ms，评分={selected.score}"
        )
        return selected


OPTIMIZER = ProxyPoolOptimizer()
