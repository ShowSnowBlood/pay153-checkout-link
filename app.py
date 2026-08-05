from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import random
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from flask import Flask, jsonify, request, send_from_directory
from curl_cffi import requests

import stripe_checkout as sc
from provider_checkout import (
    PROVIDER_DEFAULTS,
    UPI_NEXT_ACTION_TYPE,
    amount_is_zero,
    default_billing,
    stripe_to_provider,
)
from proxy_pool import OPTIMIZER, PROVIDER_PROXY_COUNTRIES, ProxyLeaseRegistry, ProxyProbe
from sentinel_token import SentinelTokenProvider as BaseSentinel


ROOT = Path(__file__).resolve().parent
BACKEND_LOG_DIR = Path(os.getenv("PAY153_LOG_DIR", str(ROOT / "logs")))
LEASES = ProxyLeaseRegistry(os.getenv("PAY153_PROXY_LEASE_FILE", str(ROOT / "data" / "proxy_leases.json")))
LEGACY_SERVICE_BASE = str(os.getenv("PAY153_LEGACY_BASE", "")).rstrip("/")
CONFIGURED_PROXY_GATEWAY = str(os.getenv("PAY153_PROXY_GATEWAY", "")).strip()
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config["JSON_AS_ASCII"] = False

PLANS = {
    "plus": "chatgptplusplan",
    "pro": "chatgptpro",
    "team": "chatgptteamplan",
    "codex_low": "chatgptbusiness_usage_based",
}

OPENAI_CHECKOUT_CURRENCIES = {
    "USD", "AUD", "CAD", "GBP", "EUR", "CLP", "JPY", "INR", "IDR", "PKR",
    "THB", "MYR", "TWD", "VND", "PHP", "NGN", "ZAR", "KZT", "TZS", "EGP",
    "BRL", "SEK", "CZK", "PLN", "DKK", "NOK", "KRW", "COP", "MXN", "PEN",
    "HUF", "QAR", "RON", "ILS", "AED", "SGD", "NZD", "CHF", "SAR",
}

# 国家接口可能返回 OpenAI Checkout 尚未接受的本地币种，例如 BA/BAM。
# 欧洲非欧元国家遇到未开放币种时优先使用 EUR，其余地区回退 USD。
EURO_CURRENCY_FALLBACK_COUNTRIES = {
    "AL", "AD", "AM", "BA", "BG", "BY", "CY", "EE", "GE", "HR", "IS", "LI",
    "LT", "LV", "MC", "MD", "ME", "MK", "MT", "RS", "SM", "SK", "SI", "TR",
    "UA", "VA", "XK",
}


def normalize_checkout_currency(country: str, currency: str = "") -> tuple[str, str]:
    country = str(country or "US").strip().upper()
    detected = str(currency or "").strip().upper()
    if detected in OPENAI_CHECKOUT_CURRENCIES:
        return detected, "代理地区接口"
    mapped = str(sc.currency_for_country(country) or "").upper()
    if country in EURO_CURRENCY_FALLBACK_COUNTRIES and detected not in OPENAI_CHECKOUT_CURRENCIES:
        return "EUR", f"OpenAI币种回退（{detected or mapped or '未知'}→EUR）"
    if mapped in OPENAI_CHECKOUT_CURRENCIES:
        return mapped, "国家币种映射"
    return "USD", f"OpenAI币种回退（{detected or mapped or '未知'}→USD）"


COUNTRY_CURRENCY = {
    country: normalize_checkout_currency(country, currency)[0]
    for country, currency in sc.COUNTRY_CURRENCY.items()
}

_TOKEN_JOB_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_JOB_LOCKS_GUARD = threading.Lock()


def checkout_token_lock(raw_token: str) -> threading.Lock:
    key = hashlib.sha256(str(raw_token or "").strip().encode("utf-8")).hexdigest()
    with _TOKEN_JOB_LOCKS_GUARD:
        return _TOKEN_JOB_LOCKS.setdefault(key, threading.Lock())

PAYPAL_CHECKOUT_REGIONS = {
    country: currency
    for country, currency in sc.COUNTRY_CURRENCY.items()
    if currency in OPENAI_CHECKOUT_CURRENCIES
}


def normalize_paypal_checkout_region(country: str, detected_currency: str = "") -> tuple[str, str, str]:
    # Prefer the proxy country native PayPal Checkout; otherwise use DE/EUR.
    country = str(country or "US").strip().upper()
    detected = str(detected_currency or "").strip().upper()
    direct_countries = {str(item).upper() for item in getattr(sc, "PAYPAL_ORDER_COUNTRIES", [])}
    if country in direct_countries:
        currency, source = normalize_checkout_currency(country, detected)
        return country, currency, f"\u5f53\u524d\u56fd\u5bb6\u652f\u6301 PayPal\uff08{source}\uff09"
    return "DE", "EUR", f"\u5f53\u524d\u56fd\u5bb6 {country} \u672a\u5217\u5165 PayPal \u8d26\u5355\u5730\u533a\uff0c\u56de\u9000 DE/EUR"


class ProxySentinel(BaseSentinel):
    def __init__(self, proxy: str | None, cookies: dict[str, str]):
        super().__init__(impersonate="chrome136", cookies=cookies)
        self.proxy = proxy

    async def _get_session(self):
        if not self._session:
            kwargs: dict[str, Any] = {"impersonate": "chrome136", "timeout": 70}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = requests.AsyncSession(**kwargs)
        return self._session


class AttemptNetworkContext:
    """Keep one TLS/cookie session per concrete proxy for a complete attempt."""

    def __init__(self, entry_proxy: str, exit_proxy: str, device_id: str, did: str):
        self.entry_proxy = entry_proxy
        self.exit_proxy = exit_proxy
        self.device_id = device_id
        self.did = did
        self._sessions: dict[str, Any] = {}

    def http(self, proxy: str):
        if proxy not in self._sessions:
            session = sc.build_http(proxy or None)
            try:
                session.cookies.set("oai-did", self.did, domain="chatgpt.com")
            except Exception:
                pass
            self._sessions[proxy] = session
        return self._sessions[proxy]

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()


def approval_session_candidates(
    provider: str,
    promo_requested: bool,
    single_chain: bool,
    *,
    exit_proxy: str,
    checkout_proxy: str,
    checkout_http: object,
    provider_http: object,
) -> list[tuple[str, object, str]]:
    """Pair every approval HTTP session with the proxy that created it."""
    candidates: list[tuple[str, object, str]] = []
    if provider == "upi" and promo_requested and not single_chain:
        candidates.append((exit_proxy, provider_http, "IN 支付 Session"))
    candidates.append((checkout_proxy, checkout_http, "Checkout 创建 Session"))
    return candidates


def validate_provider_result(provider: str, result: dict) -> None:
    """Reject local-payment placeholders before a job can be marked done."""
    if str(provider or "").lower() != "upi":
        return
    if result.get("fallback_reason"):
        raise RuntimeError("upi_invalid_fallback_result")
    if result.get("promo_requested"):
        if result.get("promo_applied") is not True:
            raise RuntimeError("upi_promo_not_applied")
        if not amount_is_zero(result.get("checkout_amount")):
            raise RuntimeError("upi_checkout_amount_not_zero")
    if not any(
        result.get(key)
        for key in (
            "provider_redirect_url",
            "qr_image_png",
            "qr_image_svg",
            "qr_data",
        )
    ):
        raise RuntimeError("upi_no_action_after_confirm")
    if result.get("next_action_type") != UPI_NEXT_ACTION_TYPE:
        raise RuntimeError("upi_invalid_next_action_type")
    redirect = str(result.get("provider_redirect_url") or "")
    redirect_host = str(urlsplit(redirect).hostname or "").lower() if redirect else ""
    if redirect_host in {"pay.openai.com", "checkout.stripe.com"}:
        raise RuntimeError("upi_checkout_url_is_not_action")


def _decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
    except Exception:
        return {}


def extract_access_token(raw: str) -> tuple[str, dict]:
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("请填写 Access Token 或 Session JSON")
    token = ""
    meta: dict[str, Any] = {}
    if raw.startswith("{"):
        data = json.loads(raw)
        token = str(data.get("accessToken") or data.get("access_token") or "")
        account = data.get("account") or {}
        if isinstance(account, dict):
            meta.update(account)
    if not token:
        match = re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", raw)
        token = match.group(0) if match else raw.splitlines()[0].strip()
    if token.count(".") < 2:
        raise ValueError("Access Token 格式未识别")
    claims = _decode_jwt(token)
    meta.update({
        "email": claims.get("email") or meta.get("email") or "",
        "exp": claims.get("exp"),
        "account_id": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
            or meta.get("id") or "",
    })
    if meta.get("exp") and int(meta["exp"]) <= int(time.time()):
        raise ValueError("Access Token 已过期")
    return token, meta


def normalize_proxy(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""

    def host_port(text: str) -> tuple[str, int]:
        text = text.strip()
        if text.startswith("[") and "]:" in text:
            host, port_text = text[1:].split("]:", 1)
            host = f"[{host}]"
        else:
            if ":" not in text:
                raise ValueError("代理缺少端口")
            host, port_text = text.rsplit(":", 1)
        if not host or not port_text.isdigit():
            raise ValueError("代理主机或端口格式不正确")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("代理端口超出范围")
        return host, port

    def credentials(text: str) -> tuple[str, str]:
        if ":" not in text:
            raise ValueError("代理凭据格式应为 username:password")
        username, password = text.split(":", 1)
        if not username or not password:
            raise ValueError("代理用户名和密码为空")
        return username, password

    def build(scheme: str, host: str, port: int, username: str = "", password: str = "") -> str:
        bare_host = host.strip("[]").lower()
        if bare_host == "rp.scrapegw.com" and port == 6060:
            # Scrapegw's residential endpoint speaks authenticated HTTP.
            # Its session/lifetime username fields pin one real exit IP.
            scheme = "http"
            lowered_username = username.lower()
            if username and "-session-" not in lowered_username:
                username += "-session-__rotate__-lifetime-120"
            elif username and "-lifetime-" not in lowered_username:
                username += "-lifetime-120"
        auth = ""
        if username or password:
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return f"{scheme}://{auth}{host}:{port}"

    if "://" in value:
        scheme, remainder = value.split("://", 1)
        scheme = scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"代理协议 {scheme} 暂未支持")
        # Some proxy vendors publish scheme://host:port:user:password.
        # Normalize that transport notation into a standard authenticated URL.
        parts = remainder.split(":")
        if "@" not in remainder and len(parts) >= 4 and parts[1].isdigit():
            host, port = host_port(f"{parts[0]}:{parts[1]}")
            return build(scheme, host, port, parts[2], ":".join(parts[3:]))
        if "@" not in remainder and len(parts) >= 4 and parts[-1].isdigit():
            host, port = host_port(f"{parts[-2]}:{parts[-1]}")
            return build(scheme, host, port, parts[0], ":".join(parts[1:-2]))
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("代理端口格式不正确") from exc
        if not parsed.hostname or port is None:
            raise ValueError("代理 URL 缺少主机或端口")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return build(scheme, host, port, unquote(parsed.username or ""), unquote(parsed.password or ""))

    if value.count("@") == 1:
        left, right = value.split("@", 1)
        try:
            username, password = credentials(left)
            host, port = host_port(right)
            return build("http", host, port, username, password)
        except ValueError:
            host, port = host_port(left)
            username, password = credentials(right)
            return build("http", host, port, username, password)

    parts = value.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = host_port(f"{parts[0]}:{parts[1]}")
        return build("http", host, port, parts[2], ":".join(parts[3:]))
    if len(parts) >= 4 and parts[-1].isdigit():
        host, port = host_port(f"{parts[-2]}:{parts[-1]}")
        return build("http", host, port, parts[0], ":".join(parts[1:-2]))

    host, port = host_port(value)
    return build("http", host, port)


