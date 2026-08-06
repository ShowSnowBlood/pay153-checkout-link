from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


CHECKOUT_COUNTRY = "KR"
PROMOTION_COUNTRY = "VN"
PROVIDER_COUNTRY = "KR"
PROXY_ROUTE_REFERENCE = "reference"
PROXY_ROUTE_COUNTRIES = {
    PROXY_ROUTE_REFERENCE: (
        CHECKOUT_COUNTRY,
        PROMOTION_COUNTRY,
        PROVIDER_COUNTRY,
    ),
}
MAX_REQUEST_WINDOW = timedelta(minutes=15)
LINK_VALIDITY = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(seconds=5)
HTTP_TIMEOUT = 30.0
PREFLIGHT_TIMEOUT = 12.0
REDIRECT_POLL_TIMEOUT = 120.0
MAX_APPROVE_ATTEMPTS = 3
MAX_REDIRECT_HOPS = 6
TRIAL_COUPON = "plus-1-month-free"
STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
STRIPE_RUNTIME = "c00af4ce81"
STRIPE_PAYMENT_UA = (
    f"stripe.js/{STRIPE_RUNTIME}; stripe-js-v3/{STRIPE_RUNTIME}; checkout"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
IP_CHECK_SOURCES = (
    ("ipinfo", "https://ipinfo.io/json"),
    ("ipapi", "https://ipapi.co/json/"),
    ("ipwho", "https://ipwho.is/"),
    ("myip", "https://api.myip.com/"),
)
PROVIDER_ALLOWED_HOSTS = frozenset(
    {
        # Kakao/KakaoPay and NicePay hosts used by the hosted payment pages.
        "kakao.com",
        "www.kakao.com",
        "pay.kakao.com",
        "kakaopay.com",
        "www.kakaopay.com",
        "pay.kakaopay.com",
        "online-payment.kakaopay.com",
        "pg.kakaopay.com",
        "kakaopay.co.kr",
        "www.kakaopay.co.kr",
        "pay.kakaopay.co.kr",
        "online-payment.kakaopay.co.kr",
        "pg.kakaopay.co.kr",
        "nicepay.com",
        "www.nicepay.com",
        "pay.nicepay.com",
        "nicepay.co.kr",
        "www.nicepay.co.kr",
        "pay.nicepay.co.kr",
        "npg.nicepay.co.kr",
    }
)
INTERMEDIATE_ALLOWED_HOSTS = PROVIDER_ALLOWED_HOSTS | frozenset(
    {
        "api.stripe.com",
        "checkout.stripe.com",
        "js.stripe.com",
        "pm-redirects.stripe.com",
        "pay.openai.com",
        "chatgpt.com",
        "www.chatgpt.com",
    }
)
PROXY_POLICY_PAYMENT_HOSTS = frozenset(
    (
        "api.stripe.com",
        "checkout.stripe.com",
        "js.stripe.com",
        "pm-redirects.stripe.com",
    )
)
STRIPE_CONNECT_HOSTS = (
    "api.stripe.com",
    "pm-redirects.stripe.com",
)
STRIPE_DOH_URL = "https://dns.google/resolve"
KOREAN_FAMILY_NAMES = (
    "김",
    "이",
    "박",
    "최",
    "정",
    "강",
    "조",
    "윤",
    "장",
    "임",
    "한",
    "오",
    "서",
    "신",
    "권",
    "황",
)
KOREAN_GIVEN_NAMES = (
    "민준",
    "서준",
    "도윤",
    "예준",
    "시우",
    "주원",
    "하준",
    "지호",
    "지후",
    "준서",
    "서연",
    "서윤",
    "지우",
    "서현",
    "하은",
    "하윤",
    "민서",
    "지유",
    "윤서",
    "채원",
)
SEOUL_ADDRESS_SEEDS = (
    {
        "district": "강남구",
        "road": "테헤란로",
        "postal": "06164",
        "base": 87,
        "span": 40,
    },
    {
        "district": "강남구",
        "road": "봉은사로",
        "postal": "06097",
        "base": 524,
        "span": 32,
    },
    {
        "district": "서초구",
        "road": "서초대로",
        "postal": "06611",
        "base": 396,
        "span": 36,
    },
    {
        "district": "송파구",
        "road": "올림픽로",
        "postal": "05510",
        "base": 300,
        "span": 36,
    },
    {
        "district": "마포구",
        "road": "월드컵북로",
        "postal": "03925",
        "base": 396,
        "span": 36,
    },
)
EMAIL_DOMAINS = ("gmail.com", "naver.com", "daum.net", "kakao.com")
COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])"
    r"(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)
SESSION_SELECTOR_RE = re.compile(
    r"(?i)(?P<prefix>(?:^|-)session-)(?P<value>[a-z0-9_]+)(?=-|$)"
)
DATAIMPULSE_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<prefix>__cr[._-])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)
DATAIMPULSE_CONFLICTING_GEO_SELECTOR_RE = re.compile(
    r"(?i);(?:city|state|zip|asn|nocity|nostate|nozip|noasn|nocr)"
    r"\.[^\s;:@/?#]*"
)
PROCESSOR_ENTITY_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

_CURL_REQUESTS: Any = None
_KAKAO_CURL_CLASS: Any = None
_DEADLINE_TS = 0.0


class WorkerFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        transient: bool = False,
        *,
        stage: str = "",
        http_status: int = 0,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient
        self.stage = stage
        self.http_status = http_status


def _is_proxy_payment_target_policy_block(error: Exception, url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").strip().lower()
    if hostname not in PROXY_POLICY_PAYMENT_HOSTS:
        return False
    message = str(error or "")
    if "connect" not in message.lower() or not re.search(r"\b403\b", message):
        return False
    error_code = getattr(error, "code", 0)
    try:
        error_code = int(error_code)
    except (TypeError, ValueError):
        error_code = 0
    return error.__class__.__name__ == "ProxyError" or error_code in (56, 97)


def _parse_timestamp(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise WorkerFailure("kakao_deadline_invalid", f"{field} is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WorkerFailure("kakao_deadline_invalid", f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise WorkerFailure(
            "kakao_deadline_invalid", f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _remaining_seconds(maximum: float) -> float:
    remaining = _DEADLINE_TS - time.time()
    if remaining <= 0:
        raise WorkerFailure(
            "kakao_deadline_exceeded",
            "Kakao extraction deadline was exceeded",
            True,
        )
    return max(0.05, min(float(maximum), remaining))


def _sleep(seconds: float) -> None:
    duration = max(0.0, float(seconds))
    remaining = _DEADLINE_TS - time.time()
    if remaining <= 0:
        _remaining_seconds(0.05)
    if duration >= remaining:
        time.sleep(max(0.0, remaining))
        _remaining_seconds(0.05)
    time.sleep(duration)


def _load_curl_requests() -> Any:
    global _CURL_REQUESTS
    if _CURL_REQUESTS is not None:
        return _CURL_REQUESTS
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise WorkerFailure(
            "kakao_dependency_missing",
            "curl_cffi 0.13.0 is required",
        ) from exc
    _CURL_REQUESTS = curl_requests
    return _CURL_REQUESTS


def _load_kakao_curl_class() -> Any:
    """Return a Curl subclass that correctly handles CONNECT_TO string lists."""
    global _KAKAO_CURL_CLASS
    if _KAKAO_CURL_CLASS is not None:
        return _KAKAO_CURL_CLASS
    try:
        from curl_cffi import CurlOpt
        from curl_cffi.curl import Curl, ffi, lib
    except ImportError as exc:
        raise WorkerFailure(
            "kakao_dependency_invalid",
            "curl_cffi does not expose required connection controls",
        ) from exc

    class KakaoCurl(Curl):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._kakao_connect_to = ffi.NULL

        def setopt(self, option: Any, value: Any) -> int:
            if option != CurlOpt.CONNECT_TO:
                return super().setopt(option, value)
            if self._curl is None:
                return 0
            values = [value] if isinstance(value, (str, bytes)) else list(value)
            if self._kakao_connect_to != ffi.NULL:
                lib.curl_slist_free_all(self._kakao_connect_to)
                self._kakao_connect_to = ffi.NULL
            for item in values:
                encoded = item.encode() if isinstance(item, str) else item
                self._kakao_connect_to = lib.curl_slist_append(
                    self._kakao_connect_to,
                    encoded,
                )
            ret = lib._curl_easy_setopt(
                self._curl,
                option,
                self._kakao_connect_to,
            )
            self._check_error(ret, "setopt", option, value)
            return ret

        def _clear_connect_to(self) -> None:
            if self._kakao_connect_to != ffi.NULL:
                lib.curl_slist_free_all(self._kakao_connect_to)
                self._kakao_connect_to = ffi.NULL

        def clean_handles_and_buffers(
            self,
            clear_headers: bool = True,
            clear_resolve: bool = True,
        ) -> None:
            try:
                super().clean_handles_and_buffers(clear_headers, clear_resolve)
            finally:
                self._clear_connect_to()

        def reset(self) -> None:
            super().reset()
            self._clear_connect_to()

    _KAKAO_CURL_CLASS = KakaoCurl
    return _KAKAO_CURL_CLASS


def normalize_access_token(raw: Any) -> str:
    if isinstance(raw, (dict, list)):
        return _find_token(raw) or ""
    text = str(raw or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return _find_token(json.loads(text)) or ""
        except json.JSONDecodeError:
            return ""
    return text


def _find_token(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        return text or None
    if isinstance(value, list):
        for item in value:
            found = _find_token(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token", "bearerToken"):
            if key in value:
                found = _find_token(value.get(key))
                if found:
                    return found
        for item in value.values():
            found = _find_token(item)
            if found:
                return found
    return None


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jwt_account_id(token: str) -> str:
    auth = _jwt_payload(token).get("https://api.openai.com/auth") or {}
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or "").strip()
    return ""


def normalize_proxy_url(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        if text.count(":") == 3 and "@" not in text:
            host, port, username, password = text.split(":", 3)
            text = f"http://{username}:{password}@{host}:{port}"
        else:
            text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname:
            return ""
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if port is not None:
            host = f"{host}:{port}"
        username = quote(unquote(parsed.username or ""), safe="-._~")
        auth = username
        if parsed.password is not None:
            auth = f"{auth}:{quote(unquote(parsed.password), safe='-._~')}"
        netloc = f"{auth}@{host}" if auth else host
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        return ""


def _replace_country_selector(value: str, country: str) -> tuple[str, int]:
    replacements = 0

    def replace_named(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        current = match.group("value")
        target = country.upper() if current.isupper() else country.lower()
        return f"{match.group('name')}{match.group('separator')}{target}"

    def replace_dataimpulse(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        current = match.group("value")
        target = country.upper() if current.isupper() else country.lower()
        return f"{match.group('prefix')}{target}"

    value = COUNTRY_SELECTOR_RE.sub(replace_named, value)
    value = DATAIMPULSE_COUNTRY_SELECTOR_RE.sub(replace_dataimpulse, value)
    return value, replacements


def _strip_conflicting_geo_selectors(value: str) -> str:
    return DATAIMPULSE_CONFLICTING_GEO_SELECTOR_RE.sub("", value)


def _regionalize_sticky_session(value: str, country: str) -> str:
    """Derive one stable sticky session per country from the supplied seed."""
    suffix = str(country or "").strip().lower()
    if len(suffix) != 2:
        return value

    def replace(match: re.Match[str]) -> str:
        seed = match.group("value")
        return f"{match.group('prefix')}{seed}{suffix}"

    return SESSION_SELECTOR_RE.sub(replace, value, count=1)


def _proxy_url_with_credentials(
    parsed: Any,
    username: str,
    password: str | None,
) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    auth = quote(username, safe="-._~")
    if password is not None:
        auth = f"{auth}:{quote(password, safe='-._~')}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{auth}@{host}" if auth else host,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _proxy_for_country(proxy_url: str, country: str) -> str:
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        raise WorkerFailure("kakao_proxy_invalid", "Proxy URL is invalid")
    parsed = urlsplit(normalized)
    username, username_count = _replace_country_selector(
        unquote(parsed.username or ""),
        country,
    )
    username = _strip_conflicting_geo_selectors(username)
    if username_count == 0:
        raise WorkerFailure(
            "kakao_proxy_country_selector_required",
            "Proxy URL must include a country or region selector",
        )
    username = _regionalize_sticky_session(username, country)
    password = (
        unquote(parsed.password)
        if parsed.password is not None
        else None
    )
    return _proxy_url_with_credentials(parsed, username, password)


def _proxy_chain_key(proxy_url: str) -> str:
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    username = _strip_conflicting_geo_selectors(
        unquote(parsed.username or "")
    )
    username = COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}*",
        username,
    )
    username = DATAIMPULSE_COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('prefix')}*",
        username,
    )
    username = SESSION_SELECTOR_RE.sub(
        lambda match: f"{match.group('prefix')}*",
        username,
        count=1,
    )
    password = (
        unquote(parsed.password)
        if parsed.password is not None
        else None
    )
    canonical = _proxy_url_with_credentials(parsed, username, password)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_proxy_route(raw: Any) -> str:
    route = str(raw or PROXY_ROUTE_REFERENCE).strip().lower()
    if route not in PROXY_ROUTE_COUNTRIES:
        raise WorkerFailure(
            "kakao_proxy_route_unsupported",
            "Kakao proxy route is unsupported",
        )
    return route


def kakao_proxy_chain(
    proxy_url: str,
    route: str = PROXY_ROUTE_REFERENCE,
) -> tuple[str, str, str]:
    countries = PROXY_ROUTE_COUNTRIES[normalize_proxy_route(route)]
    checkout_proxy = _proxy_for_country(proxy_url, countries[0])
    promotion_proxy = _proxy_for_country(proxy_url, countries[1])
    provider_proxy = _proxy_for_country(proxy_url, countries[2])
    seed_key = _proxy_chain_key(proxy_url)
    if not seed_key or any(
        _proxy_chain_key(proxy) != seed_key
        for proxy in (checkout_proxy, promotion_proxy, provider_proxy)
    ):
        raise WorkerFailure(
            "kakao_proxy_sticky_mismatch",
            "Derived proxy chain does not preserve the sticky session",
        )
    return checkout_proxy, promotion_proxy, provider_proxy


@contextmanager
def _stripe_pinned_tls(session: Any, hostname: str, target_ip: str):
    try:
        from curl_cffi import CurlOpt
    except ImportError as exc:
        raise WorkerFailure(
            "kakao_dependency_invalid",
            "curl_cffi does not expose required TLS controls",
        ) from exc
    try:
        address = ipaddress.ip_address(target_ip)
    except ValueError as exc:
        raise WorkerFailure(
            "kakao_stripe_target_invalid",
            "Stripe target is invalid",
            True,
            stage="stripe_target_resolve",
        ) from exc
    if address.version != 4 or not address.is_global or not re.fullmatch(r"[a-z0-9.-]+", hostname):
        raise WorkerFailure(
            "kakao_stripe_target_invalid",
            "Stripe target is invalid",
            True,
            stage="stripe_target_resolve",
        )
    previous_options = dict(getattr(session, "curl_options", {}) or {})
    session.curl_options = {
        **previous_options,
        CurlOpt.SSL_VERIFYPEER: 1,
        # CONNECT_TO changes the proxy CONNECT target while the request URL
        # keeps the Stripe hostname for SNI and certificate verification.
        CurlOpt.SSL_VERIFYHOST: 2,
        CurlOpt.CONNECT_TO: [f"{hostname}:443:{address}:443"],
    }
    try:
        yield
    finally:
        session.curl_options = previous_options


class KakaoHttpClient:
    def __init__(
        self,
        proxy_url: str,
        stripe_targets: dict[str, str] | None = None,
    ):
        self.proxy_url = str(proxy_url or "").strip()
        self.stripe_targets = dict(stripe_targets or {})
        if not self.proxy_url:
            raise WorkerFailure("kakao_proxy_required", "Proxy URL is required")
        try:
            self.session = _load_curl_requests().Session(
                curl=_load_kakao_curl_class()(),
                use_thread_local_curl=False,
                impersonate="chrome136",
            )
        except WorkerFailure:
            raise
        except Exception as exc:
            raise WorkerFailure(
                "kakao_dependency_invalid",
                "curl_cffi has no usable Chrome profile",
            ) from exc
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            }
        )
        self.session.proxies.clear()
        self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def request(
        self,
        method: str,
        url: str,
        *,
        stage: str = "",
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float = HTTP_TIMEOUT,
        # API calls must not silently follow an upstream Location header.  A
        # redirect carrying Authorization or payment data is only followed by
        # the explicit, host-checked provider resolver below.
        allow_redirects: bool = False,
    ) -> tuple[int, str, dict[str, str]]:
        hostname = (urlsplit(url).hostname or "").strip().lower()
        if allow_redirects:
            raise WorkerFailure(
                "kakao_redirects_disabled",
                "Automatic redirects are disabled for Kakao API requests",
                False,
                stage=stage,
            )
        target_ip = self.stripe_targets.get(hostname, "")
        request_url = url
        request_headers = dict(headers or {})
        request_redirects = False
        if target_ip:
            request_redirects = False
        try:
            with _stripe_pinned_tls(self.session, hostname, target_ip) if target_ip else _null_context():
                response = self.session.request(
                    method.upper(),
                    request_url,
                    headers=request_headers,
                    json=json_body,
                    data=form,
                    params=params,
                    timeout=_remaining_seconds(timeout),
                    allow_redirects=request_redirects,
                    impersonate="chrome136",
                    proxies={"http": self.proxy_url, "https": self.proxy_url},
                    verify=True if target_ip else None,
                )
            return (
                int(response.status_code),
                str(response.text or ""),
                {
                    str(key).lower(): str(value)
                    for key, value in response.headers.items()
                },
            )
        except WorkerFailure as exc:
            if stage and not exc.stage:
                raise WorkerFailure(
                    exc.code,
                    exc.message,
                    exc.transient,
                    stage=stage,
                    http_status=exc.http_status,
                ) from exc
            raise
        except Exception as exc:
            if _is_proxy_payment_target_policy_block(exc, url):
                raise WorkerFailure(
                    "kakao_proxy_target_policy_blocked",
                    "Proxy gateway blocks the payment target",
                    True,
                    stage=stage,
                    http_status=403,
                ) from exc
            raise WorkerFailure(
                "kakao_proxy_request_failed",
                "Kakao proxy request failed",
                True,
                stage=stage,
            ) from exc


@contextmanager
def _null_context():
    yield


def _resolve_stripe_connect_targets(proxy_url: str) -> dict[str, str]:
    client = KakaoHttpClient(proxy_url)
    stage = "stripe_target_resolve"
    try:
        targets: dict[str, str] = {}
        for hostname in STRIPE_CONNECT_HOSTS:
            payload = _request_required_json(
                client,
                "GET",
                STRIPE_DOH_URL,
                stage,
                headers={"Accept": "application/dns-json"},
                params={"name": hostname, "type": "A"},
                timeout=PREFLIGHT_TIMEOUT,
            )
            addresses: list[str] = []
            for answer in payload.get("Answer") or []:
                if not isinstance(answer, dict) or int(answer.get("type") or 0) != 1:
                    continue
                try:
                    address = ipaddress.ip_address(str(answer.get("data") or ""))
                except ValueError:
                    continue
                if address.version == 4 and address.is_global:
                    addresses.append(str(address))
            if not addresses:
                raise WorkerFailure(
                    "kakao_stripe_target_resolve_failed",
                    "Stripe target could not be resolved through the proxy",
                    True,
                    stage=stage,
                )
            targets[hostname] = sorted(set(addresses))[0]
        return targets
    finally:
        client.close()


def _json_object(text: str, stage: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkerFailure(
            "kakao_invalid_response",
            "Kakao upstream returned an invalid response",
            True,
            stage=stage,
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerFailure(
            "kakao_invalid_response",
            "Kakao upstream returned an invalid response",
            True,
            stage=stage,
        )
    return payload


def _http_failure(status: int, stage: str) -> WorkerFailure:
    if status == 401:
        return WorkerFailure(
            "kakao_access_token_rejected",
            "Access token was rejected",
            False,
            stage=stage,
            http_status=status,
        )
    if status == 407:
        return WorkerFailure(
            "kakao_proxy_rejected",
            "Proxy authentication was rejected",
            True,
            stage=stage,
            http_status=status,
        )
    if status == 429:
        return WorkerFailure(
            "kakao_rate_limited",
            "Kakao extraction was rate limited",
            True,
            stage=stage,
            http_status=status,
        )
    if status >= 500:
        return WorkerFailure(
            "kakao_upstream_unavailable",
            "Kakao upstream is unavailable",
            True,
            stage=stage,
            http_status=status,
        )
    return WorkerFailure(
        "kakao_upstream_rejected",
        "Kakao upstream rejected the request",
        status in (408, 409, 425),
        stage=stage,
        http_status=status,
    )


def _request_success(
    client: KakaoHttpClient,
    method: str,
    url: str,
    stage: str,
    *,
    accepted_statuses: tuple[int, ...] | None = None,
    maximum_status: int = 300,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> str:
    status, text, _ = client.request(
        method,
        url,
        stage=stage,
        headers=headers,
        json_body=json_body,
        form=form,
        params=params,
        timeout=timeout,
    )
    if accepted_statuses is not None:
        success = status in accepted_statuses
    else:
        success = 200 <= status < min(maximum_status, 300)
    if not success:
        raise _http_failure(status, stage)
    return text


def _request_required_json(
    client: KakaoHttpClient,
    method: str,
    url: str,
    stage: str,
    **kwargs: Any,
) -> dict[str, Any]:
    text = _request_success(
        client,
        method,
        url,
        stage,
        accepted_statuses=(200,),
        **kwargs,
    )
    return _json_object(text, stage)


def _request_optional_json(
    client: KakaoHttpClient,
    method: str,
    url: str,
    stage: str,
    **kwargs: Any,
) -> dict[str, Any]:
    text = _request_success(
        client,
        method,
        url,
        stage,
        maximum_status=300,
        **kwargs,
    )
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _request_status(
    client: KakaoHttpClient,
    method: str,
    url: str,
    stage: str,
    *,
    accepted_statuses: tuple[int, ...] | None = None,
    maximum_status: int = 300,
    **kwargs: Any,
) -> None:
    _request_success(
        client,
        method,
        url,
        stage,
        accepted_statuses=accepted_statuses,
        maximum_status=maximum_status,
        **kwargs,
    )


def _ip_identity(source: str, payload: dict[str, Any]) -> tuple[str, str]:
    if source == "ipinfo":
        return (
            str(payload.get("ip") or ""),
            str(payload.get("country") or "").upper(),
        )
    if source == "ipapi":
        return (
            str(payload.get("ip") or ""),
            str(payload.get("country_code") or payload.get("country") or "").upper(),
        )
    if source == "ipwho":
        if payload.get("success") is False:
            return str(payload.get("ip") or ""), ""
        return (
            str(payload.get("ip") or ""),
            str(payload.get("country_code") or "").upper(),
        )
    if source == "myip":
        return (
            str(payload.get("ip") or ""),
            str(payload.get("cc") or payload.get("country") or "").upper(),
        )
    return "", ""


def _verify_proxy_country(
    proxy_url: str,
    expected_country: str,
    stage: str,
) -> dict[str, str]:
    client = KakaoHttpClient(proxy_url)
    try:
        for source, url in IP_CHECK_SOURCES:
            try:
                status, text, _ = client.request(
                    "GET",
                    url,
                    stage=stage,
                    headers={"Accept": "application/json"},
                    timeout=PREFLIGHT_TIMEOUT,
                )
                if status >= 400:
                    continue
                payload = _json_object(text, stage)
                ip, country = _ip_identity(source, payload)
                if not ip or not country:
                    continue
                if country != expected_country:
                    raise WorkerFailure(
                        "kakao_proxy_country_mismatch",
                        "Proxy exit country does not match the required country",
                        True,
                        stage=stage,
                    )
                return {"ip": ip, "country": country}
            except WorkerFailure as exc:
                if exc.code == "kakao_proxy_country_mismatch":
                    raise
                if exc.code == "kakao_deadline_exceeded":
                    raise
        raise WorkerFailure(
            "kakao_proxy_country_unavailable",
            "Proxy exit country could not be verified",
            True,
            stage=stage,
        )
    finally:
        client.close()


def _stripe_headers(publishable_key: str, referer: str) -> dict[str, str]:
    origin = (
        "https://checkout.stripe.com"
        if "checkout.stripe.com" in referer
        else "https://pay.openai.com"
    )
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Origin": origin,
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Sec-Fetch-Site": (
            "same-site" if origin == "https://checkout.stripe.com" else "cross-site"
        ),
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }


def _trial_headers(access_token: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": USER_AGENT,
        "OAI-Device-Id": str(uuid.uuid4()),
        "OAI-Language": "ko-KR",
    }
    account_id = _jwt_account_id(access_token)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def _check_kakao_trial_eligibility(
    access_token: str,
    proxy_url: str,
) -> dict[str, Any]:
    promotion_proxy = _proxy_for_country(proxy_url, PROMOTION_COUNTRY)
    client = KakaoHttpClient(promotion_proxy)
    stage = "trial_eligibility"
    try:
        payload = _request_required_json(
            client,
            "GET",
            (
                "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
                f"?coupon={quote(TRIAL_COUPON, safe='')}"
                "&is_coupon_from_query_param=true"
            ),
            stage,
            headers=_trial_headers(access_token),
        )
        state = str(payload.get("state") or "").strip().lower()
        redemption = (
            payload.get("redemption")
            if isinstance(payload.get("redemption"), dict)
            else {}
        )
        if state == "eligible":
            return {"eligible": True, "coupon": TRIAL_COUPON}
        if state in ("not_eligible", "ineligible") or redemption.get(
            "redeemed_by_user"
        ) or redemption.get("redeemed"):
            raise WorkerFailure(
                "kakao_trial_unavailable",
                "Free trial eligibility is not available",
                False,
                stage=stage,
                http_status=200,
            )
        raise WorkerFailure(
            "kakao_trial_eligibility_unknown",
            "Free trial eligibility could not be confirmed",
            True,
            stage=stage,
            http_status=200,
        )
    finally:
        client.close()


def _elements_params(stripe_js_id: str, session_id: str = "") -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "ko",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def _processor_entity(checkout: dict[str, Any], stage: str) -> str:
    value = str(checkout.get("processor_entity") or "openai_llc")
    if not PROCESSOR_ENTITY_RE.fullmatch(value):
        raise WorkerFailure(
            "kakao_invalid_response",
            "Checkout response contains an invalid processor",
            True,
            stage=stage,
        )
    return value


def _checkout_page_url(
    checkout_id: str,
    checkout: dict[str, Any],
    stage: str,
) -> str:
    return f"https://chatgpt.com/checkout/{_processor_entity(checkout, stage)}/{checkout_id}"


def _checkout_headers(
    access_token: str,
    referer: str,
    target_path: str,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "oai-language": "ko-KR",
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }


def _create_checkout(
    client: KakaoHttpClient,
    access_token: str,
) -> tuple[str, str, dict[str, Any]]:
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": CHECKOUT_COUNTRY, "currency": "KRW"},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": TRIAL_COUPON,
            "is_coupon_from_query_param": False,
        },
    }
    checkout = _request_required_json(
        client,
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout",
        "kr_checkout_create",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "oai-language": "ko-KR",
            "User-Agent": USER_AGENT,
        },
        json_body=payload,
    )
    checkout_id = str(checkout.get("checkout_session_id") or "")
    publishable_key = str(checkout.get("publishable_key") or "")
    if not checkout_id.startswith("cs_") or not publishable_key.startswith("pk_"):
        raise WorkerFailure(
            "kakao_invalid_response",
            "Checkout response is missing required fields",
            True,
            stage="kr_checkout_create",
        )
    _processor_entity(checkout, "kr_checkout_create")
    return checkout_id, publishable_key, checkout