def normalize_proxy_pool(raw: Any, label: str) -> list[str]:
    if isinstance(raw, (list, tuple)):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [line.strip() for line in str(raw or "").replace("\r", "").split("\n")]
    values = [value for value in values if value]
    if len(values) > 500:
        raise ValueError(f"{label}最多填写 500 条")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values, 1):
        try:
            proxy = normalize_proxy(value)
        except ValueError as exc:
            raise ValueError(f"{label}第 {index} 条：{exc}") from exc
        if proxy not in seen:
            normalized.append(proxy)
            seen.add(proxy)
    return normalized


def generate_cpf() -> str:
    digits = [secrets.randbelow(10) for _ in range(9)]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def generate_cnpj() -> str:
    digits = [secrets.randbelow(10) for _ in range(12)]
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def generate_pix_identity(kind: str) -> dict[str, str]:
    first_names = ("Lucas", "Gabriel", "Rafael", "Matheus", "Mariana", "Beatriz", "Camila", "Larissa")
    last_names = ("Silva", "Santos", "Oliveira", "Souza", "Pereira", "Costa", "Rodrigues", "Almeida")
    locations = (
        ("Avenida Paulista 1000", "Sao Paulo", "SP", "01310-100"),
        ("Rua da Assembleia 10", "Rio de Janeiro", "RJ", "20011-901"),
        ("Avenida Afonso Pena 1500", "Belo Horizonte", "MG", "30130-005"),
        ("Rua XV de Novembro 500", "Curitiba", "PR", "80020-310"),
        ("Avenida Sete de Setembro 800", "Salvador", "BA", "40060-001"),
    )
    first, last = secrets.choice(first_names), secrets.choice(last_names)
    line1, city, state, postal_code = secrets.choice(locations)
    if kind == "cnpj":
        name = f"{first.upper()} {last.upper()} COMERCIO E SERVICOS LTDA"
        source = "generated_cnpj"
    else:
        name = f"{first} {last}"
        source = "generated_cpf"
    return {
        "name": name,
        "email": f"{first.lower()}.{last.lower()}{secrets.randbelow(9000) + 1000}@outlook.com",
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "source": source,
    }


def lookup_cnpj_identity(cnpj: str) -> dict[str, str]:
    value = re.sub(r"\D", "", cnpj or "")
    if len(value) != 14:
        return {}
    resp = requests.get(
        f"https://brasilapi.com.br/api/cnpj/v1/{value}",
        headers={"Accept": "application/json", "User-Agent": sc.CHROME_UA},
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"CNPJ 登记信息查询 HTTP {resp.status_code}")
    data = resp.json() or {}
    street = " ".join(filter(None, [str(data.get("logradouro") or "").strip(), str(data.get("numero") or "").strip()]))
    complement = str(data.get("complemento") or "").strip()
    if complement:
        street = f"{street}, {complement}" if street else complement
    return {
        "name": str(data.get("razao_social") or data.get("nome_fantasia") or "").strip(),
        "line1": street,
        "city": str(data.get("municipio") or "").strip(),
        "state": str(data.get("uf") or "").strip(),
        "postal_code": str(data.get("cep") or "").strip(),
        "status": str(data.get("descricao_situacao_cadastral") or "").strip(),
        "source": "brasilapi_cnpj",
    }


async def sentinel_headers(proxy: str, flow: str, device_id: str, cookie: str) -> dict[str, str]:
    provider = ProxySentinel(proxy or None, {"oai-did": cookie})
    try:
        token, so, diag = await provider.get_token_pair(flow, device_id)
        if not token:
            raise RuntimeError("Sentinel token 生成失败")
        if diag.get("turnstile_required") and not diag.get("has_t"):
            raise RuntimeError("Sentinel 缺少 t")
        if diag.get("so_required") and not diag.get("has_so"):
            raise RuntimeError("Sentinel 缺少 so")
        out = {"OpenAI-Sentinel-Token": json.dumps(token, separators=(",", ":"))}
        if so:
            out["OpenAI-Sentinel-SO-Token"] = json.dumps(so, separators=(",", ":"))
        return out
    finally:
        await provider.close()


def checkout_payload(options: dict, meta: dict) -> dict[str, Any]:
    plan = options["plan"]
    country = options.get("checkout_country") or options["country"]
    requested_currency = options.get("checkout_currency") or options["currency"]
    currency, _currency_source = normalize_checkout_currency(country, requested_currency)
    options["currency"] = currency
    options["checkout_currency"] = currency
    billing = {"country": country, "currency": currency}
    link_type = str(options.get("link_type") or "hosted")
    # Prefer custom UI so OpenAI returns a real Stripe cs_live_* for Payment Page.
    # Hosted/redirect often yields only oaicss_* which Stripe API rejects.
    # On UPI zero-due, still use custom; promo may be attached via promo_on_create.
    if link_type != "hosted" or plan == "codex_low":
        ui_mode = "custom"
    else:
        ui_mode = "redirect"
    common: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": PLANS[plan],
        "billing_details": billing,
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": ui_mode,
        "check_card_proxy": True,
    }
    promo = options.get("promo_campaign", "").strip()
    if plan == "team":
        common["entry_point"] = "team_workspace_purchase_modal"
        team_data = {
            "workspace_name": options.get("workspace_name") or "Codex Workspace",
            "price_interval": options.get("price_interval") or "month",
            "seat_quantity": int(options.get("seat_quantity") or 5),
        }
        if options.get("workspace_id"):
            team_data["existing_workspace_id"] = options["workspace_id"]
        common["team_plan_data"] = team_data
        if options.get("promo_code"):
            common["promo_code"] = options["promo_code"]
    elif plan == "codex_low":
        common["entry_point"] = "codex_team_start"
        common["usage_based_workspace_credit_purchase_data"] = {
            "quantity": int(options.get("credit_quantity") or 13),
            "unit": "credit",
            "workspace_name": options.get("workspace_name") or "Codex Space",
            "plan_type": "team",
            "auto_top_up_enabled": True,
        }
    elif plan == "plus" and options.get("use_promo") and (
        options.get("link_type") not in {"pix", "paypal", "upi", "ideal"}
        or options.get("promo_on_create")
    ):
        common["promo_campaign"] = {
            "promo_campaign_id": promo or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    return common


def create_checkout(token: str, payload: dict, proxy: str, device_id: str, did: str, log, *, http=None) -> dict:
    http = http or sc.build_http(proxy or None)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    try:
        http.get("https://chatgpt.com/", headers={"User-Agent": sc.CHROME_UA}, timeout=35)
    except Exception as exc:
        log(f"ChatGPT 暖身提示：{type(exc).__name__}")
    s_headers = asyncio.run(sentinel_headers(proxy, "chatgpt_checkout", device_id, did))
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": sc.CHROME_UA,
        "OAI-Language": "zh-CN",
        "OAI-Device-Id": device_id,
        **s_headers,
    }
    resp = http.post(sc.OPENAI_CHECKOUT_URL, json=payload, headers=headers, timeout=60)
    text = resp.text or ""
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI Checkout HTTP {resp.status_code}: {text[:500]}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"OpenAI Checkout 返回非 JSON：{text[:300]}")
    sid = str(data.get("checkout_session_id") or "")
    url = str(
        data.get("url")
        or data.get("checkout_url")
        or data.get("redirect_url")
        or data.get("checkout_link")
        or ""
    )
    # Also scan nested checkout_session objects (custom vs hosted variants).
    nested = data.get("checkout_session") if isinstance(data.get("checkout_session"), dict) else {}
    if not sid:
        sid = str(nested.get("checkout_session_id") or nested.get("id") or "")
    if not url:
        url = str(nested.get("url") or nested.get("checkout_url") or "")
    # Hosted/zero-due responses sometimes put an OpenAI-internal oaicss_* id in
    # checkout_session_id while the real Stripe cs_live_* only appears in url,
    # nested JSON, or a follow-up pay.openai.com redirect.
    stripe_sid = ""
    blob_sources = [url, text, sid, json.dumps(data, ensure_ascii=False, default=str)]
    for source in blob_sources:
        match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", str(source or ""))
        if match:
            stripe_sid = match.group(0)
            break
    tag = str(data.get("tag") or (nested.get("tag") if isinstance(nested, dict) else "") or "")
    ui_mode_resp = str(
        data.get("checkout_ui_mode")
        or (nested.get("checkout_ui_mode") if isinstance(nested, dict) else "")
        or payload.get("checkout_ui_mode")
        or ""
    )
    if not stripe_sid and (url or sid.startswith("oaics_")):
        # Hosted/redirect SPA pages rarely embed cs_live in static HTML.
        # Quick probe only when a url is present; oaicss-without-url fails fast.
        probe_urls: list[str] = []
        if url:
            probe_urls.append(url)
        if sid.startswith("oaics_") and url:
            probe_urls.append(f"https://pay.openai.com/c/pay/{sid}")
        if probe_urls:
            log(
                f"Checkout 仅有 oaicss/无 cs_live，尝试落地解析 tag={tag or '-'} "
                f"ui={ui_mode_resp or '-'} url=yes probes={len(probe_urls)}"
            )
        for probe in probe_urls[:2]:
            try:
                probe_resp = http.get(
                    probe,
                    headers={"User-Agent": sc.CHROME_UA, "Accept": "text/html,application/json"},
                    timeout=12,
                    allow_redirects=True,
                )
                final_url = str(getattr(probe_resp, "url", "") or "")
                probe_text = f"{final_url} {getattr(probe_resp, 'text', '') or ''}"
                match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", probe_text)
                if match:
                    stripe_sid = match.group(0)
                    log(f"Checkout 从落地页解析到 Stripe 会话：{stripe_sid[:28]}…")
                    break
            except Exception as exc:  # noqa: BLE001
                log(f"Checkout oaicss 落地解析提示：{type(exc).__name__}: {exc}")
    if stripe_sid:
        sid = stripe_sid
    elif sid.startswith("oaics_"):
        # Keep a clear failure path rather than feeding oaicss into Stripe.
        raise RuntimeError(
            f"OpenAI Checkout 未返回 Stripe cs_live 会话（仅有 {sid}；"
            f"tag={tag or '-'} ui={ui_mode_resp or '-'}），"
            "请换代理并以 custom 无 promo 创建后 checkout/update 优惠"
        )
    if not sid:
        match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", text)
        sid = match.group(0) if match else ""
    data["checkout_session_id"] = sid
    # A session id plus a copied fragment is not a signed Hosted Checkout URL.
    data["checkout_url"] = url
    return {"data": data, "http": http}


def preflight_trial_eligibility(
    token: str, account_id: str, proxy: str, device_id: str, did: str, log, *, http=None,
) -> dict:
    if not account_id:
        return {}
    http = http or sc.build_http(proxy)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": sc.CHROME_UA,
        "OAI-Language": "zh-CN",
        "OAI-Device-Id": device_id,
    }
    try:
        resp = http.get(
            "https://chatgpt.com/backend-api/payments/payment_methods",
            params={"account_id": account_id},
            headers=headers,
            timeout=35,
        )
        if resp.status_code != 200:
            log(f"优惠预检返回 HTTP {resp.status_code}")
            return {}
        data = resp.json() or {}
        log(
            "入口支付标记 one_click_trial_eligible={}（仅为 payment_methods 字段，不作为活动资格判定）".format(
                data.get("one_click_trial_eligible")
            )
        )
        return data
    except Exception as exc:
        log(f"优惠预检提示：{type(exc).__name__}")
        return {}


def promo_campaign_from_payload(payload: Any) -> str:
    """Extract the account-specific campaign id returned by OpenAI.

    Campaign ids are not guaranteed to stay equal to the UI label.  The update
    endpoint may accept a stale/default id and still return ``success=true``,
    while final approval rejects it as ``invalid_promotion``.
    """
    candidates: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower in {
                    "promo_campaign_id",
                    "promotion_campaign_id",
                    "campaign_id",
                } and isinstance(item, str):
                    candidate = item.strip()
                    if candidate:
                        candidates.append(candidate)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return candidates[0] if candidates else ""


def upi_promo_is_explicitly_unavailable(payload: Any) -> bool:
    """Return true only when the server denies the marker and no campaign exists."""
    return (
        isinstance(payload, dict)
        and payload.get("one_click_trial_eligible") is False
        and not promo_campaign_from_payload(payload)
    )


def proxy_geo(proxy: str) -> dict[str, str]:
    http = sc.build_http(proxy)
    probes = (
        "https://ipapi.co/json/",
        "http://ip-api.com/json/?fields=status,countryCode,regionName,city,zip,timezone,currency,query",
        "https://ipinfo.io/json",
    )
    errors: list[str] = []
    for url in probes:
        try:
            resp = http.get(url, timeout=20)
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
                continue
            data = resp.json() or {}
            country = str(data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper()
            if len(country) != 2:
                continue
            currency = str(data.get("currency") or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                currency = ""
            return {
                "country": country,
                "currency": currency,
                "region": str(data.get("region") or data.get("region_name") or data.get("regionName") or ""),
                "city": str(data.get("city") or ""),
                "postal": str(data.get("postal") or data.get("zip") or ""),
                "timezone": str(data.get("timezone") or ""),
            }
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise RuntimeError(f"代理地区检测失败：{' / '.join(errors[-3:]) or 'no response'}")


_PROXY_GEO_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_PROXY_GEO_CACHE_LOCK = threading.Lock()


def cache_proxy_probe(probe: ProxyProbe) -> None:
    if not probe.country:
        return
    with _PROXY_GEO_CACHE_LOCK:
        _PROXY_GEO_CACHE[probe.proxy_url] = (time.time(), probe.geo())


def proxy_geo_cached(proxy: str, ttl: int = 900) -> dict[str, str]:
    now = time.time()
    with _PROXY_GEO_CACHE_LOCK:
        cached = _PROXY_GEO_CACHE.get(proxy)
        if cached and now - cached[0] <= ttl:
            return dict(cached[1])
    data = proxy_geo(proxy)
    with _PROXY_GEO_CACHE_LOCK:
        _PROXY_GEO_CACHE[proxy] = (now, dict(data))
    return data


def select_paypal_exit_proxy(preferred: str, pool: list[str], scan_limit: int = 24) -> tuple[str, dict[str, str], list[str]]:
    """Pick a proxy whose detected country has an exact OpenAI billing pair."""
    rest = [proxy for proxy in dict.fromkeys(pool) if proxy and proxy != preferred]
    random.SystemRandom().shuffle(rest)
    candidates = ([preferred] if preferred else []) + rest
    candidates = candidates[:max(1, min(int(scan_limit), len(candidates)))]
    if not candidates:
        raise RuntimeError("代理池 2 为空")

    rejected: list[str] = []
    executor = ThreadPoolExecutor(max_workers=min(6, len(candidates)), thread_name_prefix="paypal-geo")
    future_map = {executor.submit(proxy_geo_cached, proxy): proxy for proxy in candidates}
    try:
        for future in as_completed(future_map):
            proxy = future_map[future]
            try:
                geo = future.result()
            except Exception:
                continue
            country = str(geo.get("country") or "").upper()
            if re.fullmatch(r"[A-Z]{2}", country):
                for pending in future_map:
                    if pending is not future:
                        pending.cancel()
                return proxy, geo, rejected
            if country and country not in rejected:
                rejected.append(country)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    summary = "/".join(rejected[:12]) or "未识别"
    raise RuntimeError(
        f"代理池 2 本轮未找到 OpenAI 支持的 PayPal 账单地区；已检测：{summary}。"
        "系统将更换代理继续尝试"
    )


def proxy_country(proxy: str) -> tuple[str, str]:
    data = proxy_geo_cached(proxy)
    return data["country"], data["region"]


def checkout_proxy_route(options: dict) -> tuple[str, str, bool]:
    """Return entry country, payment country, and whether both share one Session."""
    provider = str(options.get("link_type") or "hosted").lower()
    checkout_country = str(options.get("country") or "").upper()
    if provider == "upi" and options.get("use_promo"):
        promo_country = str(options.get("promo_proxy_country") or "JP").upper()
        return promo_country, "IN", False
    target_country = PROVIDER_PROXY_COUNTRIES.get(provider, "") or (
        checkout_country if provider == "hosted" else ""
    )
    return target_country, target_country, provider in {"hosted", "ideal", "upi", "pix"}


def update_checkout_promo(
    http,
    token: str,
    session_id: str,
    processor_entity: str,
    campaign_id: str,
    log,
    *,
    device_id: str = "",
) -> dict:
    body = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "plan_name": PLANS["plus"],
        "price_interval": "month",
        "seat_quantity": 1,
        "discount_code": None,
        "promo_campaign": {
            "promo_campaign_id": campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/update",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        },
        timeout=45,
    )
    text = resp.text or ""
    log(f"[promo] checkout/update: {resp.status_code} {text[:180]}")
    if resp.status_code != 200:
        raise RuntimeError(f"应用 Plus 优惠失败：HTTP {resp.status_code} {text[:300]}")
    try:
        return resp.json() or {}
    except Exception:
        return {}


def approve_checkout(
    token: str,
    session_id: str,
    processor: str,
    proxy: str,
    device_id: str,
    did: str,
    *,
    http=None,
    log=lambda _message: None,
) -> dict:
    headers = asyncio.run(sentinel_headers(proxy, "checkout_session_approval", device_id, did))
    http = http or sc.build_http(proxy or None)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    body = {"checkout_session_id": session_id, "processor_entity": processor}
    resp = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "OAI-Device-Id": device_id,
            "User-Agent": sc.CHROME_UA,
            "OAI-Language": "zh-CN",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
            **headers,
        },
        timeout=40,
    )
    text = resp.text or ""
    log(f"[stripe] manual_approval approve+sentinel: {resp.status_code} {text[:160]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Checkout approve HTTP {resp.status_code}: {text[:300]}")
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {}
    result = str(payload.get("result") or "").lower()
    if result and result != "approved":
        # Some local-method zero-due flows return HTTP 200 with
        # {"result":"exception"} even though Stripe has already accepted the
        # manual approval asynchronously. Do not discard the zero-amount
        # Payment Page immediately; let the caller poll Stripe for the actual
        # next_action/QR and fail only if no provider result appears.
        if result in {"exception", "blocked"}:
            log(
                f"[stripe] manual_approval approve returned {result}; "
                "continuing Stripe poll / recovery"
            )
            payload["_approve_soft_fail"] = result
            return payload
        raise RuntimeError(f"manual_approval approve blocked: result={result}")
    return payload


class JobStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.file_lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.worker_limit = max(1, int(os.getenv("PAY153_WORKERS", "20")))
        self.global_rpm = max(1, int(os.getenv("PAY153_GLOBAL_RPM", "20")))
        self.pool = ThreadPoolExecutor(max_workers=self.worker_limit)
        self.pending: deque[tuple[str, dict]] = deque()
        self.start_times: deque[float] = deque()
        self.active_workers = 0
        threading.Thread(target=self._dispatch_loop, name="pay153-dispatcher", daemon=True).start()

    @staticmethod
    def _is_major_log(message: str) -> bool:
        text = str(message or "")
        lowered = text.lower()
        return any(marker in text for marker in (
            "提链尝试", "代理池", "代理校验", "自动设置地区", "计划=",
            "优惠已", "优惠更新", "优惠同步", "金额校验", "今日应付",
            "Checkout 创建", "支付方式已创建", "二维码生成", "链接生成",
            "提交 Checkout approval", "错误：", "本次未成功",
        )) or any(marker in lowered for marker in (
            "init ok", "payment_method:", "manual_approval approve", "checkout/update",
        ))

    def _append_backend_log(self, job_id: str, kind: str, message: str):
        safe_message = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(message))
        day = time.strftime("%Y-%m-%d")
        path = BACKEND_LOG_DIR / day / f"{job_id}.log"
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{kind}] {safe_message}\n"
        try:
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:
            pass

    def _record_success(self, job_id: str, result: dict):
        """Persist successful link results so batch runs survive restarts."""
        try:
            if result.get("fallback_reason"):
                return
            link_type = str(result.get("link_type") or "").lower()
            validate_provider_result(link_type, result)
            action_kind = ""
            action_value = ""
            if link_type == "upi":
                for key in (
                    "provider_redirect_url",
                    "qr_data",
                    "qr_image_png",
                    "qr_image_svg",
                ):
                    value = str(result.get(key) or "")
                    if value:
                        action_kind = key
                        action_value = value
                        break
            else:
                action_kind = "url"
                action_value = str(
                    result.get("provider_redirect_url")
                    or result.get("paypal_link")
                    or result.get("url")
                    or result.get("link")
                    or result.get("checkout_url")
                    or ""
                )
            record = {
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "job_id": job_id,
                "combination": "{}-{}".format(
                    str(result.get("entry_country") or "?").upper(),
                    str(result.get("payment_proxy_country") or result.get("checkout_country") or "?").upper(),
                ),
                "attempt": result.get("attempt"),
                "max_attempts": result.get("max_attempts"),
                "account_email": result.get("account_email") or "",
                "link_type": result.get("link_type") or "",
                "checkout_amount": result.get("checkout_amount"),
                "currency": result.get("checkout_currency") or result.get("currency") or "",
                "promo_applied": result.get("promo_applied"),
                "action_kind": action_kind,
                "action_value": action_value,
                "url": action_value,
            }
            path = ROOT / "data" / "success_links.jsonl"
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                path.chmod(0o600)
        except Exception:
            pass

    def _refresh_queue_locked(self):
        for position, (job_id, _options) in enumerate(self.pending, 1):
            job = self.jobs.get(job_id)
            if not job:
                continue
            job["queue_position"] = position
            job["text"] = f"正在排队，前方 {position - 1} 个任务" if position > 1 else "正在排队，等待执行"
            job["updated_at"] = time.time()

    def _worker_done(self, _future):
        with self.condition:
            self.active_workers = max(0, self.active_workers - 1)
            self.condition.notify_all()

    def _dispatch_loop(self):
        while True:
            with self.condition:
                now = time.time()
                while self.start_times and now - self.start_times[0] >= 60:
                    self.start_times.popleft()

                if not self.pending or self.active_workers >= self.worker_limit:
                    self.condition.wait(timeout=1)
                    continue

                if len(self.start_times) >= self.global_rpm:
                    wait_seconds = max(0.1, 60 - (now - self.start_times[0]))
                    self.condition.wait(timeout=min(wait_seconds, 2))
                    continue

                job_id, options = self.pending.popleft()
                job = self.jobs.get(job_id)
                if not job or job.get("cancel"):
                    if job:
                        job.update(status="cancelled", percent=100, text="任务已停止", queue_position=0)
                    self._refresh_queue_locked()
                    continue

                self.active_workers += 1
                self.start_times.append(now)
                job.update(text="排队完成，即将开始", queue_position=0, dispatched=True, updated_at=now)
                self._refresh_queue_locked()
                future = self.pool.submit(self._run, job_id, options)
                future.add_done_callback(self._worker_done)

    def create(self, options: dict) -> str:
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self.lock:
            expired = [
                key for key, value in self.jobs.items()
                if now - float(value.get("updated_at") or now) > 7200
            ]
            for key in expired:
                self.jobs.pop(key, None)
            if len(self.jobs) >= 500:
                oldest = sorted(self.jobs, key=lambda key: self.jobs[key].get("updated_at", 0))
                for key in oldest[: len(self.jobs) - 499]:
                    self.jobs.pop(key, None)
            self.jobs[job_id] = {
                "id": job_id, "status": "queued", "percent": 2, "text": "任务已创建",
                "logs": [], "result": None, "error": "", "cancel": False,
                "created_at": now, "updated_at": now, "queue_position": 0, "dispatched": False,
            }
            self.pending.append((job_id, options))
            self._refresh_queue_locked()
            self.condition.notify_all()
        self._append_backend_log(job_id, "SYSTEM", "任务已创建并进入队列")
        return job_id

    def queue_position(self, job_id: str) -> int:
        with self.lock:
            return int((self.jobs.get(job_id) or {}).get("queue_position") or 0)

    def update(self, job_id: str, **fields):
        backend_line = ""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            # A running worker can still be inside a synchronous HTTP request
            # for a few seconds after the user presses stop.  Keep the public
            # state terminal immediately and do not let that worker overwrite
            # `cancelled` with another running/error progress update.
            if (
                job.get("cancel")
                and job.get("status") == "cancelled"
                and fields.get("status") != "cancelled"
            ):
                return
            job.update(fields)
            job["updated_at"] = time.time()
            if "text" in fields or "status" in fields:
                backend_line = f"status={job.get('status')} percent={job.get('percent')} text={job.get('text')}"
        if backend_line:
            self._append_backend_log(job_id, "STATUS", backend_line)

    def log(self, job_id: str, message: str):
        safe = re.sub(r"eyJ[A-Za-z0-9_.-]{40,}", "[TOKEN]", str(message))
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job["logs"].append({
                    "time": time.strftime("%H:%M:%S"),
                    "message": safe[:800],
                    "major": self._is_major_log(safe),
                })
                job["logs"] = job["logs"][-1000:]
                job["updated_at"] = time.time()
        self._append_backend_log(job_id, "DETAIL", safe)

    def get(self, job_id: str, public: bool = False) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            snapshot = json.loads(json.dumps(job, ensure_ascii=False)) if job else None
        if snapshot and public:
            snapshot["logs"] = [item for item in snapshot.get("logs") or [] if item.get("major")]
        return snapshot

    def cancel(self, job_id: str) -> bool:
        with self.condition:
            if job_id not in self.jobs:
                return False
            job = self.jobs[job_id]
            job["cancel"] = True
            if job.get("status") == "queued" and not job.get("dispatched"):
                self.pending = deque((jid, opts) for jid, opts in self.pending if jid != job_id)
                job.update(
                    status="cancelled", percent=100, text="任务已停止",
                    error="任务已停止", queue_position=0,
                )
                self._refresh_queue_locked()
                self._append_backend_log(job_id, "STATUS", "status=cancelled percent=100 text=任务已停止")
            else:
                # Report the terminal state at once.  Cooperative checks in
                # the worker stop the remaining stages at the next boundary.
                job.update(
                    status="cancelled", percent=100, text="任务已停止",
                    error="任务已停止", queue_position=0,
                )
                self._append_backend_log(job_id, "STATUS", "status=cancelled percent=100 text=任务已停止")
            job["updated_at"] = time.time()
            self.condition.notify_all()
            return True

    def cancelled(self, job_id: str) -> bool:
        with self.lock:
            return bool((self.jobs.get(job_id) or {}).get("cancel"))

    def ensure_not_cancelled(self, job_id: str) -> None:
        if self.cancelled(job_id):
            raise InterruptedError("任务已停止")

    def _run(self, job_id: str, options: dict):
        account_lock = checkout_token_lock(str(options.get("token_lease_key") or options.get("access_token") or ""))
        if not account_lock.acquire(blocking=False):
            message = "同一账号已有提链任务正在运行；并发创建 Checkout 会让旧 Session 失效"
            self.log(job_id, f"错误：RuntimeError: {message}")
            self.update(job_id, status="error", percent=100, text="任务失败", error=message)
            return
        try:
            self._run_locked(job_id, options)
        finally:
            account_lock.release()

    def _run_locked(self, job_id: str, options: dict):
        max_attempts = min(50, max(1, int(options.get("retry_count") or 1)))
        used_pairs: set[tuple[str, str]] = set()
        last_error = ""
        paypal_force_de_fallback = False
        for attempt in range(1, max_attempts + 1):
            if self.cancelled(job_id):
                self.update(job_id, status="cancelled", percent=100, text="任务已停止", error="任务已停止")
                return
            current = dict(options)
            current["retry_wrapper"] = True
            entry_pool = current["entry_proxies"]
            exit_pool = current.get("exit_proxies") or entry_pool
            provider = str(current.get("link_type") or "hosted")
            entry_target_country, exit_target_country, single_chain = checkout_proxy_route(current)
            target_proxy_country = entry_target_country
            try:
                lease = LEASES.get(current["token_lease_key"], provider, target_proxy_country) if single_chain else None
                if lease:
                    try:
                        entry_probe = OPTIMIZER.probe(
                            str(lease["proxy_url"]),
                            expected_country=entry_target_country,
                            expires_at=float(lease.get("expires_at") or 0),
                            force=attempt == 1,
                        )
                    except Exception as exc:
                        LEASES.invalidate(current["token_lease_key"], str(exc))
                        lease = None
                    if lease:
                        same_ip = not lease.get("exit_ip") or entry_probe.exit_ip == lease.get("exit_ip")
                        if not entry_probe.ok or (target_proxy_country and entry_probe.country != target_proxy_country) or not same_ip:
                            LEASES.invalidate(current["token_lease_key"], entry_probe.error or "proxy_ip_drift")
                            lease = None
                    if lease:
                        LEASES.touch(current["token_lease_key"], entry_probe)
                        self.log(
                            job_id,
                            f"AT 粘性租约复用：{provider.upper()} / {entry_probe.country} / "
                            f"{entry_probe.exit_ip}，剩余 {max(0, int((entry_probe.expires_at - time.time()) / 60))} 分钟",
                        )
                if not lease:
                    entry_probe = OPTIMIZER.select(
                        entry_pool,
                        role="入口",
                        provider=provider,
                        expected_country=entry_target_country,
                        log=lambda message: self.log(job_id, message),
                    )
                    if single_chain:
                        lease = LEASES.put(
                            current["token_lease_key"], provider, target_proxy_country, entry_probe,
                        )
                        self.log(
                            job_id,
                            f"AT 粘性租约已建立：{provider.upper()} / {entry_probe.country} / "
                            f"{entry_probe.exit_ip}，有效期 "
                            f"{max(1, int((float(lease['expires_at']) - time.time()) / 60))} 分钟",
                        )
                if single_chain:
                    exit_probe = entry_probe
                else:
                    exit_probe = OPTIMIZER.select(
                        exit_pool,
                        role="支付",
                        provider=provider,
                        expected_country=exit_target_country,
                        log=lambda message: self.log(job_id, message),
                    )
            except Exception as exc:
                message = f"IP 池深度选择失败：{type(exc).__name__}: {exc}"
                self.log(job_id, message)
                if attempt < max_attempts:
                    self.update(
                        job_id, status="running", percent=4,
                        text=f"第 {attempt}/{max_attempts} 批 IP 未通过，正在生成下一批",
                        error=message[:1200],
                    )
                    time.sleep(min(3, 0.75 + attempt * 0.25))
                    continue
                self.update(job_id, status="error", percent=100, text="任务失败", error=message[:1200])
                return
            cache_proxy_probe(entry_probe)
            cache_proxy_probe(exit_probe)
            pair = (entry_probe.proxy_url, exit_probe.proxy_url)
            used_pairs.add(pair)
            current["fixed_entry_proxy"], current["fixed_exit_proxy"] = pair
            current["entry_proxy_probe"] = entry_probe
            current["exit_proxy_probe"] = exit_probe
            current["entry_proxy_pool_size"] = len(entry_pool)
            current["exit_proxy_pool_size"] = len(exit_pool)
            # Downstream region checks must use the concrete sticky sessions,
            # never the dynamic template that produced them.
            current["entry_proxies"] = [entry_probe.proxy_url]
            current["exit_proxies"] = [exit_probe.proxy_url]
            if current.get("link_type") == "paypal":
                current["force_paypal_de_fallback"] = paypal_force_de_fallback
                # Strategy A creates the Checkout with the campaign already
                # attached.  This preserves the merchant's native zero-due
                # PayPal SetupIntent configuration.  Strategy B keeps the
                # existing cross-entry checkout/update flow as a fallback.
                current["promo_on_create"] = bool((attempt - 1) % 2 == 0)
            if current.get("link_type") in {"pix", "upi"}:
                # Keep PIX's adaptive shapes; UPI uses one native Hosted shape.
                if current.get("link_type") == "pix":
                    strategy_cycle = ("standalone", "late_promo", "inline")
                    current["promo_on_create"] = False
                else:
                    # A zero-due UPI SetupIntent must be configured by the
                    # merchant when Checkout is created. Eligibility is checked
                    # before this native-promo strategy is allowed to run.
                    strategy_cycle = ("hosted_minimal",)
                    current["promo_on_create"] = True
                    current["upi_create_on_promo_entry"] = False
                current["local_method_strategy"] = strategy_cycle[(attempt - 1) % len(strategy_cycle)]
            if current.get("link_type") == "pix" and current.get("pix_tax_id_auto"):
                auto_kind = current.get("pix_auto_kind") or "cpf"
                kind = ("cpf" if attempt % 2 else "cnpj") if auto_kind == "mixed" else auto_kind
                current["pix_tax_id"] = generate_cnpj() if kind == "cnpj" else generate_cpf()
                current["pix_identity"] = generate_pix_identity(kind)
            self.update(
                job_id, status="running", percent=4,
                text=f"第 {attempt}/{max_attempts} 次尝试：正在准备任务",
                error="",
            )
            self.log(job_id, f"========== 提链尝试 {attempt}/{max_attempts} ==========")
            if current.get("link_type") == "paypal" and current.get("use_promo"):
                strategy = "Checkout 创建时原生带优惠" if current.get("promo_on_create") else "创建后通过入口线路更新优惠"
                self.log(job_id, f"PayPal 优惠策略：{strategy}")
            if current.get("link_type") == "upi" and current.get("use_promo"):
                strategy = (
                    "IN 支付 Session 创建时原生带优惠"
                    if current.get("promo_on_create")
                    else "创建后 JP 入口更新优惠"
                )
                self.log(
                    job_id,
                    f"UPI 0 元 AutoPay 策略：{strategy}；提交形态={current.get('local_method_strategy')}；"
                    f"create_on_jp={bool(current.get('upi_create_on_promo_entry'))}",
                )
            self._run_single(job_id, current)
            state = self.get(job_id) or {}
            if state.get("status") in {"done", "cancelled"}:
                if state.get("status") == "done":
                    OPTIMIZER.report(pair, success=True)
                    if single_chain:
                        LEASES.touch(current["token_lease_key"], entry_probe)
                if state.get("status") == "done" and isinstance(state.get("result"), dict):
                    result = state["result"]
                    result["attempt"] = attempt
                    result["max_attempts"] = max_attempts
                    self.update(job_id, result=result)
                    self._record_success(job_id, result)
                return
            last_error = str(state.get("error") or "")
            lowered = last_error.lower()
            non_retryable = any(marker in lowered for marker in (
                "access token", "token_invalidated", "token_expired", "token_revoked", "jwt expired",
                "计划类型", "提取方式", "任务已停止", "upi_promo_not_eligible",
            ))
            if not non_retryable:
                OPTIMIZER.report(pair, success=False, error=last_error)
            if non_retryable and single_chain:
                LEASES.invalidate(current["token_lease_key"], last_error)
            network_failure = any(marker in lowered for marker in (
                "timeout", "timed out", "proxy", "connection", "network", "curl", "tls", "ssl",
                "cloudflare", "chatgpt_403", "service unavailable", "bad gateway",
            ))
            if network_failure and single_chain:
                LEASES.invalidate(current["token_lease_key"], last_error)
                self.log(job_id, "当前 AT 的粘性代理网络异常，下一轮将生成新的动态 Session")
            if non_retryable or attempt >= max_attempts:
                self.update(job_id, status="error", percent=100, text="任务失败", error=last_error[:1200])
                return
            if (
                current.get("link_type") == "paypal"
                and not paypal_force_de_fallback
                and ("\u672a\u5f00\u653e paypal" in lowered or "\u672a\u5f00\u653epaypal" in lowered)
                and str(current.get("checkout_country") or current.get("country") or "").upper() != "DE"
            ):
                paypal_force_de_fallback = True
                self.log(job_id, "\u5f53\u524d\u56fd\u5bb6 Checkout \u672a\u8fd4\u56de PayPal\uff1b\u540e\u7eed\u5c1d\u8bd5\u81ea\u52a8\u5207\u6362\u5fb7\u56fd DE/EUR \u8d26\u5355")
            self.log(job_id, f"第 {attempt}/{max_attempts} 轮未命中：{last_error[:260] or '上游未返回可用链接'}")
            if options.get("link_type") == "pix":
                self.log(job_id, "正在更换代理与 PIX 资料后重新尝试")
            else:
                self.log(job_id, "正在更换代理后重新尝试")
            time.sleep(min(4, 1 + attempt * 0.35))

    def _run_single(self, job_id: str, options: dict):
        network: AttemptNetworkContext | None = None
        try:
            self.update(job_id, status="running", percent=6, text="解析 Access Token")
            token = str(options["access_token"])
            meta = dict(options.get("token_meta") or {})
            self.ensure_not_cancelled(job_id)
            provider = options["link_type"]
            country = options["country"]
            entry_pool = options["entry_proxies"]
            _entry_country, _payment_country, single_chain = checkout_proxy_route(options)
            exit_pool = entry_pool if single_chain else (options.get("exit_proxies") or entry_pool)
            entry_proxy = options.get("fixed_entry_proxy") or secrets.choice(entry_pool)
            exit_proxy = entry_proxy if single_chain else (options.get("fixed_exit_proxy") or secrets.choice(exit_pool))
            payment_geo: dict[str, str] = {}
            if provider == "hosted":
                self.log(job_id, f"代理池共 {len(entry_pool)} 条，本次已自动选择 1 条")
            elif single_chain:
                self.log(job_id, f"代理池 1 共 {len(entry_pool)} 条，本次全流程固定使用 1 条真实 Session")
            else:
                self.log(job_id, f"代理池 1 共 {len(entry_pool)} 条，代理池 2 共 {len(exit_pool)} 条，本次已分别自动选择")
            # Every outer retry creates a brand-new Checkout, so it must also
            # use a fresh browser/device identity.  Within this single attempt
            # the same ids are kept for create -> update -> approve.
            device_id, did = str(uuid.uuid4()), str(uuid.uuid4())
            network = AttemptNetworkContext(entry_proxy, exit_proxy, device_id, did)
            entry_http = network.http(entry_proxy)
            exit_http = network.http(exit_proxy)

            if provider == "pix":
                self.update(job_id, percent=9, text="第 1/7 步：选择并检测代理")
                main_country, main_region = proxy_country(entry_proxy)
                stripe_country, stripe_region = proxy_country(exit_proxy)
                self.log(job_id, f"PIX 代理校验：代理池 1={main_country}/{main_region}")
                if main_country != "BR" or stripe_country != "BR":
                    self.log(
                        job_id,
                        f"PIX 当前代理为 {main_country or '?'} + {stripe_country or '?'}；不限制国家，继续由上游判断支付方式",
                    )
                self.ensure_not_cancelled(job_id)

            promo_requested = options["plan"] == "plus" and options.get("use_promo", False)
            if provider == "paypal":
                self.update(job_id, percent=9, text="第 1/7 步：校验 PayPal 优惠识别代理与支付代理")
                main_country, main_region = proxy_country(entry_proxy)
                exit_proxy, payment_geo, rejected_countries = select_paypal_exit_proxy(
                    exit_proxy,
                    exit_pool,
                    scan_limit=int(os.getenv("PAYPAL_PROXY_SCAN_LIMIT", "24") or 24),
                )
                payment_country = payment_geo.get("country") or ""
                payment_region = payment_geo.get("region") or ""
                if not payment_country:
                    raise RuntimeError("代理池 2 未检测到国家地区")
                if rejected_countries:
                    self.log(job_id, f"PayPal 已跳过不兼容地区：{'/'.join(rejected_countries[:8])}")
                detected_currency = str(payment_geo.get("currency") or "").upper()
                if options.get("force_paypal_de_fallback"):
                    checkout_country, checkout_currency, currency_source = (
                        "DE", "EUR", f"\u5f53\u524d\u56fd\u5bb6 {payment_country} \u5b9e\u6d4b\u672a\u5f00\u653e PayPal\uff0c\u4f7f\u7528 DE/EUR \u56de\u9000",
                    )
                else:
                    checkout_country, checkout_currency, currency_source = normalize_paypal_checkout_region(
                        payment_country, detected_currency,
                    )
                country = checkout_country
                options["country"] = checkout_country
                options["currency"] = checkout_currency
                options["checkout_country"] = checkout_country
                options["checkout_currency"] = checkout_currency
                options["payment_proxy_country"] = payment_country
                self.log(
                    job_id,
                    f"PayPal 代理池 2 地区：{payment_country}/{payment_region}；"
                    f"Checkout={checkout_country}/{checkout_currency}（{currency_source}）",
                )
                if promo_requested and main_country not in {"TR", "JP"}:
                    self.log(job_id, f"PayPal 优惠识别代理当前为 {main_country or '?'}；不限制国家，继续尝试")
                self.ensure_not_cancelled(job_id)
            if provider == "upi":
                self.update(job_id, percent=9, text="第 1/7 步：校验 UPI 印度粘性代理")
                main_country, main_region = proxy_country(entry_proxy)
                payment_country, payment_region = proxy_country(exit_proxy)
                self.log(
                    job_id,
                    f"UPI 双地区校验：优惠入口={main_country}/{main_region}，"
                    f"支付出口={payment_country}/{payment_region}，账单=IN/INR",
                )
                if promo_requested and main_country not in {"TR", "JP"}:
                    self.log(job_id, f"UPI 优惠识别代理当前为 {main_country or '?'}；不限制国家，继续尝试")
                if payment_country != "IN":
                    self.log(job_id, f"UPI 支付代理当前为 {payment_country or '?'}；不限制国家，继续由上游判断支付方式")
                self.ensure_not_cancelled(job_id)
            if provider == "ideal":
                self.update(job_id, percent=9, text="校验 iDEAL 荷兰支付代理")
                main_country, main_region = proxy_country(entry_proxy)
                payment_country, payment_region = proxy_country(exit_proxy)
                self.log(
                    job_id,
                    f"iDEAL 代理校验：全流程={main_country}/{main_region}，真实 Session 固定，账单=NL/EUR",
                )
                if payment_country != "NL":
                    raise RuntimeError(
                        f"iDEAL 支付代理出口为 {payment_country or '未知'}，需要 NL 荷兰出口"
                    )
                self.ensure_not_cancelled(job_id)
            preflight = {}
            if promo_requested:
                self.update(job_id, percent=12, text="读取入口支付与活动标记")
                preflight = preflight_trial_eligibility(
                    token, meta.get("account_id") or "", entry_proxy, device_id, did,
                    lambda m: self.log(job_id, m),
                    http=entry_http,
                )
                detected_campaign = promo_campaign_from_payload(preflight)
                if preflight.get("one_click_trial_eligible") is True:
                    options["promo_marker_eligible"] = True
                if detected_campaign:
                    options["promo_campaign"] = detected_campaign
                    options["promo_campaign_verified"] = True
                    self.log(job_id, f"优惠预检已匹配账号活动：{detected_campaign}")
                if provider == "upi" and upi_promo_is_explicitly_unavailable(preflight):
                    raise RuntimeError("upi_promo_not_eligible")
                self.ensure_not_cancelled(job_id)

            self.update(job_id, percent=18, text="生成 Sentinel 校验")
            payload = checkout_payload(options, meta)
            if provider == "paypal":
                self.log(job_id, f"计划={options['plan']}，方式=paypal，账单={country}/{options['currency']}，PayPal订单={options.get('checkout_country')}/{options.get('checkout_currency')}")
            else:
                self.log(job_id, f"计划={options['plan']}，方式={provider}，地区={country}/{options['currency']}")
            stage2_text = "第 2/7 步：BR 创建 Checkout（首段不带优惠）" if provider == "pix" else (
                (f"第 2/7 步：使用 {country} 代理创建 PayPal Checkout"
                 + ("（原生携带优惠）" if options.get("promo_on_create") else "（稍后更新优惠）"))
                if provider == "paypal" and promo_requested else (
                    "第 2/7 步：使用 IN 代理创建 UPI Checkout" if provider == "upi" else "创建 OpenAI Checkout"
                )
            )
            self.update(job_id, percent=34, text=stage2_text)
            checkout_proxy = exit_proxy if provider in {"paypal", "upi", "ideal"} else entry_proxy
            if provider == "upi" and promo_requested and options.get("upi_create_on_promo_entry"):
                # JP entry creates the zero-due Checkout; IN Session owns Stripe
                # confirm/approve/QR extraction.
                checkout_proxy = entry_proxy
            if provider == "pix":
                self.log(
                    job_id,
                    "Stage1 Checkout、优惠更新、Stripe 和 approval 使用同一条 BR 代理"
                    + ("；本轮优惠随 Checkout 创建" if options.get("promo_on_create") else ""),
                )
            elif provider == "paypal" and promo_requested:
                self.log(job_id, f"PayPal 设置：代理池 1 用于优惠检查，代理池 2 创建 {country}/{options['currency']} Checkout")
            elif provider == "upi":
                if promo_requested and options.get("upi_create_on_promo_entry"):
                    self.log(
                        job_id,
                        "UPI 动态 IP：JP 入口创建/优惠 Checkout；IN 账单 + IN Session 做 Stripe/提链",
                    )
                else:
                    self.log(job_id, "UPI 设置：优惠检查、IN/INR Checkout 与 Stripe 全部使用同一真实 Session")
            elif provider == "ideal":
                self.log(job_id, "iDEAL 设置：NL/EUR Checkout、优惠更新与 Stripe 全部使用同一真实 Session")
            elif provider != "hosted":
                self.log(job_id, f"Checkout 将使用所选的 {country} 地区代理")
            created = create_checkout(
                token, payload, checkout_proxy, device_id, did, lambda m: self.log(job_id, m),
                http=network.http(checkout_proxy),
            )
            self.ensure_not_cancelled(job_id)
            self.update(job_id, percent=44, text="Checkout 创建完成，正在准备支付方式")
            checkout_data = created["data"]
            chatgpt_http = created["http"]
            stage1_campaign = promo_campaign_from_payload(checkout_data)
            if checkout_data.get("one_click_trial_eligible") is True:
                options["promo_marker_eligible"] = True
            if stage1_campaign:
                options["promo_campaign"] = stage1_campaign
                options["promo_campaign_verified"] = True
                self.log(job_id, f"Checkout 已返回活动标识：{stage1_campaign}")
            if (
                provider == "upi"
                and promo_requested
                and upi_promo_is_explicitly_unavailable(checkout_data)
            ):
                raise RuntimeError("upi_promo_not_eligible")
            provider_chatgpt_http = chatgpt_http
            promo_chatgpt_http = chatgpt_http
            if provider in {"paypal", "upi", "ideal"}:
                promo_chatgpt_http = entry_http
                try:
                    promo_chatgpt_http.cookies.set("oai-did", did, domain="chatgpt.com")
                    for cookie_name, cookie_value in chatgpt_http.cookies.get_dict().items():
                        promo_chatgpt_http.cookies.set(cookie_name, cookie_value, domain="chatgpt.com")
                    promo_chatgpt_http.get("https://chatgpt.com/", headers={"User-Agent": sc.CHROME_UA}, timeout=25)
                except Exception as exc:
                    self.log(job_id, f"{provider.upper()} 优惠线路暖身提示：{type(exc).__name__}")
                if provider == "paypal":
                    self.log(job_id, f"PayPal 支付处理使用代理池 2（{country}）")
                elif provider == "upi":
                    if promo_requested and options.get("upi_create_on_promo_entry"):
                        # Checkout cookies were collected on JP create Session.
                        # Keep approval candidates able to use either Session.
                        provider_chatgpt_http = exit_http
                        try:
                            provider_chatgpt_http.cookies.set("oai-did", did, domain="chatgpt.com")
                            for cookie_name, cookie_value in chatgpt_http.cookies.get_dict().items():
                                provider_chatgpt_http.cookies.set(
                                    cookie_name, cookie_value, domain="chatgpt.com",
                                )
                        except Exception as exc:
                            self.log(job_id, f"UPI IN Session cookie 同步提示：{type(exc).__name__}")
                        self.log(job_id, "UPI Stripe/提链使用 IN Session；优惠/创建在 JP 入口")
                    else:
                        self.log(job_id, "UPI 支付处理继续复用同一 IN Session")
                else:
                    self.log(job_id, "iDEAL 优惠更新与 Stripe 继续复用同一 NL Session")
            session_id = checkout_data.get("checkout_session_id") or ""
            if not session_id and provider != "hosted":
                raise RuntimeError("Checkout 未返回 Stripe Session ID")
            if self.cancelled(job_id):
                raise InterruptedError("任务已停止")

            result: dict[str, Any] = {
                "plan": options["plan"],
                "link_type": provider,
                "checkout_session_id": session_id,
                "checkout_url": checkout_data.get("checkout_url") or "",
                "account_email": meta.get("email") or "",
                "account_id": meta.get("account_id") or "",
                "country": country,
                "currency": options["currency"],
                "checkout_country": options.get("checkout_country") or country,
                "checkout_currency": options.get("checkout_currency") or options["currency"],
                "entry_proxy_pool_size": int(options.get("entry_proxy_pool_size") or len(entry_pool)),
                "exit_proxy_pool_size": int(options.get("exit_proxy_pool_size") or len(exit_pool)) if not single_chain else 0,
                "proxy_mode": "dual_region" if not single_chain else "single_chain",
                "entry_exit_ip": getattr(options.get("entry_proxy_probe"), "exit_ip", ""),
                "entry_proxy_score": getattr(options.get("entry_proxy_probe"), "score", None),
                "payment_exit_ip": getattr(options.get("exit_proxy_probe"), "exit_ip", ""),
                "payment_proxy_score": getattr(options.get("exit_proxy_probe"), "score", None),
                "network_fingerprint": "chrome136/sticky-session",
                "promo_requested": promo_requested,
                "promo_applied": None,
                "promo_campaign_used": options.get("promo_campaign") or "plus-1-month-free",
                "entry_trial_eligible": preflight.get("one_click_trial_eligible"),
                "checkout_trial_eligible": checkout_data.get("one_click_trial_eligible"),
                "entry_one_click_marker": preflight.get("one_click_trial_eligible"),
                "checkout_one_click_marker": checkout_data.get("one_click_trial_eligible"),
                "promotion_eligibility_decided_by": "checkout_approve",
                "entry_country": str(locals().get("main_country") or "").upper(),
                "payment_proxy_country": str(options.get("payment_proxy_country") or locals().get("payment_country") or "").upper(),
            }
            if promo_requested:
                checkout_trial = checkout_data.get("one_click_trial_eligible")
                self.log(
                    job_id,
                    "支付标记（仅供诊断）：入口 one_click={}，Stage1 one_click={}".format(
                        preflight.get("one_click_trial_eligible"), checkout_trial
                    ),
                )
                if checkout_trial is False:
                    self.log(
                        job_id,
                        "Stage1 one_click 标记为 false；该字段不代表活动资格，继续以金额与 approval 结果判定",
                    )
            if provider == "hosted":
                self.update(job_id, percent=56, text="正在检测官方长链金额")
                if not session_id:
                    if promo_requested:
                        raise RuntimeError("官方长链未返回 Stripe Session ID，优惠金额校验失败")
                    self.update(job_id, percent=100, text="支付长链生成完成", status="done", result=result)
                    return

                hosted_stripe_http = entry_http
                hosted_profile = sc._profile(country)
                hosted_pk = str(checkout_data.get("publishable_key") or "") or sc.verify_pk(
                    hosted_stripe_http, session_id, lambda m: self.log(job_id, m)
                )
                hosted_init, hosted_version, hosted_ctx = sc.init_checkout(
                    hosted_stripe_http, session_id, hosted_pk, hosted_profile, lambda m: self.log(job_id, m)
                )
                hosted_processor = (
                    str(checkout_data.get("processor_entity") or "")
                    or sc._entity_from_return_url(hosted_ctx.get("return_url") or hosted_init.get("return_url") or "")
                    or "openai_llc"
                )
                hosted_amount = hosted_ctx.get("checkout_amount")
                try:
                    hosted_zero = int(str(hosted_amount)) == 0
                except (TypeError, ValueError):
                    hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}

                if promo_requested and not hosted_zero:
                    self.update(job_id, percent=68, text="正在应用优惠并同步金额")
                    update_checkout_promo(
                        chatgpt_http,
                        token,
                        session_id,
                        hosted_processor,
                        options.get("promo_campaign") or "plus-1-month-free",
                        lambda m: self.log(job_id, m),
                        device_id=device_id,
                    )
                    for sync_attempt in range(6):
                        time.sleep(1.5 if sync_attempt else 0.8)
                        hosted_init, hosted_version, hosted_ctx = sc.init_checkout(
                            hosted_stripe_http, session_id, hosted_pk, hosted_profile, lambda m: self.log(job_id, m)
                        )
                        hosted_amount = hosted_ctx.get("checkout_amount")
                        self.log(job_id, f"官方长链优惠同步检查 {sync_attempt + 1}/6：amount={hosted_amount}")
                        try:
                            hosted_zero = int(str(hosted_amount)) == 0
                        except (TypeError, ValueError):
                            hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}
                        if hosted_zero:
                            break

                hosted_billing = default_billing(country, meta.get("email") or "")
                sc.update_tax_region(
                    hosted_stripe_http,
                    session_id,
                    hosted_pk,
                    hosted_version,
                    hosted_ctx,
                    hosted_billing,
                    hosted_profile,
                    lambda m: self.log(job_id, m),
                )
                hosted_amount = hosted_ctx.get("checkout_amount")
                try:
                    hosted_zero = int(str(hosted_amount)) == 0
                except (TypeError, ValueError):
                    hosted_zero = str(hosted_amount).strip() in {"0", "0.0", "0.00"}
                result.update({
                    "checkout_amount": hosted_amount,
                    "promo_applied": hosted_zero if promo_requested else None,
                    "payment_method_types": hosted_ctx.get("payment_method_types") or [],
                    "processor_entity": hosted_processor,
                    "stripe_publishable_key": hosted_pk,
                })
                if promo_requested and not hosted_zero:
                    raise RuntimeError(f"官方长链优惠未生效：Stripe 今日应付 amount={hosted_amount}")
                if promo_requested:
                    self.log(job_id, "官方长链金额校验通过：Stripe 今日应付 amount=0")
                else:
                    self.log(job_id, f"官方长链金额检测完成：Stripe 今日应付 amount={hosted_amount}")
                self.update(job_id, percent=100, text="支付长链生成完成", status="done", result=result)
                return

            stage3_text = "第 3/7 步：正在初始化 PIX" if provider == "pix" else (
                "第 3/7 步：正在初始化 PayPal" if provider == "paypal" and promo_requested else f"正在初始化 {provider.upper()}"
            )
            self.update(job_id, percent=56, text=stage3_text)
            billing_geo = None
            if provider == "paypal" and str(options.get("payment_proxy_country") or "").upper() == country:
                billing_geo = payment_geo
            billing = default_billing(
                country,
                meta.get("email") or "",
                options.get("pix_tax_id") or "",
                billing_geo,
                real_random=(provider == "paypal"),
            )
            if provider == "paypal":
                selected_address = billing.get("address") or {}
                self.log(
                    job_id,
                    "PayPal 本轮随机真实账单：source={}，城市={}，邮编={}，地点={}".format(
                        billing.get("_address_source") or "unknown",
                        selected_address.get("city") or "-",
                        selected_address.get("postal_code") or "-",
                        billing.get("_place_name") or "公开场所",
                    ),
                )
            paypal_payment_billing = None
            if provider == "paypal":
                paypal_country = str(options.get("payment_proxy_country") or country).upper()
                if paypal_country != country:
                    paypal_payment_billing = default_billing(
                        paypal_country,
                        meta.get("email") or "",
                        geo=payment_geo,
                        real_random=True,
                    )
                    paypal_address = paypal_payment_billing.get("address") or {}
                    self.log(
                        job_id,
                        f"PayPal separated billing: OpenAI={country}/{options.get('currency')}, "
                        f"PayPal={paypal_country}, city={paypal_address.get('city') or '-'}, "
                        f"postal={paypal_address.get('postal_code') or '-'}",
                    )
            promotion_billing = None
            if provider == "paypal" and promo_requested:
                promotion_country = str(main_country or "BR").upper()
                promotion_billing = default_billing(
                    promotion_country,
                    meta.get("email") or "",
                )
                self.log(
                    job_id,
                    f"PayPal 地区：优惠更新={promotion_country}，Stripe/PayPal 账单与 merchant 快照={country}",
                )
            if provider == "pix":
                identity = options.get("pix_identity") or {}
                if identity:
                    billing["name"] = identity.get("name") or billing.get("name")
                    billing["email"] = identity.get("email") or billing.get("email")
                    address = billing.setdefault("address", {})
                    for key in ("line1", "city", "state", "postal_code"):
                        if identity.get(key):
                            address[key] = identity[key]
                    if identity.get("source") == "brasilapi_cnpj":
                        self.log(job_id, f"PIX 已匹配 CNPJ 登记主体：{billing.get('name')} / {address.get('state')}")
                    elif str(identity.get("source") or "").startswith("generated_"):
                        generated_kind = str(identity.get("source")).removeprefix("generated_").upper()
                        self.log(job_id, f"PIX 本轮已自动生成 {generated_kind}、持有人/企业名称及巴西地址")
            stripe_http = exit_http

            progress_mark = 62

            def advance_progress(percent: int, text: str):
                nonlocal progress_mark
                self.ensure_not_cancelled(job_id)
                if percent > progress_mark:
                    progress_mark = percent
                    self.update(job_id, percent=percent, text=text)

            def provider_log(message: str):
                self.log(job_id, message)
                lowered_message = message.lower()
                if "init ok" in lowered_message:
                    advance_progress(64, "支付方式初始化完成")
                elif "checkout/update" in lowered_message or "优惠更新完成" in message:
                    advance_progress(72, "优惠已应用，正在确认金额")
                elif "tax_region" in lowered_message:
                    advance_progress(78, "金额确认完成，正在提交账单信息")
                elif "snapshot billing" in lowered_message:
                    advance_progress(84, "账单信息已提交")
                elif "payment_method" in lowered_message:
                    advance_progress(88, "支付方式已创建")
                elif "manual_approval" in lowered_message or "approve:" in lowered_message:
                    advance_progress(92, "正在确认支付请求")
                elif "poll" in lowered_message:
                    advance_progress(96, "正在获取最终结果")

            def approve_cb(processor: str):
                self.ensure_not_cancelled(job_id)
                advance_progress(90, "正在确认支付请求")
                # Approval stays on the payment/creation Sessions. The JP promo
                # Session never owns the Stripe submission and is not a valid
                # fingerprint match for approval.
                candidates = approval_session_candidates(
                    provider,
                    promo_requested,
                    single_chain,
                    exit_proxy=exit_proxy,
                    checkout_proxy=checkout_proxy,
                    checkout_http=chatgpt_http,
                    provider_http=provider_chatgpt_http,
                )
                seen: set[tuple[str, int]] = set()
                last_payload: dict = {}
                for approval_proxy, approval_http, label in candidates:
                    key = (str(approval_proxy or ""), id(approval_http))
                    if key in seen:
                        continue
                    seen.add(key)
                    self.log(job_id, f"提交 Checkout approval（{label}）")
                    last_payload = approve_checkout(
                        token,
                        session_id,
                        processor,
                        approval_proxy,
                        device_id,
                        did,
                        http=approval_http,
                        log=provider_log,
                    )
                    result = str((last_payload or {}).get("result") or "").lower()
                    soft = str((last_payload or {}).get("_approve_soft_fail") or "").lower()
                    if result in {"", "approved"} and not soft:
                        self.log(job_id, f"Checkout approval 已通过（{label}）")
                        break
                    if soft == "blocked" and label != candidates[-1][2]:
                        self.log(job_id, f"Checkout approval {soft}（{label}），切换下一 Session 重试")
                        continue
                    if soft == "exception":
                        self.log(job_id, f"Checkout approval exception（{label}），交 Stripe poll 确认")
                        break
                self.ensure_not_cancelled(job_id)
                return last_payload

            def apply_promo_cb(processor: str):
                self.ensure_not_cancelled(job_id)
                if provider == "pix":
                    self.log(job_id, "第 4/7 步：初始化已确认 PIX，开始应用优惠")
                elif provider == "paypal":
                    self.log(job_id, "PayPal 已确认可用，正在应用优惠")
                elif provider == "upi":
                    self.log(job_id, "UPI 已确认可用，正在应用优惠")
                elif provider == "ideal":
                    self.log(job_id, "iDEAL 已确认可用，正在通过代理池 1 提交优惠；最终以 Stripe 今日应付金额为准")
                advance_progress(70, "正在应用优惠")
                campaign = options.get("promo_campaign") or "plus-1-month-free"
                response = update_checkout_promo(
                    promo_chatgpt_http,
                    token,
                    session_id,
                    processor,
                    campaign,
                    provider_log,
                    device_id=device_id,
                )
                self.ensure_not_cancelled(job_id)
                return response

            self.update(job_id, percent=62, text="正在生成支付结果")
            provider_result = stripe_to_provider(
                stripe_http,
                session_id,
                provider,
                billing=billing,
                promotion_billing=promotion_billing,
                payment_billing=paypal_payment_billing,
                payment_http=stripe_http if paypal_payment_billing else None,
                country=options.get("checkout_country") or country,
                chatgpt_http=provider_chatgpt_http,
                access_token=token,
                stage1=checkout_data,
                # PayPal 保持原协议的 Bearer approval；PIX/UPI 才使用带
                # Sentinel 的 callback。PayPal approval 返回 approved 后仍
                # 卡住时，额外 Sentinel 上下文会让批准结果与 Stripe
                # submission 不同步。
                approve_callback=None if provider == "paypal" else approve_cb,
                apply_promo_callback=apply_promo_cb if provider in {"pix", "paypal", "upi", "ideal"} and promo_requested else None,
                ideal_bank=options.get("ideal_bank", ""),
                require_zero_due=promo_requested,
                local_method_strategy=options.get("local_method_strategy") or "standalone",
                log=provider_log,
            )
            validate_provider_result(provider, provider_result)
            self.ensure_not_cancelled(job_id)
            self.update(job_id, percent=98, text="结果已生成，正在整理页面")
            result.update(provider_result)
            # Display the currency Stripe actually returned instead of only
            # echoing the requested currency.  This also makes automatic
            # proxy-region adaptation observable in the result panel/API.
            if provider_result.get("checkout_currency"):
                result["currency"] = str(provider_result["checkout_currency"]).upper()
                result["checkout_currency"] = result["currency"]
            if provider == "upi":
                mandate_source = str(provider_result.get("upi_mandate_source") or "")
                if mandate_source:
                    self.log(
                        job_id,
                        f"UPI AutoPay mandate 来源={mandate_source}，"
                        f"available={bool(provider_result.get('upi_mandate_available'))}",
                    )
            done_text = "第 7/7 步：PIX 二维码生成完成" if provider == "pix" else (
                "第 7/7 步：PayPal agreements/approve 链接生成完成" if provider == "paypal" else f"{provider.upper()} 提取完成"
            )
            self.update(job_id, percent=100, text=done_text, status="done", result=result)
        except InterruptedError as exc:
            self.update(job_id, status="cancelled", percent=100, text=str(exc), error=str(exc))
        except Exception as exc:
            raw_error = str(exc)
            error_text = raw_error
            lowered = raw_error.lower()
            if "token_invalidated" in lowered or "authentication token has been invalidated" in lowered:
                error_text = "Access Token 已失效，请重新登录 ChatGPT 获取新的 Session JSON 或 AT。"
            elif "token_expired" in lowered or "jwt expired" in lowered:
                error_text = "Access Token 已过期，请重新登录 ChatGPT 获取新的 Session JSON 或 AT。"
            elif "upi_promo_not_eligible" in lowered:
                error_text = (
                    "当前账号未返回可用的 Plus 优惠活动，不能创建零金额 UPI mandate。"
                    "（upi_promo_not_eligible）"
                )
            elif "not_eligible" in lowered:
                error_text = "当前账号未开放所选套餐或支付通道。"
            elif "cannot combine currencies" in lowered:
                error_text = "该账号已有其他币种的活跃结账会话，请等待原会话释放，或更换账号后再生成当前币种链接。"
            elif "amount_too_small" in lowered:
                error_text = "当前地区换算后的结账金额低于支付提供商下限，请提高 Codex 积分数量后重试。"
            self.log(job_id, f"错误：{type(exc).__name__}: {error_text}")
            if options.get("retry_wrapper"):
                self.update(job_id, status="running", percent=8, text="本次未成功，正在更换代理重试", error=error_text[:1200])
            else:
                self.update(job_id, status="error", percent=100, text="任务失败", error=error_text[:1200])
        finally:
            if network is not None:
                network.close()