def _activate_stripe_checkout(
    client: KakaoHttpClient,
    checkout_id: str,
) -> str:
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": "https://chatgpt.com/",
    }
    stage = "kr_checkout_activate_openai"
    status, _, response_headers = client.request(
        "GET",
        f"https://pay.openai.com/c/pay/{checkout_id}",
        stage=stage,
        headers=headers,
    )
    if status >= 500:
        raise _http_failure(status, stage)
    if 300 <= status < 400 and response_headers.get("location"):
        _validate_redirect_url(response_headers["location"], final=False)
    return checkout_page


def _stripe_init(
    client: KakaoHttpClient,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    stage: str,
) -> tuple[dict[str, Any], str]:
    stripe_js_id = str(uuid.uuid4())
    payload = _request_required_json(
        client,
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        stage,
        headers=_stripe_headers(publishable_key, checkout_page),
        form={
            "key": publishable_key,
            "eid": "NA",
            "browser_locale": "ko-KR",
            "browser_timezone": "Asia/Seoul",
            "redirect_type": "url",
            "_stripe_version": STRIPE_VERSION,
            **_elements_params(stripe_js_id),
        },
    )
    return payload, stripe_js_id


def _expected_amount(payload: dict[str, Any]) -> int | None:
    options = (
        payload.get("elements_options")
        if isinstance(payload.get("elements_options"), dict)
        else {}
    )
    if options.get("amount") is not None:
        return int(options["amount"])
    summary = (
        payload.get("total_summary")
        if isinstance(payload.get("total_summary"), dict)
        else {}
    )
    if summary.get("due") is not None:
        return int(summary["due"])
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    for key in ("amount_due", "total"):
        if invoice.get(key) is not None:
            return int(invoice[key])
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        amounts = [
            item.get("amount")
            for item in line_items
            if isinstance(item, dict) and item.get("amount") is not None
        ]
        if amounts:
            return sum(int(value) for value in amounts)
    return None


def _inspect_kakao_init(
    payload: dict[str, Any],
    stage: str,
    *,
    require_zero: bool,
) -> int | None:
    try:
        amount = _expected_amount(payload)
    except (TypeError, ValueError) as exc:
        raise WorkerFailure(
            "kakao_invalid_response",
            "Stripe response contains an invalid amount",
            True,
            stage=stage,
        ) from exc
    currency = str(payload.get("currency") or "").lower()
    methods = {
        str(item).lower()
        for item in payload.get("payment_method_types") or []
        if isinstance(item, str)
    }
    if "kakao_pay" not in methods:
        raise WorkerFailure(
            "kakao_checkout_state_invalid",
            "Checkout does not expose the required payment method",
            True,
            stage=stage,
        )
    if require_zero and (amount != 0 or currency != "krw"):
        raise WorkerFailure(
            "kakao_checkout_state_invalid",
            "Checkout does not expose the expected trial price",
            True,
            stage=stage,
        )
    return amount


def _update_checkout_promotion(
    client: KakaoHttpClient,
    access_token: str,
    checkout_id: str,
    checkout: dict[str, Any],
) -> None:
    target_path = "/backend-api/payments/checkout/update"
    payload = _request_optional_json(
        client,
        "POST",
        f"https://chatgpt.com{target_path}",
        "vn_promotion_update",
        headers=_checkout_headers(
            access_token,
            _checkout_page_url(checkout_id, checkout, "vn_promotion_update"),
            target_path,
        ),
        json_body={
            "checkout_session_id": checkout_id,
            "processor_entity": _processor_entity(checkout, "vn_promotion_update"),
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
            "promo_campaign_id": TRIAL_COUPON,
                "is_coupon_from_query_param": False,
            },
        },
    )
    if payload.get("success") is False:
        raise WorkerFailure(
            "kakao_promotion_rejected",
            "Kakao promotion update was rejected",
            False,
            stage="vn_promotion_update",
        )