class IpTaskLimiter:
    def __init__(self, limit: int = 3, window_seconds: int = 60):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self.lock = threading.RLock()
        self.events: defaultdict[str, deque[float]] = defaultdict(deque)

    def acquire(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        with self.lock:
            bucket = self.events[ip]
            while bucket and now - bucket[0] >= self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0]) + 0.999))
                return False, retry_after
            bucket.append(now)
            if len(self.events) > 10000:
                stale = [key for key, values in self.events.items() if not values or now - values[-1] > self.window_seconds * 2]
                for key in stale[:2000]:
                    self.events.pop(key, None)
            return True, 0


def request_client_ip() -> str:
    remote = str(request.remote_addr or "").strip()
    if remote in {"127.0.0.1", "::1"}:
        return str(request.headers.get("X-Real-IP") or remote).strip()
    return remote or "unknown"


STORE = JobStore()
IP_TASK_LIMITER = IpTaskLimiter(
    limit=int(os.getenv("PAY153_IP_RPM", "3")),
    window_seconds=60,
)


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "pay153", "time": int(time.time())})


@app.get("/api/config")
def config():
    return jsonify({
        "plans": list(PLANS),
        "link_types": ["hosted", "paypal", "ideal", "upi", "pix"],
        "country_currency": COUNTRY_CURRENCY,
        "provider_defaults": PROVIDER_DEFAULTS,
        "proxy_policy": {
            "entry_required": False,
            "exit_required_for": [],
            "managed_gateway": bool(CONFIGURED_PROXY_GATEWAY),
            "single_chain_for": ["hosted", "ideal", "pix", "upi_without_promo"],
            "dual_region_for": {"upi_promo": {"entry": "JP", "payment": "IN"}},
            "max_per_pool": 500,
            "selection": "deep_probe_sticky_session",
            "lease_minutes": LEASES.lease_seconds // 60,
            "dynamic_country_routes": PROVIDER_PROXY_COUNTRIES,
        },
        "retry_policy": {"min": 1, "max": 50, "default_pix": 10, "default_other": 3},
        "pix_identity_policy": {"default": "cpf", "auto_kinds": ["cpf", "mixed", "cnpj"], "regenerate_each_attempt": True},
        "task_limits": {
            "global_rpm": STORE.global_rpm,
            "per_ip_rpm": IP_TASK_LIMITER.limit,
            "queue_enabled": True,
            "workers": STORE.worker_limit,
        },
    })


@app.get("/api/proxy-leases")
def proxy_leases():
    return jsonify({"ok": True, "leases": LEASES.public_records()})


@app.post("/api/checkout")
def start_checkout():
    data = request.get_json(silent=True) or {}
    plan = str(data.get("plan") or "plus").lower()
    link_type = str(data.get("link_type") or "hosted").lower()
    if plan not in PLANS:
        return jsonify({"error": "计划类型不正确"}), 400
    if link_type not in {"hosted", "paypal", "ideal", "upi", "pix"}:
        return jsonify({"error": "提取方式不正确"}), 400
    defaults = PROVIDER_DEFAULTS.get(link_type, {})
    country = str(data.get("country") or defaults.get("country") or "US").upper()
    requested_currency = str(data.get("currency") or defaults.get("currency") or COUNTRY_CURRENCY.get(country, "USD")).upper()
    if link_type in {"ideal", "upi", "pix"}:
        country = str(defaults.get("country") or country).upper()
        requested_currency = str(defaults.get("currency") or COUNTRY_CURRENCY.get(country, "USD")).upper()
    currency, _currency_source = normalize_checkout_currency(country, requested_currency)
    entry_raw = data.get("entry_proxies")
    if entry_raw is None:
        entry_raw = data.get("entry_proxy") or data.get("api_proxy") or data.get("proxy") or CONFIGURED_PROXY_GATEWAY
    exit_raw = data.get("exit_proxies")
    if exit_raw is None:
        exit_raw = data.get("exit_proxy") or data.get("payment_proxy") or ""
    promo_requested = plan == "plus" and bool(data.get("use_promo", True))
    dual_region_upi = link_type == "upi" and promo_requested
    if link_type == "paypal" and not exit_raw:
        exit_raw = CONFIGURED_PROXY_GATEWAY or entry_raw
    if dual_region_upi and not exit_raw:
        exit_raw = CONFIGURED_PROXY_GATEWAY or entry_raw
    if not entry_raw:
        return jsonify({"error": "服务器尚未配置动态代理网关"}), 503
    if link_type == "paypal" and not exit_raw:
        return jsonify({"error": "服务器尚未配置 PayPal 支付代理网关"}), 503
    try:
        entry_proxies = normalize_proxy_pool(entry_raw, "入口代理")
        exit_proxies = (
            normalize_proxy_pool(exit_raw, "出口代理")
            if exit_raw and (link_type == "paypal" or dual_region_upi)
            else []
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not entry_proxies:
        return jsonify({"error": "入口代理至少填写 1 条"}), 400
    if link_type == "paypal" and not exit_proxies:
        return jsonify({"error": "出口代理至少填写 1 条"}), 400
    raw_pix_tax_id = re.sub(r"\D", "", str(data.get("pix_tax_id") or ""))[:14] if link_type == "pix" else ""
    try:
        retry_count = min(50, max(1, int(data.get("retry_count") or (10 if link_type == "pix" else 3))))
    except (TypeError, ValueError):
        return jsonify({"error": "重试次数需要填写 1-50 的整数"}), 400
    pix_identity: dict[str, str] = {}
    if link_type == "pix":
        manual_identity = {
            "name": str(data.get("pix_name") or "").strip()[:160],
            "email": str(data.get("pix_email") or "").strip()[:200],
            "line1": str(data.get("pix_line1") or "").strip()[:180],
            "city": str(data.get("pix_city") or "").strip()[:100],
            "state": str(data.get("pix_state") or "").strip()[:40],
            "postal_code": str(data.get("pix_postal_code") or "").strip()[:30],
        }
        if len(raw_pix_tax_id) == 14:
            try:
                pix_identity.update(lookup_cnpj_identity(raw_pix_tax_id))
            except Exception as exc:
                if not manual_identity["name"]:
                    return jsonify({"error": f"CNPJ 登记信息查询失败：{exc}"}), 400
        pix_identity.update({key: value for key, value in manual_identity.items() if value})
    token_raw = str(data.get("token") or "")
    try:
        access_token, token_meta = extract_access_token(token_raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 400
    options = {
        "access_token": access_token,
        "token_meta": token_meta,
        "token_lease_key": LEASES.token_hash(access_token),
        "plan": plan,
        "link_type": link_type,
        "country": country,
        "currency": currency,
        "checkout_country": country,
        "checkout_currency": currency,
        "entry_proxies": entry_proxies,
        "exit_proxies": exit_proxies if (link_type == "paypal" or dual_region_upi) else entry_proxies,
        "use_promo": promo_requested,
        "promo_proxy_country": "JP" if dual_region_upi else "",
        "promo_campaign": str(data.get("promo_campaign") or "") if plan == "plus" else "",
        "promo_code": str(data.get("promo_code") or "") if plan == "team" else "",
        "workspace_name": str(data.get("workspace_name") or "")[:80],
        "workspace_id": str(data.get("workspace_id") or "")[:120],
        "seat_quantity": min(999, max(2, int(data.get("seat_quantity") or 5))),
        "price_interval": "year" if data.get("price_interval") == "year" else "month",
        "credit_quantity": min(100000, max(1, int(data.get("credit_quantity") or 13))),
        "ideal_bank": str(data.get("ideal_bank") or "")[:40] if link_type == "ideal" else "",
        "pix_tax_id": raw_pix_tax_id,
        "pix_tax_id_auto": link_type == "pix" and not raw_pix_tax_id,
        "pix_auto_kind": str(data.get("pix_auto_kind") or "cpf").lower()
            if str(data.get("pix_auto_kind") or "cpf").lower() in {"mixed", "cpf", "cnpj"} else "cpf",
        "pix_identity": pix_identity,
        "retry_count": retry_count,
    }
    if link_type == "pix" and options["pix_tax_id"] and len(options["pix_tax_id"]) not in {11, 14}:
        return jsonify({"error": "PIX 需要填写 11 位 CPF 或 14 位 CNPJ"}), 400
    client_ip = request_client_ip()
    allowed, retry_after = IP_TASK_LIMITER.acquire(client_ip)
    if not allowed:
        response = jsonify({
            "error": f"当前 IP 每分钟最多创建 {IP_TASK_LIMITER.limit} 个任务，请在 {retry_after} 秒后重试。",
            "retry_after": retry_after,
            "limit": IP_TASK_LIMITER.limit,
        })
        response.headers["Retry-After"] = str(retry_after)
        return response, 429
    job_id = STORE.create(options)
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "queue_position": STORE.queue_position(job_id),
        "global_rpm": STORE.global_rpm,
        "ip_rpm": IP_TASK_LIMITER.limit,
    }), 202