def _random_billing() -> dict[str, str]:
    rng = random.SystemRandom()
    address = rng.choice(SEOUL_ADDRESS_SEEDS)
    name = f"{rng.choice(KOREAN_FAMILY_NAMES)}{rng.choice(KOREAN_GIVEN_NAMES)}"
    local_name = hashlib.sha256(f"{name}:{uuid.uuid4()}".encode("utf-8")).hexdigest()[
        :10
    ]
    return {
        "name": name,
        "email": f"{local_name}@{rng.choice(EMAIL_DOMAINS)}",
        "line1": f"{address['road']} {address['base'] + rng.randrange(address['span'])}",
        "line2": "",
        "city": "서울특별시",
        "state": str(address["district"]),
        "postal_code": str(address["postal"]),
        "country": PROVIDER_COUNTRY,
    }


def _update_checkout_taxes(
    client: KakaoHttpClient,
    access_token: str,
    checkout_id: str,
    checkout: dict[str, Any],
    billing: dict[str, str],
) -> None:
    target_path = "/backend-api/payments/checkout/taxes"
    _request_status(
        client,
        "POST",
        f"https://chatgpt.com{target_path}",
        "kr_checkout_taxes",
        headers=_checkout_headers(
            access_token,
            _checkout_page_url(checkout_id, checkout, "kr_checkout_taxes"),
            target_path,
        ),
        json_body={
            "checkout_session_id": checkout_id,
            "checkout_email": billing["email"],
            "billing_country": PROVIDER_COUNTRY,
            "billing_name": billing["name"],
            "currency": "KRW",
            "tax_id": None,
            "processor_entity": _processor_entity(checkout, "kr_checkout_taxes"),
            "billing_address": {
                "line1": billing["line1"],
                "city": billing["city"],
                "country": PROVIDER_COUNTRY,
                "postal_code": billing["postal_code"],
                "state": billing["state"],
            },
        },
    )


def _update_stripe_tax_region(
    client: KakaoHttpClient,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    stripe_js_id: str,
    elements_session_id: str,
    billing: dict[str, str],
) -> None:
    _request_status(
        client,
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
        "kr_stripe_tax_region",
        headers=_stripe_headers(publishable_key, checkout_page),
        form={
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
            **_elements_params(stripe_js_id, elements_session_id),
            "tax_region[country]": billing["country"],
            "tax_region[postal_code]": billing["postal_code"],
            "tax_region[line1]": billing["line1"],
            "tax_region[city]": billing["city"],
            "tax_region[state]": billing["state"],
        },
    )


def _extract_redirect(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    action = payload.get("next_action")
    if isinstance(action, dict) and action.get("type") == "redirect_to_url":
        redirect = action.get("redirect_to_url")
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    for key in ("setup_intent", "payment_intent"):
        redirect = _extract_redirect(payload.get(key))
        if redirect:
            return redirect
    return ""


def _pre_confirm(
    client: KakaoHttpClient,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
) -> None:
    _request_status(
        client,
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/pre_confirm",
        "kr_stripe_pre_confirm",
        accepted_statuses=(200,),
        headers=_stripe_headers(publishable_key, checkout_page),
        form={
            "eid": str(uuid.uuid4()),
            "payment_method_type": "kakao_pay",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
        },
    )


def _create_payment_method(
    client: KakaoHttpClient,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    init_payload: dict[str, Any],
    billing: dict[str, str],
) -> tuple[str, str, str, str, str]:
    client_session_id = str(uuid.uuid4())
    guid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    muid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    sid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    body = {
        "type": "kakao_pay",
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": PROVIDER_COUNTRY,
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": billing["line2"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": STRIPE_VERSION,
        "key": publishable_key,
        "payment_user_agent": STRIPE_PAYMENT_UA,
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        body["client_attribution_metadata[checkout_config_id]"] = config_id
    payload = _request_required_json(
        client,
        "POST",
        "https://api.stripe.com/v1/payment_methods",
        "kr_stripe_payment_method",
        headers=_stripe_headers(publishable_key, checkout_page),
        form=body,
    )
    payment_method_id = str(payload.get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise WorkerFailure(
            "kakao_invalid_response",
            "Stripe response is missing a payment method",
            True,
            stage="kr_stripe_payment_method",
        )
    return payment_method_id, client_session_id, guid, muid, sid


def _confirm_payment(
    client: KakaoHttpClient,
    access_token: str,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    checkout: dict[str, Any],
    init_payload: dict[str, Any],
    stripe_js_id: str,
    elements_session_id: str,
    amount: int,
    payment_method_id: str,
    client_session_id: str,
    guid: str,
    muid: str,
    sid: str,
) -> str:
    processor_entity = _processor_entity(checkout, "kr_stripe_confirm")
    success_url = (
        f"https://chatgpt.com/backend-api/payments/checkout/"
        f"{processor_entity}/{checkout_id}/success?"
        f"billing_country={PROVIDER_COUNTRY}"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}?"
        "returned_from_redirect=true&ui_mode=custom&"
        f"return_url={quote(success_url, safe='')}"
    )
    body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": str(amount),
        "tax_id_collection[purchasing_as_business]": "false",
        "expected_payment_method_type": "kakao_pay",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": STRIPE_RUNTIME,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "link_brand": "link",
        **_elements_params(stripe_js_id, elements_session_id),
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        body["client_attribution_metadata[checkout_config_id]"] = config_id
    payload = _request_required_json(
        client,
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
        "kr_stripe_confirm",
        headers=_stripe_headers(publishable_key, checkout_page),
        form=body,
    )
    redirect = _extract_redirect(payload)
    submission = (
        payload.get("submission_attempt")
        if isinstance(payload.get("submission_attempt"), dict)
        else {}
    )
    requires_approval = submission.get("state") == "requires_approval" or bool(
        checkout.get("requires_manual_approval")
    )
    if not redirect and requires_approval:
        _approve_checkout(
            client,
            access_token,
            checkout_id,
            processor_entity,
        )
    return redirect


def _approve_checkout(
    client: KakaoHttpClient,
    access_token: str,
    checkout_id: str,
    processor_entity: str,
) -> None:
    for attempt in range(MAX_APPROVE_ATTEMPTS):
        payload = _request_required_json(
            client,
            "POST",
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            "kr_checkout_approve",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "oai-language": "ko-KR",
                "User-Agent": USER_AGENT,
                "Referer": (
                    f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}"
                ),
            },
            json_body={
                "checkout_session_id": checkout_id,
                "processor_entity": processor_entity,
            },
        )
        if payload.get("result") == "approved":
            return
        if attempt + 1 < MAX_APPROVE_ATTEMPTS:
            _sleep(1)
    raise WorkerFailure(
        "kakao_approval_failed",
        "Kakao checkout approval failed",
        True,
        stage="kr_checkout_approve",
    )