@app.get("/api/checkout-progress")
def checkout_progress():
    job = STORE.get(str(request.args.get("job_id") or ""), public=True)
    if not job:
        if LEGACY_SERVICE_BASE:
            try:
                legacy = requests.get(
                    f"{LEGACY_SERVICE_BASE}/api/checkout-progress",
                    params={"job_id": str(request.args.get("job_id") or "")},
                    timeout=8,
                )
                return app.response_class(
                    response=legacy.content,
                    status=legacy.status_code,
                    content_type=legacy.headers.get("content-type", "application/json"),
                )
            except Exception:
                pass
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)


@app.post("/api/checkout-cancel")
def checkout_cancel():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "")
    ok = STORE.cancel(job_id)
    if not ok and LEGACY_SERVICE_BASE:
        try:
            legacy = requests.post(
                f"{LEGACY_SERVICE_BASE}/api/checkout-cancel",
                json={"job_id": job_id},
                timeout=8,
            )
            return app.response_class(
                response=legacy.content,
                status=legacy.status_code,
                content_type=legacy.headers.get("content-type", "application/json"),
            )
        except Exception:
            pass
    return jsonify({"ok": ok}), 200 if ok else 404


if __name__ == "__main__":
    app.run(host=os.getenv("PAY153_HOST", "127.0.0.1"), port=int(os.getenv("PAY153_PORT", "18082")), threaded=True)