def _poll_redirect(
    client: KakaoHttpClient,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    stripe_js_id: str,
    elements_session_id: str,
    redirect: str,
) -> str:
    poll_deadline = min(_DEADLINE_TS, time.time() + REDIRECT_POLL_TIMEOUT)
    params = {
        "key": publishable_key,
        **_elements_params(stripe_js_id, elements_session_id),
    }
    while not redirect and time.time() < poll_deadline:
        status, text, _ = client.request(
            "GET",
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            stage="kr_stripe_redirect_poll",
            headers=_stripe_headers(publishable_key, checkout_page),
            params=params,
            timeout=8,
        )
        if status == 200:
            redirect = _extract_redirect(
                _json_object(text, "kr_stripe_redirect_poll")
            )
        if not redirect:
            _sleep(1)
    if not redirect:
        raise WorkerFailure(
            "kakao_redirect_timeout",
            "Kakao redirect was not ready before the deadline",
            True,
            stage="kr_stripe_redirect_poll",
        )
    return redirect


def _host_matches(host: str, allowed_hosts: frozenset[str]) -> bool:
    normalized = str(host or "").strip(".").lower()
    return normalized in allowed_hosts


def _validate_redirect_url(url: str, *, final: bool) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise WorkerFailure(
            "kakao_invalid_link",
            "Kakao redirect URL is invalid",
            True,
            stage="kr_provider_redirect",
        ) from exc
    allowed_hosts = PROVIDER_ALLOWED_HOSTS if final else INTERMEDIATE_ALLOWED_HOSTS
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not _host_matches(parsed.hostname, allowed_hosts)
        or port not in (None, 443)
    ):
        raise WorkerFailure(
            "kakao_invalid_link",
            "Kakao redirect URL is invalid",
            True,
            stage="kr_provider_redirect",
        )
    return parsed.geturl()


def _resolve_provider_redirect(
    client: KakaoHttpClient,
    redirect: str,
) -> str:
    current = _validate_redirect_url(redirect, final=False)
    for _ in range(MAX_REDIRECT_HOPS):
        parsed = urlsplit(current)
        if _host_matches(parsed.hostname or "", PROVIDER_ALLOWED_HOSTS):
            return _validate_redirect_url(current, final=True)
        status, _, headers = client.request(
            "GET",
            current,
            stage="kr_provider_redirect",
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
        location = headers.get("location", "")
        if status not in (301, 302, 303, 307, 308) or not location:
            break
        current = _validate_redirect_url(urljoin(current, location), final=False)
    return _validate_redirect_url(current, final=True)


def _validated_proxy_chain(
    proxy_url: str,
    route: str = PROXY_ROUTE_REFERENCE,
) -> tuple[tuple[str, str, str], list[dict[str, str]]]:
    normalized_route = normalize_proxy_route(route)
    countries = PROXY_ROUTE_COUNTRIES[normalized_route]
    proxies = kakao_proxy_chain(proxy_url, normalized_route)
    identities: dict[str, dict[str, str]] = {}
    role_names = ("checkout", "promotion", "provider")
    for proxy, country, role in zip(proxies, countries, role_names):
        if proxy not in identities:
            identities[proxy] = _verify_proxy_country(
                proxy,
                country,
                f"{country.lower()}_{role}_proxy_preflight",
            )
    egress: list[dict[str, str]] = []
    seen_regions: set[str] = set()
    for proxy, country in zip(proxies, countries):
        region = country.lower()
        if region in seen_regions:
            continue
        seen_regions.add(region)
        identity = identities[proxy]
        egress.append(
            {
                "region": region,
                "country": identity["country"],
                "ip": identity["ip"],
            }
        )
    return proxies, egress


def probe_kakao_proxy_chain(
    proxy_url: str,
    route: str = PROXY_ROUTE_REFERENCE,
) -> dict[str, Any]:
    normalized_route = normalize_proxy_route(route)
    _, egress = _validated_proxy_chain(proxy_url, normalized_route)
    return {"operation": "probe", "route": normalized_route, "egress": egress}


def extract_kakao_link(
    access_token: str,
    proxy_url: str,
    route: str = PROXY_ROUTE_REFERENCE,
    trial_eligibility_confirmed: bool = False,
) -> dict[str, Any]:
    if not trial_eligibility_confirmed:
        raise WorkerFailure(
            "kakao_trial_eligibility_required",
            "Free trial eligibility must be confirmed before extraction",
            False,
            stage="trial_eligibility",
        )
    proxies, _ = _validated_proxy_chain(proxy_url, route)
    checkout_proxy, promotion_proxy, provider_proxy = proxies
    stripe_targets = _resolve_stripe_connect_targets(provider_proxy)
    checkout_client = KakaoHttpClient(checkout_proxy, stripe_targets)
    promotion_client = KakaoHttpClient(promotion_proxy)
    provider_client = KakaoHttpClient(provider_proxy, stripe_targets)
    try:
        status, _, _ = checkout_client.request(
            "GET",
            "https://chatgpt.com/backend-api/me",
            stage="kr_account_validate",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": "https://chatgpt.com/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "OAI-Device-Id": str(uuid.uuid4()),
                "OAI-Language": "en-US",
            },
        )
        if status != 200:
            raise _http_failure(status, "kr_account_validate")
        checkout_id, publishable_key, checkout = _create_checkout(
            checkout_client,
            access_token,
        )
        checkout_page = _activate_stripe_checkout(checkout_client, checkout_id)
        bootstrap_payload, _ = _stripe_init(
            checkout_client,
            checkout_id,
            publishable_key,
            checkout_page,
            "kr_bootstrap_init",
        )
        _inspect_kakao_init(
            bootstrap_payload,
            "kr_bootstrap_init",
            require_zero=False,
        )
        _update_checkout_promotion(
            promotion_client,
            access_token,
            checkout_id,
            checkout,
        )
        init_payload, stripe_js_id = _stripe_init(
            provider_client,
            checkout_id,
            publishable_key,
            checkout_page,
            "kr_post_promotion_init",
        )
        _inspect_kakao_init(
            init_payload,
            "kr_post_promotion_init",
            require_zero=True,
        )
        billing = _random_billing()
        tax_elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        _update_checkout_taxes(
            provider_client,
            access_token,
            checkout_id,
            checkout,
            billing,
        )
        _update_stripe_tax_region(
            provider_client,
            checkout_id,
            publishable_key,
            checkout_page,
            stripe_js_id,
            tax_elements_session_id,
            billing,
        )
        init_payload, stripe_js_id = _stripe_init(
            provider_client,
            checkout_id,
            publishable_key,
            checkout_page,
            "kr_final_init",
        )
        amount = _inspect_kakao_init(
            init_payload,
            "kr_final_init",
            require_zero=True,
        )
        if amount is None:
            raise WorkerFailure(
                "kakao_invalid_response",
                "Stripe response is missing the checkout amount",
                True,
                stage="kr_final_init",
            )
        elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        _pre_confirm(
            provider_client,
            checkout_id,
            publishable_key,
            checkout_page,
        )
        (
            payment_method_id,
            client_session_id,
            guid,
            muid,
            sid,
        ) = _create_payment_method(
            provider_client,
            checkout_id,
            publishable_key,
            checkout_page,
            init_payload,
            billing,
        )
        redirect = _confirm_payment(
            provider_client,
            access_token,
            checkout_id,
            publishable_key,
            checkout_page,
            checkout,
            init_payload,
            stripe_js_id,
            elements_session_id,
            amount,
            payment_method_id,
            client_session_id,
            guid,
            muid,
            sid,
        )
        redirect = _poll_redirect(
            provider_client,
            checkout_id,
            publishable_key,
            checkout_page,
            stripe_js_id,
            elements_session_id,
            redirect,
        )
        link = _resolve_provider_redirect(provider_client, redirect)
        return {
            "link": link,
            "qr_text": link,
            "checkout_session_id": checkout_id,
            "amount": amount,
            "currency": "KRW",
        }
    finally:
        checkout_client.close()
        promotion_client.close()
        provider_client.close()


def _failure_payload(error: Exception) -> dict[str, Any]:
    if isinstance(error, WorkerFailure):
        payload: dict[str, Any] = {
            "code": error.code,
            "message": error.message,
            "transient": error.transient,
        }
        if error.stage:
            payload["stage"] = error.stage
        if error.http_status:
            payload["http_status"] = error.http_status
        return payload
    return {
        "code": "kakao_extraction_failed",
        "message": "Kakao extraction failed",
        "transient": True,
    }


def _run(raw: str) -> dict[str, Any]:
    global _DEADLINE_TS
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerFailure(
            "kakao_invalid_request", "Request must be valid JSON"
        ) from exc
    if not isinstance(request, dict):
        raise WorkerFailure("kakao_invalid_request", "Request must be a JSON object")
    protocol_version = request.get("protocol_version", 1)
    if protocol_version != 1:
        raise WorkerFailure(
            "kakao_protocol_unsupported",
            "Kakao worker protocol version is unsupported",
        )
    operation = str(request.get("operation") or "extract").strip().lower()
    if operation not in ("extract", "probe", "eligibility"):
        raise WorkerFailure(
            "kakao_operation_unsupported",
            "Kakao worker operation is unsupported",
        )
    proxy_route = normalize_proxy_route(request.get("route"))
    access_token = ""
    if operation in ("extract", "eligibility"):
        access_token = normalize_access_token(request.get("access_token"))
        if not access_token:
            raise WorkerFailure(
                "kakao_access_token_required",
                "Access token is required",
            )
    proxy_url = normalize_proxy_url(request.get("proxy_url"))
    if not proxy_url:
        raise WorkerFailure("kakao_proxy_required", "Proxy URL is required")
    requested_at = _parse_timestamp(request.get("requested_at"), "requested_at")
    deadline = _parse_timestamp(request.get("deadline"), "deadline")
    if deadline <= requested_at or deadline - requested_at > MAX_REQUEST_WINDOW:
        raise WorkerFailure("kakao_deadline_invalid", "Kakao deadline is invalid")
    now = datetime.now(timezone.utc)
    if requested_at > now + MAX_CLOCK_SKEW:
        raise WorkerFailure(
            "kakao_requested_at_invalid",
            "Kakao request time is invalid",
        )
    if deadline <= now:
        raise WorkerFailure(
            "kakao_deadline_exceeded",
            "Kakao extraction deadline was exceeded",
            True,
        )
    _DEADLINE_TS = min(deadline, now + MAX_REQUEST_WINDOW).timestamp()
    _load_curl_requests()
    if operation == "probe":
        return probe_kakao_proxy_chain(proxy_url, proxy_route)
    if operation == "eligibility":
        _validated_proxy_chain(proxy_url, proxy_route)
        return _check_kakao_trial_eligibility(access_token, proxy_url)
    if not bool(request.get("trial_eligibility_confirmed")):
        raise WorkerFailure(
            "kakao_trial_eligibility_required",
            "Free trial eligibility must be confirmed before extraction",
            False,
            stage="trial_eligibility",
        )
    result = extract_kakao_link(
        access_token,
        proxy_url,
        proxy_route,
        trial_eligibility_confirmed=True,
    )
    result.pop("payment_method_id", None)
    generated_at = datetime.now(timezone.utc)
    result["generated_at"] = _format_timestamp(generated_at)
    result["expires_at"] = _format_timestamp(generated_at + LINK_VALIDITY)
    result["expiry_source"] = "policy"
    return result


def main() -> int:
    try:
        raw = sys.stdin.read(1_048_577)
        if len(raw) > 1_048_576:
            raise WorkerFailure("kakao_invalid_request", "Request is too large")
        result = _run(raw)
        output = {"ok": True, "result": result}
        exit_code = 0
    except Exception as exc:
        output = {"ok": False, "error": _failure_payload(exc)}
        exit_code = 1
    sys.stdout.write(json.dumps(output, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
