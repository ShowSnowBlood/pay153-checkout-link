from __future__ import annotations

import base64
import io
import json
import random
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from datetime import date, timedelta
from typing import Any, Callable

import stripe_checkout as sc
from billing_address_resolver import resolve_public_address


PROVIDER_DEFAULTS = {
    "paypal": {"country": "US", "currency": "USD"},
    "ideal": {"country": "NL", "currency": "EUR"},
    "upi": {"country": "IN", "currency": "INR"},
    "pix": {"country": "BR", "currency": "BRL"},
}

# PIX Automático / UPI AutoPay mandate_options were added after the Checkout
# Payment Page version currently returned by this merchant.  Use the current
# Stripe API train only for the direct SetupIntent fallback.
LOCAL_MANDATE_STRIPE_VERSION = "2026-06-24.dahlia"
LOCAL_MANDATE_STRIPE_VERSION_FULL = (
    f"{LOCAL_MANDATE_STRIPE_VERSION}; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
HOSTED_CHECKOUT_STRIPE_VERSION = (
    "2020-08-27;custom_checkout_beta=v1; "
    "checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
HOSTED_CHECKOUT_RUNTIME_VERSION = "e1fb22ad35"
UPI_NEXT_ACTION_TYPE = "upi_handle_redirect_or_display_qr_code"


def amount_is_zero(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return False
    return amount.is_finite() and amount == 0


def _amount_is_positive(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return False
    return amount.is_finite() and amount > 0


def generate_cpf() -> str:
    digits = [random.randint(0, 9) for _ in range(9)]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        value = 11 - sum(number * weight for number, weight in zip(digits, weights)) % 11
        digits.append(0 if value >= 10 else value)
    return "".join(map(str, digits))


def _format_cpf(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return digits


def _hosted_provider_return_url(hosted_url: str, provider: str) -> str:
    if not hosted_url:
        return "https://checkout.stripe.com/"
    base, marker, fragment = hosted_url.partition("#")
    joiner = "&" if "?" in base else "?"
    base = (
        f"{base}{joiner}redirect_pm_type={provider}"
        f"&lid={uuid.uuid4()}&ui_mode=custom"
    )
    return f"{base}#{fragment}" if marker else base


def payment_decline(data: dict) -> dict:
    payment_intent = data.get("payment_intent") or {}
    setup_intent = data.get("setup_intent") or {}
    return payment_intent.get("last_payment_error") or setup_intent.get("last_setup_error") or {}


def provider_failure_detail(data: dict) -> str:
    """Return the most useful local-payment failure fields without secrets."""
    decline = payment_decline(data)
    submission = data.get("submission_attempt") or {}
    setup_intent = data.get("setup_intent") or {}
    payment_intent = data.get("payment_intent") or {}
    values: list[str] = []
    for label, value in (
        ("submission_state", submission.get("state")),
        ("submission_failure", submission.get("failure_code") or submission.get("failure_reason")),
        ("decline_code", decline.get("decline_code") or decline.get("code")),
        ("decline_message", decline.get("message")),
        ("setup_status", setup_intent.get("status")),
        ("payment_status", payment_intent.get("status")),
    ):
        text = str(value or "").strip()
        if text:
            values.append(f"{label}={text[:220]}")
    return "; ".join(values)


def provider_terminal_failure_detail(data: dict) -> str:
    """Return failure detail only for states that cannot produce a valid action."""
    submission = data.get("submission_attempt") or {}
    setup_intent = data.get("setup_intent") or {}
    payment_intent = data.get("payment_intent") or {}
    submission_state = str(submission.get("state") or "").lower()
    setup_status = str(setup_intent.get("status") or "").lower()
    payment_status = str(payment_intent.get("status") or "").lower()
    terminal_states = {
        "canceled",
        "cancelled",
        "failed",
        "requires_confirmation",
        "requires_payment_method",
    }
    if (
        payment_decline(data)
        or submission_state in {"canceled", "cancelled", "failed"}
        or setup_status in terminal_states
        or payment_status in terminal_states
    ):
        return provider_failure_detail(data) or "terminal_provider_state"
    return ""


def extract_payment_method_id(*sources: Any) -> str:
    """Find a pm_* id from confirm/init/setup error payloads."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("payment_method", "payment_method_id"):
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get("id")
            value = str(value or "")
            if value.startswith("pm_"):
                return value
        setup_intent = source.get("setup_intent")
        if isinstance(setup_intent, dict):
            value = setup_intent.get("payment_method")
            if isinstance(value, dict):
                value = value.get("id")
            value = str(value or "")
            if value.startswith("pm_"):
                return value
            last_error = setup_intent.get("last_setup_error") or {}
            if isinstance(last_error, dict):
                value = last_error.get("payment_method")
                if isinstance(value, dict):
                    value = value.get("id")
                value = str(value or "")
                if value.startswith("pm_"):
                    return value
        submission = source.get("submission_attempt")
        if isinstance(submission, dict):
            value = submission.get("payment_method")
            if isinstance(value, dict):
                value = value.get("id")
            value = str(value or "")
            if value.startswith("pm_"):
                return value
        last_error = source.get("last_setup_error") or source.get("last_payment_error") or {}
        if isinstance(last_error, dict):
            value = last_error.get("payment_method")
            if isinstance(value, dict):
                value = value.get("id")
            value = str(value or "")
            if value.startswith("pm_"):
                return value
    return ""


def stash_setup_intent_context(ctx: dict, source: dict | None) -> None:
    """Keep SetupIntent id/secret from init/confirm so recover can still run."""
    if not isinstance(source, dict):
        return
    setup_intent = source.get("setup_intent") or {}
    if not isinstance(setup_intent, dict):
        return
    setup_id = str(setup_intent.get("id") or "")
    client_secret = str(setup_intent.get("client_secret") or "")
    if setup_id.startswith("seti_"):
        ctx["setup_intent_id"] = setup_id
    if client_secret.startswith("seti_") and "_secret_" in client_secret:
        ctx["setup_intent_client_secret"] = client_secret
    payment_method = extract_payment_method_id(setup_intent, source)
    if payment_method.startswith("pm_"):
        ctx["payment_method_id"] = payment_method


def resolve_setup_intent_credentials(payment_page: dict, ctx: dict) -> tuple[str, str]:
    setup_intent = payment_page.get("setup_intent") if isinstance(payment_page, dict) else {}
    if not isinstance(setup_intent, dict):
        setup_intent = {}
    setup_id = str(
        setup_intent.get("id")
        or ctx.get("setup_intent_id")
        or ""
    )
    client_secret = str(
        setup_intent.get("client_secret")
        or ctx.get("setup_intent_client_secret")
        or ""
    )
    return setup_id, client_secret


def seed_setup_intent_mandate(
    http,
    pk: str,
    provider: str,
    ctx: dict,
    log: Callable[[str], None],
) -> bool:
    """Best-effort write of AutoPay mandate onto the live SetupIntent.

    OpenAI often creates the SetupIntent without UPI mandate_options. Updating
    the intent with publishable-key scoped fields before Payment Page confirm
    gives Stripe a native place to attach the recurring authorization.

    Note: Stripe returns ``secret_key_required`` for publishable keys on
    ``POST /v1/setup_intents/{id}``. Skip immediately to avoid wasting rounds.
    """
    provider = (provider or "").lower()
    if provider not in {"upi", "pix"}:
        return False
    # Publishable keys cannot update SetupIntents (403 secret_key_required).
    if str(pk or "").startswith("pk_"):
        return False
    setup_id = str(ctx.get("setup_intent_id") or "")
    client_secret = str(ctx.get("setup_intent_client_secret") or "")
    if not setup_id.startswith("seti_") or not client_secret:
        return False
    local_options = build_local_mandate_options(
        provider,
        ctx,
        ctx.get("provider_payment_method_options") or {},
    )
    mandate = dict(local_options.get("mandate_options") or {})
    if not mandate:
        return False
    ctx["provider_payment_method_options"] = local_options
    body = {
        "client_secret": client_secret,
        "key": pk,
    }
    body.update(flatten_stripe_params(local_options, f"payment_method_options[{provider}]"))
    headers_candidates = [
        LOCAL_MANDATE_STRIPE_VERSION,
        sc.STRIPE_VERSION_FULL,
        sc.STRIPE_VERSION_BASE,
    ]
    for api_version in headers_candidates:
        headers = dict(sc._stripe_headers())
        headers["Stripe-Version"] = api_version
        try:
            resp = http.post(
                f"{sc.STRIPE_API}/v1/setup_intents/{setup_id}",
                data=body,
                headers=headers,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[{provider}] SetupIntent mandate 预写异常：{type(exc).__name__}: {exc}")
            continue
        try:
            status_code = int(getattr(resp, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status_code = 0
        if status_code == 200:
            try:
                payload = resp.json() or {}
            except Exception:
                payload = {}
            stash_setup_intent_context(ctx, {"setup_intent": payload})
            log(
                f"[{provider}] SetupIntent mandate 预写成功："
                f"api={api_version.split(';')[0]} amount={mandate.get('amount')}"
            )
            return True
        text = str(getattr(resp, "text", "") or "")[:240]
        log(f"[{provider}] SetupIntent mandate 预写失败 [{status_code}]: {text}")
        if "secret_key_required" in text:
            return False
    return False


def recover_upi_via_payment_page(
    http,
    pk: str,
    session_id: str,
    init_resp: dict,
    ctx: dict,
    billing: dict,
    profile: dict,
    version: str,
    log: Callable[[str], None],
    *,
    approve_callback=None,
    processor: str = "",
) -> dict:
    """Recover zero-due UPI QR through Payment Page re-confirm.

    Stripe rejects ``setup_intents/{id}/confirm`` for Checkout-created SetupIntents
    (\"You cannot confirm SetupIntents created by Checkout.\"). After merchant
    approval fails, re-submit via payment_pages/confirm. Prefer the pm_* that was
    attached before a late promotion; only create a fresh inline attempt when
    the current intent also supplied AutoPay mandate data.

    Important: never send both ``Stripe-Version`` header and body
    ``_stripe_version`` — Stripe returns 400 invalid_request_error.
    """
    try:
        amount = int(str(ctx.get("checkout_amount") or 0))
    except (TypeError, ValueError):
        amount = 0
    if amount != 0 and str(ctx.get("checkout_amount")).strip() not in {"0", "0.0", "0.00"}:
        return {}

    server_options = dict(ctx.get("provider_payment_method_options") or {})
    mandate = dict(server_options.get("mandate_options") or {})
    ctx["provider_payment_method_options"] = server_options
    ctx["server_upi_mandate_present"] = bool(mandate)
    ctx["local_mandate_synthesized"] = False
    return_url = str(ctx.get("stripe_hosted_url") or ctx.get("return_url") or "https://chatgpt.com/")
    last_page: dict = {}
    version_candidates = (
        LOCAL_MANDATE_STRIPE_VERSION_FULL,
        sc.STRIPE_VERSION_FULL,
        sc.STRIPE_VERSION_BASE,
    )

    # Zero-only mode uses one bounded round. A late-promo attempt may have no
    # explicit mandate payload but still has the already-attached pm_*.
    for round_idx in range(1, 2):
        pm_id = str(ctx.get("payment_method_id") or "")
        if mandate and not pm_id.startswith("pm_"):
            try:
                pm_id = create_provider_payment_method(
                    http, pk, session_id, "upi", version, ctx, billing, log,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"[upi] Payment Page 补交创建 pm 失败：{type(exc).__name__}: {exc}")
        if not str(pm_id).startswith("pm_"):
            log("[upi] Payment Page 补交缺少已挂载 pm_*，停止")
            break
        ctx["payment_method_id"] = pm_id

        # Reuse the pre-promotion PaymentMethod first. With a server mandate,
        # retain one inline fallback that preserves those options exactly.
        addr = (billing or {}).get("address") or {}
        variants: list[tuple[str, dict]] = [
            ("pp_existing_pm", {"mode": "pm_id", "payment_method": pm_id}),
        ]
        if mandate:
            variants.append(("pp_inline_server", {"mode": "inline"}))
        else:
            log("[upi] 服务端未返回 AutoPay mandate，使用归零前已挂载 pm_* 做一次受控补交")
        # Prefer Payment Page init train (basil) first — matches zero Checkout.
        version_candidates = (
            sc.STRIPE_VERSION_FULL,
            LOCAL_MANDATE_STRIPE_VERSION_FULL,
        )

        for variant_name, options in variants:
            page: dict | None = None
            for api_version in version_candidates:
                body: dict[str, str] = {
                    "key": pk,
                    "_stripe_version": api_version,
                    "expected_amount": "0",
                    "expected_payment_method_type": "upi",
                    "return_url": return_url,
                    "init_checksum": str(
                        init_resp.get("init_checksum") or ctx.get("init_checksum") or ""
                    ),
                }
                if options.get("mode") == "pm_id":
                    body["payment_method"] = str(options.get("payment_method") or pm_id)
                else:
                    # Inline PM + mandate under payment_method_data (browser shape).
                    body.update({
                        "payment_method_data[type]": "upi",
                        "payment_method_data[billing_details][name]": str(
                            billing.get("name") or "Arjun Sharma"
                        ),
                        "payment_method_data[billing_details][email]": str(
                            billing.get("email") or f"upi-{uuid.uuid4().hex[:8]}@outlook.com"
                        ),
                        "payment_method_data[billing_details][address][line1]": str(
                            addr.get("line1") or "1 MG Road"
                        ),
                        "payment_method_data[billing_details][address][city]": str(
                            addr.get("city") or "Bengaluru"
                        ),
                        "payment_method_data[billing_details][address][state]": str(
                            addr.get("state") or "KA"
                        ),
                        "payment_method_data[billing_details][address][postal_code]": str(
                            addr.get("postal_code") or "560001"
                        ),
                        "payment_method_data[billing_details][address][country]": str(
                            addr.get("country") or "IN"
                        ),
                    })
                    body.update(
                        flatten_stripe_params(server_options, "payment_method_data[upi]")
                    )
                body = {k: v for k, v in body.items() if v not in (None, "")}
                headers = dict(sc._stripe_headers())
                headers.pop("Stripe-Version", None)
                log(
                    f"[upi] Payment Page 补交 round={round_idx} variant={variant_name} "
                    f"mode={options.get('mode')} api={api_version.split(';')[0]}"
                )
                try:
                    resp = http.post(
                        f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
                        data=body,
                        headers=headers,
                        timeout=40,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"[upi] Payment Page 补交异常：{type(exc).__name__}: {exc}")
                    continue

                # Strip unknown params repeatedly without dual version.
                # Payment Page rejects payment_method_options / mandate_data;
                # also fall back to regex when helper misses "param".
                for _ in range(8):
                    status_code = int(getattr(resp, "status_code", 0) or 0)
                    text = str(getattr(resp, "text", "") or "")
                    if status_code == 200:
                        break
                    unknown = ""
                    if hasattr(sc, "_stripe_unknown_param"):
                        unknown = sc._stripe_unknown_param(resp) or ""
                    if not unknown:
                        m = re.search(
                            r'"param"\s*:\s*"([^"]+)"',
                            text,
                        ) or re.search(
                            r"unknown parameter:\s*([A-Za-z0-9_\[\].]+)",
                            text,
                            flags=re.I,
                        )
                        if m:
                            unknown = m.group(1).split(".")[0].split("[")[0]
                    if not unknown or unknown in {"_stripe_version", "Stripe-Version", "key"}:
                        log(f"[upi] Payment Page 补交失败 [{status_code}]: {text[:280]}")
                        break
                    log(f"[upi] Payment Page 补交移除不支持参数：{unknown}")
                    body = {
                        k: v for k, v in body.items()
                        if k != unknown and not k.startswith(f"{unknown}[")
                    }
                    try:
                        resp = http.post(
                            f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
                            data=body,
                            headers=headers,
                            timeout=40,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log(f"[upi] Payment Page 补交重试异常：{type(exc).__name__}: {exc}")
                        resp = None
                        break
                if resp is None:
                    continue
                status_code = int(getattr(resp, "status_code", 0) or 0)
                text = str(getattr(resp, "text", "") or "")
                if status_code != 200:
                    # Version / dual-write issues → try next api_version.
                    if any(
                        marker in text
                        for marker in ("Stripe-Version", "_stripe_version", "API version")
                    ):
                        continue
                    # Other 400s: try next variant (not next api version forever).
                    break

                try:
                    page = resp.json() or {}
                except Exception:
                    log("[upi] Payment Page 补交返回非 JSON")
                    page = None
                    break

                last_page = page
                out = extract_provider_result(page, "upi")
                sub = page.get("submission_attempt") or {}
                setup = page.get("setup_intent") or {}
                log(
                    f"[upi] Payment Page 补交返回：submission={sub.get('state') or ''} "
                    f"setup={setup.get('status') or ''} "
                    f"next={sc._find_next_action(page).get('type') or ''}"
                )
                if provider_has_action(out):
                    log("[upi] Payment Page 补交已产出 UPI QR/跳转")
                    return page
                break  # stop version loop after first 200 for this variant

            if not page:
                continue

            sub = page.get("submission_attempt") or {}
            if str(sub.get("state") or "") == "requires_approval" and approve_callback:
                log("[upi] Payment Page 补交再次 requires_approval，重新提交 approve")
                try:
                    approve_callback(processor)
                except Exception as exc:  # noqa: BLE001
                    log(f"[upi] 补交 approve 提示：{type(exc).__name__}: {exc}")
                page = sc.poll_payment_page_after_approve(
                    http, pk, session_id, log, ctx=ctx, max_attempts=3,
                )
                last_page = page
                out = extract_provider_result(page, "upi")
                if provider_has_action(out):
                    log("[upi] 补交 approve/poll 已产出 UPI QR/跳转")
                    return page
                still = str((page.get("submission_attempt") or {}).get("state") or "")
                decline = payment_decline(page)
                # setup_attempt_failed after approved means merchant path has no
                # usable UPI AutoPay mandate — stop recover immediately.
                if still == "failed" or (decline or {}).get("code") == "setup_attempt_failed":
                    log("[upi] 补交后 setup_attempt_failed，结束补交（换号/换 IP）")
                    return last_page
                if still == "requires_approval":
                    log("[upi] 补交后仍 requires_approval，结束本轮补交")
                    return last_page
                try:
                    reinit_data, _, _ = sc.init_checkout(http, session_id, pk, profile, log)
                    out = extract_provider_result(reinit_data, "upi")
                    if provider_has_action(out):
                        log("[upi] 补交 re-init 已产出 UPI QR/跳转")
                        return reinit_data
                    last_page = reinit_data if isinstance(reinit_data, dict) else last_page
                except Exception as exc:  # noqa: BLE001
                    log(f"[upi] 补交 re-init 提示：{type(exc).__name__}: {exc}")
                # One approve cycle per recover is enough.
                return last_page
    return last_page


def confirm_local_setup_intent(
    http,
    pk: str,
    provider: str,
    payment_page: dict,
    payment_method_id: str,
    ctx: dict,
    log: Callable[[str], None],
) -> dict:
    """Legacy SetupIntent direct confirm.

    Checkout-created SetupIntents reject this path with:
    \"You cannot confirm SetupIntents created by Checkout.\"
    Kept for non-Checkout flows / diagnostics; UPI zero-due recover uses
    ``recover_upi_via_payment_page`` instead.
    """
    provider = provider.lower()
    stash_setup_intent_context(ctx, payment_page)
    setup_intent = payment_page.get("setup_intent") or {}
    if not isinstance(setup_intent, dict):
        setup_intent = {}
    setup_id, client_secret = resolve_setup_intent_credentials(payment_page, ctx)
    candidate_pm = str(payment_method_id or ctx.get("payment_method_id") or "")
    if not candidate_pm.startswith("pm_"):
        candidate_pm = extract_payment_method_id(
            payment_page,
            setup_intent,
            payment_page.get("submission_attempt") if isinstance(payment_page, dict) else {},
            {"payment_method": ctx.get("payment_method_id")},
        )
    if candidate_pm.startswith("pm_"):
        ctx["payment_method_id"] = candidate_pm
    if not setup_id.startswith("seti_") or not client_secret or not candidate_pm.startswith("pm_"):
        log(
            f"[{provider}] SetupIntent 直连补交条件不足："
            f"setup_id={setup_id[:18] + '…' if setup_id else False} "
            f"client_secret={bool(client_secret)} "
            f"pm_id={candidate_pm[:16] + '…' if candidate_pm.startswith('pm_') else False}"
        )
        return payment_page

    # Fast-fail Checkout-owned SetupIntents: Stripe never allows public-key
    # confirm on them, so skip the expensive multi-variant matrix.
    probe_body = {
        "client_secret": client_secret,
        "payment_method": candidate_pm,
        "key": pk,
    }
    headers = dict(sc._stripe_headers())
    headers["Stripe-Version"] = sc.STRIPE_VERSION_FULL
    try:
        probe = http.post(
            f"{sc.STRIPE_API}/v1/setup_intents/{setup_id}/confirm",
            data=probe_body,
            headers=headers,
            timeout=20,
        )
        probe_text = str(getattr(probe, "text", "") or "")
        if int(getattr(probe, "status_code", 0) or 0) == 400 and "created by Checkout" in probe_text:
            log(
                f"[{provider}] SetupIntent 由 Checkout 创建，禁止直连 confirm；"
                "改走 Payment Page 补交"
            )
            return payment_page
    except Exception as exc:  # noqa: BLE001
        log(f"[{provider}] SetupIntent 直连探测异常：{type(exc).__name__}: {exc}")

    mandate_amount = resolve_mandate_amount(provider, ctx)
    base_options = dict(ctx.get("provider_payment_method_options") or {})
    base_mandate = dict((base_options.get("mandate_options") or {}))
    if provider == "upi" and not base_mandate:
        log("[upi] 服务端未配置 UPI AutoPay mandate，跳过 SetupIntent 直连补交")
        return payment_page
    if not base_mandate:
        base_mandate = dict(
            (build_local_mandate_options(provider, ctx, base_options).get("mandate_options") or {})
        )
    return_url = str(ctx.get("stripe_hosted_url") or ctx.get("return_url") or "https://chatgpt.com/")
    headers_candidates = [
        LOCAL_MANDATE_STRIPE_VERSION,
        sc.STRIPE_VERSION_FULL,
        sc.STRIPE_VERSION_BASE,
    ]

    variants: list[tuple[str, dict]] = []
    if provider == "upi":
        variants.extend(
            [
                ("upi_server", base_options),
                ("upi_pm_only", {}),
            ]
        )
    elif provider == "pix":
        amount = int(base_mandate.get("amount") or mandate_amount)
        variants.extend(
            [
                (
                    "pix_monthly",
                    {
                        "mandate_options": {
                            "amount": amount,
                            "amount_type": "maximum",
                            "payment_schedule": str(base_mandate.get("payment_schedule") or "monthly"),
                            "start_date": str(
                                base_mandate.get("start_date")
                                or (date.today() + timedelta(days=3)).isoformat()
                            ),
                        }
                    },
                ),
                ("pix_pm_only", {}),
            ]
        )
    else:
        variants.append(("pm_only", {}))

    last_error = ""
    for variant_name, options in variants:
        body = {
            "client_secret": client_secret,
            "payment_method": candidate_pm,
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "mandate_data[customer_acceptance][type]": "online",
            "mandate_data[customer_acceptance][online][infer_from_client]": "true",
            "key": pk,
        }
        if options:
            body.update(flatten_stripe_params(options, f"payment_method_options[{provider}]"))
        mandate_keys = sorted((options.get("mandate_options") or {}).keys())
        for api_version in headers_candidates:
            headers = dict(sc._stripe_headers())
            headers["Stripe-Version"] = api_version
            log(
                f"[{provider}] SetupIntent 直连补交：variant={variant_name} "
                f"amount={mandate_amount} mandate_keys={','.join(mandate_keys) or '-'} "
                f"api={api_version.split(';')[0]}"
            )
            try:
                resp = http.post(
                    f"{sc.STRIPE_API}/v1/setup_intents/{setup_id}/confirm",
                    data=body,
                    headers=headers,
                    timeout=40,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                log(f"[{provider}] SetupIntent 直连补交异常：{last_error}")
                continue
            resp_text = getattr(resp, "text", "") or ""
            status_code = getattr(resp, "status_code", 0)
            if status_code != 200:
                last_error = f"HTTP {status_code}: {resp_text[:360]}"
                log(f"[{provider}] SetupIntent 直连补交失败 [{status_code}]: {resp_text[:360]}")
                if "created by Checkout" in resp_text:
                    return payment_page
                continue
            try:
                payload = resp.json() or {}
            except Exception:
                last_error = "invalid JSON from SetupIntent confirm"
                continue
            next_action = payload.get("next_action") or {}
            log(
                f"[{provider}] SetupIntent 直连补交返回：status={payload.get('status') or ''} "
                f"next_action={next_action.get('type') or ''}"
            )
            merged = dict(payment_page)
            merged["setup_intent"] = payload
            if next_action:
                merged["next_action"] = next_action
            if payload.get("status") in {"requires_action", "succeeded", "processing"}:
                submission = dict(merged.get("submission_attempt") or {})
                if submission.get("state") == "failed":
                    submission["state"] = "processing"
                    merged["submission_attempt"] = submission
            return merged
    if last_error:
        log(f"[{provider}] SetupIntent 直连补交全部变体失败：{last_error[:300]}")
    return payment_page


def flatten_stripe_params(value: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}[{key}]" if prefix else str(key)
            out.update(flatten_stripe_params(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(flatten_stripe_params(item, f"{prefix}[{index}]"))
    elif value is not None and prefix:
        if isinstance(value, bool):
            out[prefix] = "true" if value else "false"
        else:
            out[prefix] = str(value)
    return out


def resolve_mandate_amount(provider: str, ctx: dict) -> int:
    """Pick a recurring ceiling amount for local PIX/UPI AutoPay mandates."""
    provider = (provider or "").lower()
    for key in ("original_checkout_amount", "checkout_amount"):
        raw = ctx.get(key)
        if raw in (None, "", 0, "0", "0.0", "0.00"):
            continue
        try:
            amount = max(1, int(str(raw)))
        except (TypeError, ValueError):
            continue
        if amount > 1:
            return amount
    return 9990 if provider == "pix" else 199900


def extract_server_provider_options(init_resp: dict, provider: str) -> dict:
    """Return provider options emitted by Stripe/OpenAI for the live intent.

    Payment Page responses are not consistent about where they expose these
    options.  The intent object is more specific than the top-level Checkout
    defaults, so later sources merge over earlier ones.
    """
    if not isinstance(init_resp, dict):
        return {}

    provider = (provider or "").lower()
    sources: list[dict] = [init_resp]
    for key in ("payment_intent", "setup_intent"):
        intent = init_resp.get(key)
        if isinstance(intent, dict):
            sources.append(intent)

    payment_method_object = init_resp.get("payment_method_object")
    if isinstance(payment_method_object, dict):
        for key in ("payment_intent", "setup_intent"):
            intent = payment_method_object.get(key)
            if isinstance(intent, dict):
                sources.append(intent)

    merged: dict = {}
    for source in sources:
        payment_method_options = source.get("payment_method_options")
        if not isinstance(payment_method_options, dict):
            continue
        options = payment_method_options.get(provider)
        if not isinstance(options, dict):
            continue
        _merge_nonempty_server_options(merged, options)
    return merged


def _merge_nonempty_server_options(base: dict, incoming: dict) -> dict:
    """Merge server data without letting empty values erase valid fields."""
    for key, value in incoming.items():
        if value is None or value == "" or value == {} or value == []:
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_nonempty_server_options(base[key], value)
        elif isinstance(value, dict):
            base[key] = _merge_nonempty_server_options({}, value)
        else:
            base[key] = value
    return base


def merge_server_provider_options(
    ctx: dict,
    provider: str,
    *payloads: dict,
    reset: bool = False,
) -> dict:
    """Merge provider options observed across Stripe server responses."""
    merged: dict = {}
    if not reset:
        _merge_nonempty_server_options(
            merged,
            dict(ctx.get("provider_payment_method_options") or {}),
        )
    for payload in payloads:
        options = extract_server_provider_options(payload, provider)
        _merge_nonempty_server_options(merged, options)
    if merged:
        ctx["provider_payment_method_options"] = merged
    elif reset:
        ctx.pop("provider_payment_method_options", None)
    return merged


def build_local_mandate_options(provider: str, ctx: dict, server_options: dict | None = None) -> dict:
    """Build local PIX options while preserving server-owned UPI options.

    A Checkout-created UPI SetupIntent mandate is merchant configuration.  A
    publishable-key client cannot repair a missing mandate, so UPI options must
    pass through unchanged instead of being synthesized locally.
    """
    provider = (provider or "").lower()
    server_options = dict(server_options or {})
    if provider == "upi":
        return server_options
    mandate = dict(server_options.get("mandate_options") or {})
    amount = resolve_mandate_amount(provider, ctx)
    if provider == "pix":
        mandate.setdefault("amount", amount)
        mandate.setdefault("amount_type", "maximum")
        mandate.setdefault("payment_schedule", "monthly")
        mandate.setdefault("start_date", (date.today() + timedelta(days=3)).isoformat())
    else:
        return server_options
    local = dict(server_options)
    local["mandate_options"] = mandate
    return local


def provider_has_action(result: dict) -> bool:
    if result.get("fallback_reason"):
        return False
    has_action = bool(
        result.get("provider_redirect_url")
        or result.get("qr_image_png")
        or result.get("qr_image_svg")
        or result.get("qr_data")
    )
    if str(result.get("provider") or "").lower() == "upi":
        if not has_action or result.get("next_action_type") != UPI_NEXT_ACTION_TYPE:
            return False
        redirect = str(result.get("provider_redirect_url") or "")
        redirect_host = str(urlsplit(redirect).hostname or "").lower() if redirect else ""
        return redirect_host not in {"pay.openai.com", "checkout.stripe.com"}
    return has_action


def default_billing(country: str, email: str = "", tax_id: str = "", geo: dict[str, str] | None = None, real_random: bool = False) -> dict[str, Any]:
    country = (country or "US").upper()
    rows = {
        "DE": ("Alex Meyer", "Friedrichstrasse 100", "Berlin", "10117", "BE"),
        "NL": ("Lars de Vries", "Damrak 1", "Amsterdam", "1012LG", "NH"),
        "IN": ("Arjun Sharma", "1 MG Road", "Bengaluru", "560001", "KA"),
        "BR": ("Lucas Silva", "Avenida Paulista 1000", "Sao Paulo", "01310-100", "SP"),
        "US": ("Alex Morgan", "1 Market Street", "San Francisco", "94105", "CA"),
        "GB": ("Alex Taylor", "10 King Street", "London", "SW1A 1AA", "London"),
        "FR": ("Alex Martin", "10 Rue de Rivoli", "Paris", "75001", "IDF"),
        "AU": ("Alex Wilson", "1 George Street", "Sydney", "2000", "NSW"),
        "JP": ("Haruto Sato", "1-1 Marunouchi", "Chiyoda", "100-0005", "Tokyo"),
        "KR": ("Minjun Kim", "30 Eulji-ro", "Seoul", "04533", "Seoul"),
        "BA": ("Adnan Hadzic", "Zmaja od Bosne 7", "Sarajevo", "71000", ""),
    }
    geo = geo or {}
    name, line1, city, postal, state = rows.get(country, rows["US"])
    local_names = {
        "BA": ["Adnan Hadzic", "Amar Kovacevic", "Haris Basic", "Lejla Music", "Amina Softic", "Emir Delic"],
        "US": ["Alex Morgan", "Jordan Taylor", "Casey Wilson", "Taylor Reed"],
        "GB": ["Oliver Smith", "George Taylor", "Amelia Wilson", "Sophie Brown"],
        "BR": ["Lucas Silva", "Gabriel Santos", "Mariana Costa", "Ana Oliveira"],
        "IN": ["Arjun Sharma", "Rahul Verma", "Priya Patel", "Ananya Singh"],
        "KR": ["Minjun Kim", "Jihoon Lee", "Seo-yeon Kim", "Ji-woo Park"],
    }
    if country in local_names:
        name = random.SystemRandom().choice(local_names[country])
    address_source = "country_profile"
    place_name = ""
    if geo:
        city = geo.get("city") or city
        postal = geo.get("postal") or postal
        state = geo.get("region") or state
    if real_random:
        resolved = resolve_public_address(
            country,
            str(geo.get("city") or city),
            str(geo.get("region") or state),
            str(geo.get("postal") or postal),
        )
        if resolved:
            line1 = resolved.get("line1") or line1
            city = resolved.get("city") or city
            postal = resolved.get("postal_code") or postal
            state = resolved.get("state") or state
            address_source = resolved.get("source") or "public_address_pool"
            place_name = resolved.get("name") or ""
    if country == "BA":
        postal_digits = re.sub(r"\D", "", str(postal or ""))
        if len(postal_digits) == 5:
            postal = postal_digits
        elif not real_random:
            postal = "71000"
        state = ""
    if country == "US":
        us_states = {
            "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
            "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
            "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
            "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
            "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
            "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
            "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
            "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
            "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
            "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
            "district of columbia":"DC",
        }
        raw_state = str(state or "").strip()
        state = raw_state.upper() if re.fullmatch(r"[A-Za-z]{2}", raw_state) else us_states.get(raw_state.lower(), raw_state[:2].upper())
        postal_match = re.search(r"\b\d{5}(?:-\d{4})?\b", str(postal or ""))
        if postal_match:
            postal = postal_match.group(0)
    if country == "GB":
        state = ""
        postal = str(postal or "").strip().upper()
    if country == "BR":
        br_states = {
            "acre": "AC", "alagoas": "AL", "amapá": "AP", "amapa": "AP",
            "amazonas": "AM", "bahia": "BA", "ceará": "CE", "ceara": "CE",
            "distrito federal": "DF", "espírito santo": "ES", "espirito santo": "ES",
            "goiás": "GO", "goias": "GO", "maranhão": "MA", "maranhao": "MA",
            "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
            "pará": "PA", "para": "PA", "paraíba": "PB", "paraiba": "PB",
            "paraná": "PR", "parana": "PR", "pernambuco": "PE", "piauí": "PI",
            "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
            "rio grande do sul": "RS", "rondônia": "RO", "rondonia": "RO",
            "roraima": "RR", "santa catarina": "SC", "são paulo": "SP",
            "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
        }
        raw_state = str(state or "").strip()
        state = raw_state.upper() if re.fullmatch(r"[A-Za-z]{2}", raw_state) else br_states.get(raw_state.lower(), "SP")
        postal = re.sub(r"\D", "", str(postal or ""))
        if len(postal) != 8:
            postal = "01310100"
    if country == "KR":
        postal_digits = re.sub(r"\D", "", str(postal or ""))
        if len(postal_digits) == 5:
            postal = postal_digits
        else:
            # Korean Stripe/PayPal billing requires the modern 5-digit postal code.
            name, line1, city, postal, state = rows["KR"]
            address_source = "country_profile_postal_fallback"
            place_name = "Lotte Hotel Seoul"
    if country not in rows and address_source == "country_profile":
        line1 = "1 Main Street"
        postal = geo.get("postal") or "00000"
    billing = {
        "name": name,
        "email": email or f"checkout-{uuid.uuid4().hex[:10]}@outlook.com",
        "address": {
            "country": country,
            "line1": line1,
            "city": city,
            "postal_code": postal,
            "state": state,
        },
    }
    billing["_address_source"] = address_source
    if place_name:
        billing["_place_name"] = place_name
    if tax_id:
        billing["tax_id"] = re.sub(r"\D", "", tax_id)
    return billing


def _runtime_meta(ctx: dict, session_id: str) -> tuple[dict, dict]:
    guid, muid, sid = ctx.get("guid"), ctx.get("muid"), ctx.get("sid")
    if not (guid and muid and sid):
        guid, muid, sid = sc._gen_fingerprint()
        ctx["guid"], ctx["muid"], ctx["sid"] = guid, muid, sid
    runtime_version = ctx.get("runtime_version") or sc.DEFAULT_STRIPE_RUNTIME_VERSION
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", sc._gen_elements_session_id())
    elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
    checkout_config_id = ctx.get("config_id") or ""
    common = {
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "runtime_version": runtime_version,
        "stripe_js_id": stripe_js_id,
        "elements_session_id": elements_session_id,
        "elements_session_config_id": elements_session_config_id,
        "checkout_config_id": checkout_config_id,
    }
    attr = {
        "client_session_id": stripe_js_id,
        "checkout_session_id": session_id,
        "checkout_config_id": checkout_config_id,
        "elements_session_id": elements_session_id,
        "elements_session_config_id": elements_session_config_id,
    }
    return common, attr


def _hosted_checkout_init(http, session_id: str, pk: str, profile: dict, ctx: dict, log):
    """Refresh PIX using the same compact init shape as checkout.stripe.com."""
    resp = http.post(
        f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/init",
        data={
            "key": pk,
            "eid": "NA",
            "browser_locale": profile.get("browser_locale") or "pt-BR",
            "browser_timezone": profile.get("browser_timezone") or "America/Sao_Paulo",
            "redirect_type": "url",
        },
        headers=sc._stripe_headers(),
        timeout=35,
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(
            f"PIX hosted init [{getattr(resp, 'status_code', '?')}]: "
            f"{(getattr(resp, 'text', '') or '')[:500]}"
        )
    payload = resp.json() or {}
    total = payload.get("total_summary") or {}
    amount = total.get("due")
    if amount is None:
        amount = (payload.get("invoice") or {}).get("amount_due")
    ctx.update({
        "runtime_version": HOSTED_CHECKOUT_RUNTIME_VERSION,
        "config_id": payload.get("config_id") or ctx.get("config_id") or "",
        "init_checksum": payload.get("init_checksum") or ctx.get("init_checksum") or "",
        "stripe_hosted_url": payload.get("stripe_hosted_url") or ctx.get("stripe_hosted_url") or "",
        "return_url": payload.get("return_url") or ctx.get("return_url") or "",
        "currency": str(payload.get("currency") or ctx.get("currency") or "brl").lower(),
        "checkout_amount": amount,
        "payment_method_types": sc._extract_payment_method_types(payload),
    })
    log(
        f"[stripe] PIX hosted init amount={amount} currency={ctx['currency']} "
        f"pm={ctx['payment_method_types']}"
    )
    return payload, HOSTED_CHECKOUT_STRIPE_VERSION, ctx


def create_provider_payment_method(
    http,
    pk: str,
    session_id: str,
    provider: str,
    version: str,
    ctx: dict,
    billing: dict,
    log: Callable[[str], None],
) -> str:
    """Create PIX/UPI PaymentMethod before confirming the Payment Page.

    Submitting local-method billing details inline on every confirm can create
    a new setup attempt after manual approval.  A standalone ``pm_*`` keeps the
    approved submission bound to one PaymentMethod and lets the post-approval
    poll read the original QR/action.
    """
    provider = provider.lower()
    addr = billing.get("address") or {}
    common, attr = _runtime_meta(ctx, session_id)
    try:
        zero_due = int(str(ctx.get("checkout_amount") or 0)) == 0
    except (TypeError, ValueError):
        zero_due = str(ctx.get("checkout_amount")).strip() in {"0", "0.0", "0.00"}
    browser_exact = provider == "pix" and zero_due
    if browser_exact:
        # Captured from Stripe's own hosted Checkout PIX form.  The older
        # custom-checkout revision leaves mandate_options on the server-side
        # Checkout Session; forwarding them to /confirm makes Stripe discard
        # the valid recurring PIX setup attempt.
        data = {
            "type": "pix",
            "billing_details[name]": billing.get("name", ""),
            "billing_details[email]": billing.get("email", ""),
            "billing_details[address][country]": addr.get("country", "BR"),
            "billing_details[address][line1]": addr.get("line1", ""),
            "billing_details[address][line2]": addr.get("line2", ""),
            "billing_details[address][city]": addr.get("city", ""),
            "billing_details[address][postal_code]": re.sub(r"\D", "", str(addr.get("postal_code", ""))),
            "billing_details[address][state]": addr.get("state", "SP"),
            "billing_details[tax_id]": _format_cpf(billing.get("tax_id", "")),
            "_stripe_version": HOSTED_CHECKOUT_STRIPE_VERSION,
            "key": pk,
            "payment_user_agent": (
                f"stripe.js/{HOSTED_CHECKOUT_RUNTIME_VERSION}; "
                f"stripe-js-v3/{HOSTED_CHECKOUT_RUNTIME_VERSION}; checkout"
            ),
            "client_attribution_metadata[client_session_id]": attr["client_session_id"],
            "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
        }
        data = {key: value for key, value in data.items() if value not in (None, "")}
        resp = http.post(
            f"{sc.STRIPE_API}/v1/payment_methods",
            data=data,
            headers=sc._stripe_headers(),
            timeout=35,
        )
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(
                f"pix PaymentMethod 创建失败 [{getattr(resp, 'status_code', '?')}]: "
                f"{(getattr(resp, 'text', '') or '')[:500]}"
            )
        payment_method_id = str((resp.json() or {}).get("id") or "")
        if not payment_method_id.startswith("pm_"):
            raise RuntimeError("pix PaymentMethod 未返回 pm_id")
        ctx["payment_method_id"] = payment_method_id
        ctx["pix_hosted_exact"] = True
        log(f"[stripe] pix hosted-checkout payment_method: {payment_method_id}")
        return payment_method_id
    data = {
        "type": provider,
        "billing_details[name]": billing.get("name", ""),
        "billing_details[email]": billing.get("email", ""),
        "billing_details[address][country]": addr.get("country", ""),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "payment_user_agent": (
            f"stripe.js/{common['runtime_version']}; stripe-js-v3/{common['runtime_version']}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[client_session_id]": attr["client_session_id"],
        "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
        "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
        "client_attribution_metadata[elements_session_id]": attr["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": attr["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "key": pk,
        "_stripe_version": sc.STRIPE_VERSION_FULL,
    }
    if billing.get("tax_id"):
        data["billing_details[tax_id]"] = billing["tax_id"]
    data = {key: value for key, value in data.items() if value not in (None, "")}
    resp = http.post(
        f"{sc.STRIPE_API}/v1/payment_methods",
        data=data,
        headers=sc._stripe_headers(),
        timeout=35,
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(
            f"{provider} PaymentMethod 创建失败 [{getattr(resp, 'status_code', '?')}]: "
            f"{(getattr(resp, 'text', '') or '')[:500]}"
        )
    payload = resp.json() or {}
    payment_method_id = str(payload.get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise RuntimeError(f"{provider} PaymentMethod 未返回 pm_id")
    ctx["payment_method_id"] = payment_method_id
    log(f"[stripe] {provider} payment_method: {payment_method_id}")
    return payment_method_id


def confirm_upi_hosted_minimal(
    http,
    pk: str,
    session_id: str,
    init_resp: dict,
    ctx: dict,
    billing: dict,
    log: Callable[[str], None],
) -> dict:
    """Minimal UPI Payment Page confirm used by live OpenAI hosted extractors.

    Avoids custom-checkout-only fields that this merchant revision reports as
    unknown and strips.  A zero-due confirm is valid only when the live intent
    already contains server-owned UPI ``mandate_options``.  The Hosted request
    itself does not synthesize or inject mandate fields.
    """
    addr = (billing or {}).get("address") or {}
    amount = ctx.get("checkout_amount")
    if amount is None:
        total = init_resp.get("total_summary") or {}
        amount = total.get("due")
    if amount is None:
        amount = (init_resp.get("invoice") or {}).get("amount_due", 0)
    try:
        zero_due = int(str(amount or 0)) == 0
    except (TypeError, ValueError):
        zero_due = str(amount).strip() in {"0", "0.0", "0.00"}

    eid = str(init_resp.get("eid") or "").strip()
    init_checksum = str(init_resp.get("init_checksum") or ctx.get("init_checksum") or "").strip()

    version_candidates = [sc.STRIPE_VERSION_FULL]

    server_upi: dict = {}
    if zero_due:
        _merge_nonempty_server_options(
            server_upi,
            extract_server_provider_options(init_resp, "upi"),
        )
        _merge_nonempty_server_options(
            server_upi,
            dict(ctx.get("provider_payment_method_options") or {}),
        )
    if zero_due:
        server_mandate = dict(server_upi.get("mandate_options") or {})
        ctx["provider_payment_method_options"] = dict(server_upi)
        ctx["server_upi_mandate_present"] = bool(server_mandate)
        ctx["local_mandate_synthesized"] = False
        if not server_mandate:
            raise RuntimeError("0 元 UPI Checkout 缺少服务端 AutoPay mandate，拒绝提交 confirm")

    mandate_layouts: list[tuple[str, dict[str, str]]] = [("", {})]

    last_error = ""
    for api_version in version_candidates:
        for layout_name, mandate_fields in mandate_layouts:
            body: dict[str, str] = {
                "key": pk,
                "_stripe_version": api_version,
                "expected_amount": str(amount or 0),
                "expected_payment_method_type": "upi",
                "payment_method_data[type]": "upi",
                "payment_method_data[billing_details][name]": str(
                    billing.get("name") or "Arjun Sharma"
                ),
                "payment_method_data[billing_details][email]": str(
                    billing.get("email") or f"upi-{uuid.uuid4().hex[:8]}@outlook.com"
                ),
                "payment_method_data[billing_details][address][line1]": str(
                    addr.get("line1") or "1 MG Road"
                ),
                "payment_method_data[billing_details][address][city]": str(
                    addr.get("city") or "Bengaluru"
                ),
                "payment_method_data[billing_details][address][state]": str(
                    addr.get("state") or "KA"
                ),
                "payment_method_data[billing_details][address][postal_code]": str(
                    addr.get("postal_code") or "560001"
                ),
                "payment_method_data[billing_details][address][country]": str(
                    addr.get("country") or "IN"
                ),
            }
            if eid:
                body["eid"] = eid
            if init_checksum:
                body["init_checksum"] = init_checksum
            if mandate_fields:
                body.update(mandate_fields)
            body = {k: v for k, v in body.items() if v not in (None, "")}
            headers = dict(sc._stripe_headers())
            headers.pop("Stripe-Version", None)
            log(
                f"[upi] hosted-minimal confirm amount={amount} zero_due={zero_due} "
                f"layout={layout_name or 'plain'} api={api_version.split(';')[0]} "
                f"checksum={bool(init_checksum)}"
            )
            try:
                resp = http.post(
                    f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
                    data=body,
                    headers=headers,
                    timeout=40,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                log(f"[upi] hosted-minimal 异常：{last_error}")
                continue
            # Adaptive strip of unknown params.
            for _ in range(8):
                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code == 200:
                    break
                unknown = ""
                if hasattr(sc, "_stripe_unknown_param"):
                    unknown = sc._stripe_unknown_param(resp) or ""
                if not unknown:
                    m = re.search(r'"param"\s*:\s*"([^"]+)"', str(getattr(resp, "text", "") or ""))
                    if m:
                        unknown = m.group(1).split(".")[0].split("[")[0]
                if not unknown or unknown in {"_stripe_version", "Stripe-Version", "key"}:
                    break
                log(f"[upi] hosted-minimal 移除不支持参数：{unknown}")
                body = {
                    k: v for k, v in body.items()
                    if k != unknown and not k.startswith(f"{unknown}[")
                }
                try:
                    resp = http.post(
                        f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
                        data=body,
                        headers=headers,
                        timeout=40,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    resp = None
                    break
            if resp is None:
                continue
            if int(getattr(resp, "status_code", 0) or 0) != 200:
                last_error = (
                    f"HTTP {getattr(resp, 'status_code', '?')}: "
                    f"{(getattr(resp, 'text', '') or '')[:240]}"
                )
                log(f"[upi] hosted-minimal 失败：{last_error}")
                continue
            page = resp.json() or {}
            out = extract_provider_result(page, "upi")
            if provider_has_action(out):
                log(f"[upi] hosted-minimal 已产出 QR layout={layout_name or 'plain'}")
                return page
            sub = (page.get("submission_attempt") or {}).get("state") or ""
            # Prefer first 200 that reaches requires_approval / processing —
            # post-approve recover handles QR extraction.
            if sub in {"requires_approval", "processing", "succeeded"} or page.get("setup_intent"):
                log(
                    f"[upi] hosted-minimal 接受 layout={layout_name or 'plain'} "
                    f"submission={sub}"
                )
                return page
            last_error = f"unexpected submission={sub}"
    if last_error:
        raise RuntimeError(f"upi hosted-minimal confirm failed: {last_error[:500]}")
    raise RuntimeError("upi hosted-minimal confirm failed: no successful variant")


def confirm_provider_payment(
    http,
    pk: str,
    session_id: str,
    provider: str,
    init_resp: dict,
    version: str,
    ctx: dict,
    profile: dict,
    log: Callable[[str], None],
    *,
    ideal_bank: str = "",
    payment_method_id: str = "",
) -> dict:
    provider = provider.lower()
    billing = ctx.get("billing") or {}
    addr = billing.get("address") or {}
    common, attr = _runtime_meta(ctx, session_id)
    locale_short = ctx.get("locale") or sc._locale_short(profile)
    total = init_resp.get("total_summary") or {}
    amount = ctx.get("checkout_amount")
    if amount is None:
        amount = total.get("due")
    if amount is None:
        amount = (init_resp.get("invoice") or {}).get("amount_due", 0)

    stripe_hosted_url = ctx.get("stripe_hosted_url") or init_resp.get("stripe_hosted_url") or ""
    success_return_url = ctx.get("return_url") or init_resp.get("return_url") or init_resp.get("url") or ""
    return_url = stripe_hosted_url or success_return_url or "https://chatgpt.com/"

    try:
        exact_zero_due = int(str(amount or 0)) == 0
    except (TypeError, ValueError):
        exact_zero_due = str(amount).strip() in {"0", "0.0", "0.00"}
    if provider == "pix" and payment_method_id and exact_zero_due and ctx.get("pix_hosted_exact"):
        data = {
            "eid": "NA",
            "payment_method": payment_method_id,
            "expected_amount": "0",
            "expected_payment_method_type": "pix",
            "return_url": _hosted_provider_return_url(stripe_hosted_url or return_url, "pix"),
            "_stripe_version": HOSTED_CHECKOUT_STRIPE_VERSION,
            "key": pk,
            "version": HOSTED_CHECKOUT_RUNTIME_VERSION,
            "init_checksum": init_resp.get("init_checksum") or ctx.get("init_checksum") or "",
            "client_attribution_metadata[client_session_id]": attr["client_session_id"],
            "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
            "link_brand": "link",
        }
        data = {key: value for key, value in data.items() if value not in (None, "")}
        log("[stripe] pix confirm strategy=hosted_exact zero_due=True")
        resp = http.post(
            f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
            data=data,
            headers=sc._stripe_headers(),
            timeout=40,
        )
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(
                f"pix hosted confirm [{getattr(resp, 'status_code', '?')}]: "
                f"{(getattr(resp, 'text', '') or '')[:500]}"
            )
        return resp.json()

    data = {
        "guid": common["guid"],
        "muid": common["muid"],
        "sid": common["sid"],
        "expected_amount": str(amount or 0),
        "expected_payment_method_type": provider,
        "key": pk,
        "_stripe_version": sc.STRIPE_VERSION_FULL,
        "init_checksum": init_resp.get("init_checksum", ""),
        "version": common["runtime_version"],
        "return_url": return_url,
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": common["stripe_js_id"],
        "elements_session_client[locale]": locale_short,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[session_id]": common["elements_session_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "client_attribution_metadata[client_session_id]": attr["client_session_id"],
        "client_attribution_metadata[checkout_session_id]": attr["checkout_session_id"],
        "client_attribution_metadata[checkout_config_id]": attr["checkout_config_id"],
        "client_attribution_metadata[elements_session_id]": attr["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": attr["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "payment_method_data[type]": provider,
        "payment_method_data[billing_details][name]": billing.get("name", ""),
        "payment_method_data[billing_details][email]": billing.get("email", ""),
        "payment_method_data[billing_details][address][country]": addr.get("country", "US"),
        "payment_method_data[billing_details][address][line1]": addr.get("line1", ""),
        "payment_method_data[billing_details][address][city]": addr.get("city", ""),
        "payment_method_data[billing_details][address][postal_code]": addr.get("postal_code", ""),
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{common['runtime_version']}; stripe-js-v3/{common['runtime_version']}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 55000)),
    }
    if addr.get("state"):
        data["payment_method_data[billing_details][address][state]"] = addr["state"]
    for key, value in attr.items():
        data[f"payment_method_data[client_attribution_metadata][{key}]"] = value
    data["payment_method_data[client_attribution_metadata][merchant_integration_source]"] = "elements"
    data["payment_method_data[client_attribution_metadata][merchant_integration_subtype]"] = "payment-element"
    data["payment_method_data[client_attribution_metadata][merchant_integration_version]"] = "2021"
    data["payment_method_data[client_attribution_metadata][payment_intent_creation_flow]"] = "deferred"
    data["payment_method_data[client_attribution_metadata][payment_method_selection_flow]"] = "automatic"
    if provider == "ideal" and ideal_bank:
        # iDEAL 2.0 performs issuer selection on the hosted redirect page.
        # Sending the legacy `ideal[bank]` field can make newer PaymentIntent /
        # SetupIntent revisions reject the confirm payload.
        log(f"[ideal] 忽略旧版银行参数 {ideal_bank}，由 iDEAL 跳转页选择银行")
    if billing.get("tax_id"):
        data["payment_method_data[billing_details][tax_id]"] = billing["tax_id"]
    consent = init_resp.get("consent_collection") or {}
    if consent.get("terms_of_service") not in (None, "", "none"):
        data["consent[terms_of_service]"] = "accepted"
    data.update(ctx.get("elements_options_client") or sc._elements_options_client_payload())

    if payment_method_id:
        data["payment_method"] = payment_method_id
        for key in list(data):
            if key.startswith("payment_method_data["):
                data.pop(key, None)

    setup_future_usage = init_resp.get("setup_future_usage_for_payment_method_type") or {}
    if isinstance(setup_future_usage, dict) and provider in setup_future_usage:
        data[f"setup_future_usage_for_payment_method_type[{provider}]"] = setup_future_usage[provider]
    setup_intent = init_resp.get("setup_intent") or {}
    if isinstance(setup_intent, dict) and setup_intent.get("usage"):
        data["setup_intent[usage]"] = setup_intent["usage"]
    payment_method_options = init_resp.get("payment_method_options") or {}
    server_provider_options = extract_server_provider_options(init_resp, provider)
    _merge_nonempty_server_options(
        server_provider_options,
        dict(ctx.get("provider_payment_method_options") or {}),
    )
    if server_provider_options:
        ctx["provider_payment_method_options"] = dict(server_provider_options)
        if payment_method_id:
            data.update(
                flatten_stripe_params(
                    server_provider_options,
                    f"payment_method_options[{provider}]",
                )
            )

    # A zero-due promotional Checkout is a setup/mandate flow.  Keep the
    # provider-specific payment_method_options because PIX/UPI revisions may
    # require mandate_options to produce the QR/action.  The adaptive confirm
    # loop below removes only parameters Stripe explicitly reports as unknown.
    try:
        zero_due = int(str(amount or 0)) == 0
    except (TypeError, ValueError):
        zero_due = str(amount).strip() in {"0", "0.0", "0.00"}
    if zero_due:
        if provider == "ideal":
            setup_usage = (
                (init_resp.get("setup_future_usage_for_payment_method_type") or {}).get("ideal")
                if isinstance(init_resp.get("setup_future_usage_for_payment_method_type"), dict)
                else ""
            ) or ((init_resp.get("setup_intent") or {}).get("usage") if isinstance(init_resp.get("setup_intent"), dict) else "")
            log(
                "[ideal] 0 元 Checkout 使用 SetupIntent；"
                f"setup_usage={setup_usage or 'merchant-default'}，银行选择由 redirect_to_url 完成"
            )
        if provider == "upi":
            server_mandate = dict((server_provider_options.get("mandate_options") or {}))
            ctx["server_upi_mandate_present"] = bool(server_mandate)
            ctx["local_mandate_synthesized"] = False
            log(
                "[upi] 0 元 Checkout 使用 SetupIntent；"
                f"服务端 mandate_options={'present' if server_mandate else 'missing'}。"
                + (
                    "将使用服务端 UPI AutoPay 配置。"
                    if server_mandate
                    else "OpenAI/Stripe 未返回 AutoPay mandate，拒绝提交 confirm。"
                )
            )
            if not server_mandate:
                raise RuntimeError("0 元 UPI Checkout 缺少服务端 AutoPay mandate，拒绝提交 confirm")
        data.pop("elements_options_client[saved_payment_method][enable_save]", None)
        data.pop("elements_options_client[saved_payment_method][enable_redisplay]", None)
        data.pop("client_attribution_metadata[payment_intent_creation_flow]", None)
        if provider in {"pix", "upi"}:
            data.setdefault(
                f"setup_future_usage_for_payment_method_type[{provider}]",
                "off_session",
            )
            data.setdefault("setup_intent[usage]", "off_session")
            provider_options = (
                build_local_mandate_options(
                    provider,
                    ctx,
                    ctx.get("provider_payment_method_options") or server_provider_options,
                )
                if provider == "pix"
                else dict(server_provider_options)
            )
            ctx["provider_payment_method_options"] = provider_options
            ctx["local_mandate_synthesized"] = (
                provider == "pix"
                and not bool(server_provider_options.get("mandate_options") or {})
            )
            mandate_keys = sorted((provider_options.get("mandate_options") or {}).keys())
            log(
                f"[{provider}] 0 元 mandate 配置："
                f"source={'server' if not ctx.get('local_mandate_synthesized') else 'local'} "
                f"keys={','.join(mandate_keys) or '-'} "
                f"amount={(provider_options.get('mandate_options') or {}).get('amount')}"
            )
            # Customer acceptance is required for offline-capable AutoPay /
            # Automático mandates when the Checkout session itself did not
            # already collect it server-side.
            data.setdefault("mandate_data[customer_acceptance][type]", "online")
            data.setdefault(
                "mandate_data[customer_acceptance][online][infer_from_client]",
                "true",
            )
            if payment_method_id:
                # Stripe rejects mixing payment_method with payment_method_data.
                # Pre-created pm_* must carry AutoPay mandate only via
                # payment_method_options / SetupIntent direct confirm.
                data.update(flatten_stripe_params(provider_options, f"payment_method_options[{provider}]"))
                for key in list(data):
                    if key.startswith("payment_method_data["):
                        data.pop(key, None)
            else:
                # Checkout's Payment Page endpoint often rejects root
                # payment_method_options on older revisions. Browser
                # submissions carry provider fields with the inline
                # PaymentMethod data instead.
                data.update(flatten_stripe_params(provider_options, f"payment_method_data[{provider}]"))
            data.pop("_stripe_version", None)
    option_keys = sorted(
        key for key in data
        if key.startswith("payment_method_options[")
        or key.startswith(f"payment_method_data[{provider}][mandate_options]")
    )
    log(
        f"[stripe] {provider} confirm strategy={'pm_id' if payment_method_id else 'inline'} "
        f"zero_due={zero_due} setup_usage={data.get('setup_intent[usage]', '')} "
        f"provider_options={len(option_keys)}"
    )

    resp = None
    confirm_headers = dict(sc._stripe_headers())
    if zero_due and provider in {"pix", "upi"}:
        confirm_headers["Stripe-Version"] = LOCAL_MANDATE_STRIPE_VERSION_FULL
    for _ in range(5):
        resp = http.post(
            f"{sc.STRIPE_API}/v1/payment_pages/{session_id}/confirm",
            data=data,
            headers=confirm_headers,
            timeout=40,
        )
        if getattr(resp, "status_code", 0) == 200:
            break
        unknown_param = sc._stripe_unknown_param(resp)
        if not unknown_param:
            break
        removed = False
        for key in list(data):
            if key == unknown_param or key.startswith(f"{unknown_param}["):
                data.pop(key, None)
                removed = True
        if not removed:
            break
        log(f"[stripe] confirm 移除当前版本不支持参数：{unknown_param}")
    if resp is None or getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(
            f"{provider} confirm [{getattr(resp, 'status_code', '?')}]: "
            f"{(getattr(resp, 'text', '') or '')[:500]}"
        )
    return resp.json()


def enrich_ideal_redirect(http, redirect_url: str, log: Callable[[str], None]) -> dict[str, Any]:
    """Resolve the iDEAL hosted page and extract its Canvas QR payload."""
    if not redirect_url:
        return {}
    try:
        page = http.get(
            redirect_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": sc.CHROME_UA,
            },
            timeout=45,
            allow_redirects=True,
        )
        final_url = str(getattr(page, "url", "") or redirect_url)
        parsed = urlsplit(final_url)
        prefix = "/transactions/"
        if parsed.netloc.lower() != "pay.ideal.nl" or prefix not in parsed.path:
            log(f"[ideal] 跳转页已生成：{parsed.netloc or 'unknown-host'}")
            return {"provider_redirect_url": final_url}
        transaction_path = parsed.path.split(prefix, 1)[1]
        api_url = f"https://pay.ideal.nl/api/v1/transactions/{transaction_path}/initiate"
        init_resp = http.post(
            api_url,
            json={
                "deviceInfo": {
                    "language": "nl-NL",
                    "timeZone": "Europe/Amsterdam",
                    "screenWidth": 1280,
                    "screenHeight": 720,
                    "screenAvailableWidth": 1280,
                    "screenAvailableHeight": 720,
                    "colorDepth": 24,
                },
                "httpReferrer": "https://some-referrer",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://pay.ideal.nl",
                "Referer": final_url,
                "User-Agent": sc.CHROME_UA,
            },
            timeout=45,
        )
        if getattr(init_resp, "status_code", 0) != 200:
            log(f"[ideal] 二维码初始化 HTTP {getattr(init_resp, 'status_code', '?')}，保留支付页链接")
            return {"provider_redirect_url": final_url}
        payload = init_resp.json() or {}
        qr_url = str(payload.get("qrCodeUrl") or "")
        issuers = payload.get("supportedIssuers") or []
        result: dict[str, Any] = {
            "provider_redirect_url": final_url,
            "ideal_qr_url": qr_url,
            "qr_data": qr_url,
            "ideal_creditor_name": str(payload.get("creditorName") or ""),
            "ideal_amount": payload.get("amount"),
            "ideal_transaction_flow": str(payload.get("transactionFlow") or ""),
            "ideal_supported_issuers": [
                {
                    "id": str(item.get("id") or ""),
                    "deeplink": str(item.get("deeplink") or ""),
                    "availability": str(item.get("availabilityStatus") or ""),
                }
                for item in issuers
                if isinstance(item, dict)
            ],
        }
        if qr_url:
            try:
                import qrcode
                image = qrcode.make(qr_url)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                result["qr_image_png"] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            except Exception as exc:
                log(f"[ideal] 二维码图片生成提示：{type(exc).__name__}")
            log(f"[ideal] 二维码已提取，银行入口 {len(result['ideal_supported_issuers'])} 个")
        else:
            log("[ideal] 支付页已打开，但初始化响应未包含 qrCodeUrl")
        return result
    except Exception as exc:
        log(f"[ideal] 二维码提取提示：{type(exc).__name__}: {str(exc)[:180]}")
        return {"provider_redirect_url": redirect_url}


def _next_action_with_source(data: dict) -> tuple[dict, str]:
    """Select one action and its owning response object using Stripe's priority."""
    for key in ("next_action", "action"):
        action = data.get(key)
        if isinstance(action, dict) and action:
            return action, "payment_page"

    elements_session = data.get("elements_session") or {}
    if isinstance(elements_session, dict):
        for key in ("action", "next_action"):
            action = elements_session.get(key)
            if isinstance(action, dict) and action:
                return action, "elements_session"

    submission = data.get("submission_attempt") or {}
    if isinstance(submission, dict):
        for key in ("next_action", "action"):
            action = submission.get(key)
            if isinstance(action, dict) and action:
                return action, "submission_attempt"

    for intent_key in ("payment_intent", "setup_intent"):
        intent = data.get(intent_key) or {}
        if isinstance(intent, dict):
            action = intent.get("next_action")
            if isinstance(action, dict) and action:
                return action, intent_key

    payment_method_object = data.get("payment_method_object") or {}
    if isinstance(payment_method_object, dict):
        setup_intent = payment_method_object.get("setup_intent") or {}
        if isinstance(setup_intent, dict):
            action = setup_intent.get("next_action")
            if isinstance(action, dict) and action:
                return action, "setup_intent"

    action = sc._find_next_action(data)
    return (action, "unknown") if action else ({}, "")


def extract_provider_result(data: dict, provider: str) -> dict[str, Any]:
    provider = provider.lower()
    na, next_action_intent_kind = _next_action_with_source(data)
    redirect = (na.get("redirect_to_url") or {}).get("url") or ""
    if not redirect:
        redirect = sc.extract_redirect_url(data)
    result: dict[str, Any] = {
        "provider": provider,
        "provider_redirect_url": redirect,
        "next_action_type": na.get("type") or "",
        "next_action_intent_kind": next_action_intent_kind,
    }
    if provider == "pix":
        qr = na.get("pix_display_qr_code") or {}
        result.update({
            "provider_redirect_url": qr.get("hosted_instructions_url") or redirect,
            "qr_data": qr.get("data") or "",
            "qr_image_png": qr.get("image_url_png") or "",
            "qr_image_svg": qr.get("image_url_svg") or "",
            "expires_at": qr.get("expires_at"),
        })
    elif provider == "upi":
        upi = na.get("upi_handle_redirect_or_display_qr_code") or {}
        qr = upi.get("qr_code") or {}
        result.update({
            "provider_redirect_url": upi.get("hosted_instructions_url") or redirect,
            "qr_image_png": qr.get("image_url_png") or "",
            "qr_image_svg": qr.get("image_url_svg") or "",
            "expires_at": qr.get("expires_at"),
        })
    # Only accept Stripe next_action.redirect_to_url (or the generic redirect
    # extractor). A broad search for the word "ideal" also matches static
    # assets such as icon-pm-ideal@3x.png and creates a false success.
    return result


def stripe_to_provider(
    http,
    session_id: str,
    provider: str,
    *,
    billing: dict,
    country: str,
    promotion_billing: dict | None = None,
    payment_billing: dict | None = None,
    payment_http=None,
    chatgpt_http=None,
    access_token: str = "",
    stage1: dict | None = None,
    approve_callback=None,
    apply_promo_callback=None,
    ideal_bank: str = "",
    require_zero_due: bool = False,
    local_method_strategy: str = "standalone",
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    provider = provider.lower()
    local_method_strategy = str(local_method_strategy or "standalone").lower()
    late_promo = provider in {"pix", "upi"} and local_method_strategy == "late_promo"
    stage1 = stage1 or {}
    if provider == "paypal":
        redirect, pk, paypal_ctx = sc.stripe_to_paypal_redirect(
            http,
            session_id,
            billing=billing,
            promotion_billing=promotion_billing,
            payment_billing=payment_billing,
            payment_http=payment_http,
            country=country,
            chatgpt_http=chatgpt_http,
            access_token=access_token,
            publishable_key=str(stage1.get("publishable_key") or ""),
            processor_entity=str(stage1.get("processor_entity") or ""),
            approve_callback=approve_callback,
            apply_promo_callback=apply_promo_callback,
            require_zero_due=require_zero_due,
            log=log,
        )
        return {
            "provider": provider,
            "provider_redirect_url": redirect,
            "stripe_redirect_url": paypal_ctx.get("stripe_redirect_url") or "",
            "payment_method_types": paypal_ctx.get("payment_method_types") or [],
            "processor_entity": paypal_ctx.get("processor_entity") or stage1.get("processor_entity") or "",
            "stripe_publishable_key": pk,
            "checkout_amount": paypal_ctx.get("checkout_amount"),
            "checkout_currency": str(paypal_ctx.get("currency") or "").upper(),
            "paypal_billing_country": str(paypal_ctx.get("paypal_billing_country") or "").upper(),
            "promo_requested": require_zero_due,
            "promo_applied": paypal_ctx.get("promo_applied") if require_zero_due else None,
        }

    profile = sc._profile(country)
    pk = str(stage1.get("publishable_key") or "") or sc.verify_pk(http, session_id, log)
    init_data, version, ctx = sc.init_checkout(http, session_id, pk, profile, log)
    stash_setup_intent_context(ctx, init_data)
    merge_server_provider_options(ctx, provider, init_data, reset=True)
    methods = ctx.get("payment_method_types") or []
    if provider not in methods:
        raise RuntimeError(f"当前 checkout 未开放 {provider}，可用方式：{', '.join(methods) or 'card'}")
    elements_data = sc.fetch_elements_session(http, pk, session_id, ctx, version, profile, log)
    merge_server_provider_options(ctx, provider, elements_data)
    processor = str(stage1.get("processor_entity") or "") or sc._entity_from_return_url(ctx.get("return_url") or init_data.get("return_url") or "") or "openai_llc"

    def sync_billing_context() -> dict:
        ctx["billing"] = billing
        tax_data = sc.update_tax_region(http, session_id, pk, version, ctx, billing, profile, log)
        merge_server_provider_options(ctx, provider, tax_data)
        sc.snapshot_billing(chatgpt_http, access_token, session_id, processor, billing, log)
        return tax_data

    # The Checkout and ChatGPT snapshot must already carry the local billing
    # address before promotion eligibility is recalculated.
    sync_billing_context()
    if apply_promo_callback and not late_promo:
        original_checkout_amount = ctx.get("checkout_amount")
        already_zero = amount_is_zero(original_checkout_amount)
        if already_zero:
            log("[promo] Checkout 创建时金额已为 0，保留 Stage1 原生 mandate 配置")
            ctx["original_checkout_amount"] = original_checkout_amount
        else:
            apply_promo_callback(processor)
            log("[promo] 优惠更新完成，重新初始化 Stripe 并获取新鲜 elements_session_id")
            init_data, version, ctx = sc.init_checkout(http, session_id, pk, profile, log)
            ctx["original_checkout_amount"] = original_checkout_amount
            stash_setup_intent_context(ctx, init_data)
            merge_server_provider_options(ctx, provider, init_data, reset=True)
            methods = ctx.get("payment_method_types") or []
            if provider not in methods:
                raise RuntimeError(f"应用优惠后 checkout 未开放 {provider}，可用方式：{', '.join(methods) or 'card'}")
            elements_data = sc.fetch_elements_session(http, pk, session_id, ctx, version, profile, log)
            merge_server_provider_options(ctx, provider, elements_data)
            sync_billing_context()
    if provider == "pix" and require_zero_due and not late_promo:
        original_checkout_amount = ctx.get("original_checkout_amount")
        init_data, version, ctx = _hosted_checkout_init(
            http, session_id, pk, profile, ctx, log
        )
        ctx["original_checkout_amount"] = original_checkout_amount
        methods = ctx.get("payment_method_types") or []
        if "pix" not in methods:
            raise RuntimeError(
                f"PIX hosted init 未开放 pix，可用方式：{', '.join(methods) or 'card'}"
            )
        sync_billing_context()
    checkout_amount = ctx.get("checkout_amount")
    if ctx.get("original_checkout_amount") in (None, "", 0, "0"):
        ctx["original_checkout_amount"] = checkout_amount
    promo_applied = None

    if require_zero_due:
        if late_promo:
            if not _amount_is_positive(checkout_amount):
                raise RuntimeError("late_promo_requires_positive_initial_amount")
            log(f"[promo] 本轮延后到 PaymentMethod 挂载后应用优惠，当前 amount={checkout_amount}")
        else:
            if checkout_amount is None:
                raise RuntimeError("优惠金额校验失败：Stripe 未返回今日应付金额")
            promo_applied = amount_is_zero(checkout_amount)
            if not promo_applied:
                raise RuntimeError(f"Plus 首月免费优惠未生效：Stripe 今日应付 amount={checkout_amount}")
            if provider == "upi":
                server_upi = merge_server_provider_options(
                    ctx,
                    "upi",
                    init_data if isinstance(init_data, dict) else {},
                )
                server_mandate = dict((server_upi.get("mandate_options") or {}))
                ctx["server_upi_mandate_present"] = bool(server_mandate)
                ctx["local_mandate_synthesized"] = False
                if server_mandate:
                    ctx["provider_payment_method_options"] = dict(server_upi)
                    log(
                        "[upi] Checkout 已归零且服务端返回 UPI AutoPay mandate；"
                        f"amount={server_mandate.get('amount') or resolve_mandate_amount('upi', ctx)}"
                    )
                else:
                    ctx["provider_payment_method_options"] = dict(server_upi)
                    log(
                        "[upi] Checkout 已归零，但 OpenAI/Stripe 未返回 UPI AutoPay mandate；"
                        "当前周期无法安全提交 0 元 confirm"
                    )
                    raise RuntimeError(
                        "0 元 UPI Checkout 缺少服务端 AutoPay mandate；"
                        "当前 Checkout 无法创建 UPI AutoPay"
                    )
            if provider == "pix":
                log("[promo] 第 5/7 步：返回 BR 主链路并校验通过，Stripe 今日应付 amount=0")
            else:
                log("[promo] Plus 首月免费校验通过：Stripe 今日应付 amount=0")
    if provider == "pix":
        log("[pix] 第 6/7 步：创建独立 PIX PaymentMethod")
    elif provider == "upi":
        if local_method_strategy == "hosted_minimal":
            log("[upi] 本轮使用 hosted-minimal 提交（对齐 OpenAI 托管 UPI 提取器）")
        else:
            log("[upi] 正在创建独立 UPI PaymentMethod")
    payment_method_id = ""
    create_standalone_method = (
        provider in {"pix", "upi"}
        and (
            local_method_strategy == "standalone"
            or (provider == "upi" and late_promo)
        )
    )
    if create_standalone_method:
        payment_method_id = create_provider_payment_method(
            http,
            pk,
            session_id,
            provider,
            version,
            ctx,
            billing,
            log,
        )
    elif provider in {"pix", "upi"} and local_method_strategy not in {"hosted_minimal"}:
        log(f"[stripe] {provider} 本轮使用 {local_method_strategy} inline PaymentMethod 提交")
    if provider == "upi" and local_method_strategy == "hosted_minimal":
        confirm = confirm_upi_hosted_minimal(
            http, pk, session_id, init_data, ctx, billing, log,
        )
    else:
        confirm = confirm_provider_payment(
            http, pk, session_id, provider, init_data, version, ctx, profile, log,
            ideal_bank=ideal_bank,
            payment_method_id=payment_method_id,
        )
    stash_setup_intent_context(ctx, confirm)
    initial_submission = confirm.get("submission_attempt") or {}
    initial_setup = confirm.get("setup_intent") or {}
    initial_action = sc._find_next_action(confirm)
    log(
        f"[{provider}] confirm 返回：submission_state={initial_submission.get('state') or ''} "
        f"setup_status={initial_setup.get('status') or ''} "
        f"next_action={initial_action.get('type') or ''} "
        f"failure={provider_failure_detail(confirm)}"
    )
    if not payment_method_id:
        for source in (initial_setup, initial_submission, confirm):
            if not isinstance(source, dict):
                continue
            cand = source.get("payment_method")
            if isinstance(cand, dict):
                cand = cand.get("id")
            cand = str(cand or "")
            if cand.startswith("pm_"):
                payment_method_id = cand
                ctx["payment_method_id"] = cand
                log(f"[{provider}] 从 confirm 回填 payment_method={cand}")
                break
    if not payment_method_id:
        # Payment Page sometimes nests the pm under submission_attempt.payment_method_types
        # objects or only exposes it after a soft-failed approve poll.
        walk_targets = [confirm, initial_submission, initial_setup]
        for node in walk_targets:
            if not isinstance(node, dict):
                continue
            raw = json.dumps(node, ensure_ascii=False)
            match = re.search(r"\bpm_(?:[A-Za-z0-9]+)\b", raw)
            if match:
                payment_method_id = match.group(0)
                ctx["payment_method_id"] = payment_method_id
                log(f"[{provider}] 从 confirm JSON 扫描 payment_method={payment_method_id}")
                break
    if payment_method_id:
        ctx["payment_method_id"] = payment_method_id
    stash_setup_intent_context(ctx, confirm)
    out = extract_provider_result(confirm, provider)

    def try_setup_intent_recover(stage: str) -> None:
        nonlocal confirm, out
        if provider not in {"upi", "pix"} or provider_has_action(out):
            return
        setup_status = str(((confirm.get("setup_intent") or {}).get("status") or "")).lower()
        decline = payment_decline(confirm)
        failure_detail = provider_failure_detail(confirm)
        need_setup_recover = (
            setup_status in {"requires_payment_method", "requires_confirmation", "requires_action", ""}
            or bool(decline)
            or "requires_payment_method" in failure_detail
            or "generic_decline" in failure_detail
            or (
                provider == "upi"
                and (
                    bool(ctx.get("local_mandate_synthesized"))
                    or not bool(ctx.get("server_upi_mandate_present"))
                )
            )
        )
        if not need_setup_recover:
            return
        recover_pm = (
            payment_method_id
            or str(ctx.get("payment_method_id") or "")
            or extract_payment_method_id(confirm, confirm.get("setup_intent") or {})
        )
        if provider == "upi" and not (ctx.get("provider_payment_method_options") or {}).get("mandate_options"):
            log(f"[upi] {stage} 缺少服务端 AutoPay mandate，跳过 SetupIntent 补交")
            return
        setup_id, client_secret = resolve_setup_intent_credentials(confirm, ctx)
        log(
            f"[{provider}] {stage} SetupIntent 未产出动作，"
            f"尝试直连补交 setup={bool(setup_id)} secret={bool(client_secret)} "
            f"pm={recover_pm[:18] + '…' if str(recover_pm).startswith('pm_') else False} "
            f"setup_status={setup_status or '-'} local_mandate={bool(ctx.get('local_mandate_synthesized'))}"
        )
        confirm = confirm_local_setup_intent(
            http,
            pk,
            provider,
            confirm,
            recover_pm,
            ctx,
            log,
        )
        out = extract_provider_result(confirm, provider)
        if provider_has_action(out):
            log(f"[{provider}] {stage} SetupIntent 补交已产出 next_action/QR")
        failure_detail = provider_failure_detail(confirm)
        if failure_detail and not provider_has_action(out):
            log(f"[{provider}] SetupIntent 补交后详情：{failure_detail}")

    if not provider_has_action(out):
        sub = confirm.get("submission_attempt") or {}
        if sub.get("state") == "requires_approval" and approve_callback:
            approval_needed = True
            zero_reconfirm_before_approval = False
            if late_promo and apply_promo_callback:
                log(f"[{provider}] PaymentMethod 已挂载，开始延后应用优惠")
                original_checkout_amount = ctx.get("original_checkout_amount") or checkout_amount
                apply_promo_callback(processor)
                promo_init, promo_version, promo_ctx = sc.init_checkout(
                    http, session_id, pk, profile, log,
                )
                promo_ctx["original_checkout_amount"] = original_checkout_amount
                promo_ctx["billing"] = billing
                if payment_method_id:
                    promo_ctx["payment_method_id"] = payment_method_id
                stash_setup_intent_context(promo_ctx, promo_init)
                merge_server_provider_options(
                    promo_ctx,
                    provider,
                    promo_init if isinstance(promo_init, dict) else {},
                    reset=True,
                )
                promo_methods = promo_ctx.get("payment_method_types") or []
                if provider not in promo_methods:
                    log(
                        f"[{provider}] 归零后可选方式不再列出 {provider}；"
                        "继续处理归零前已挂载的 PaymentMethod"
                    )
                promo_elements = sc.fetch_elements_session(
                    http,
                    pk,
                    session_id,
                    promo_ctx,
                    promo_version,
                    profile,
                    log,
                )
                merge_server_provider_options(promo_ctx, provider, promo_elements)
                promo_tax = sc.update_tax_region(
                    http, session_id, pk, promo_version, promo_ctx, billing, profile, log,
                )
                merge_server_provider_options(promo_ctx, provider, promo_tax)
                sc.snapshot_billing(
                    chatgpt_http, access_token, session_id, processor, billing, log,
                )
                promo_amount = promo_ctx.get("checkout_amount")
                promo_applied = amount_is_zero(promo_amount)
                if not promo_applied:
                    raise RuntimeError(f"延后应用优惠未归零：Stripe 今日应付 amount={promo_amount}")
                checkout_amount = promo_amount
                init_data = promo_init
                version = promo_version
                ctx = promo_ctx
                methods = promo_methods or methods
                if provider == "upi":
                    server_upi = merge_server_provider_options(
                        ctx,
                        "upi",
                        promo_init if isinstance(promo_init, dict) else {},
                    )
                    server_mandate = dict(server_upi.get("mandate_options") or {})
                    ctx["server_upi_mandate_present"] = bool(server_mandate)
                    ctx["local_mandate_synthesized"] = False
                    if not server_mandate:
                        log(
                            "[upi] 延后优惠归零后仍缺少服务端 AutoPay mandate；"
                            "继续使用归零前已挂载的 PaymentMethod 执行 approval/poll"
                        )
                log(f"[{provider}] 延后优惠金额校验通过：Stripe 今日应付 amount=0")
                if provider == "upi":
                    log("[upi] approval 前使用原 PaymentMethod 重建零金额 submission")
                    zero_confirm = recover_upi_via_payment_page(
                        http,
                        pk,
                        session_id,
                        init_data if isinstance(init_data, dict) else {},
                        ctx,
                        billing,
                        profile,
                        version,
                        log,
                        approve_callback=None,
                        processor=processor,
                    )
                    if not zero_confirm:
                        raise RuntimeError("upi_zero_reconfirm_failed_before_approval")
                    confirm = zero_confirm
                    stash_setup_intent_context(ctx, confirm)
                    out = extract_provider_result(confirm, provider)
                    zero_reconfirm_before_approval = True
                    zero_submission_state = str(
                        ((confirm.get("submission_attempt") or {}).get("state") or "")
                    )
                    if provider_has_action(out):
                        approval_needed = False
                        log("[upi] 零金额重确认已直接产出 UPI QR/跳转")
                    elif zero_submission_state == "requires_approval":
                        approval_needed = True
                        log("[upi] 零金额 submission 已就绪，开始 approval")
                    elif zero_submission_state in {"processing", "succeeded"}:
                        approval_needed = False
                        log(
                            "[upi] 零金额 submission 无需 approval，"
                            f"当前 state={zero_submission_state}"
                        )
                    else:
                        detail = provider_failure_detail(confirm)
                        raise RuntimeError(
                            "upi_zero_reconfirm_failed_before_approval:"
                            f"{detail or zero_submission_state or 'missing_submission_state'}"
                        )
            if approval_needed:
                approve_callback(processor)
            # Zero-only recovery (fast):
            # 1) poll the current submission once for QR
            # 2) re-init for last_setup_error.pm
            # 3) only legacy paths get one post-approval Payment Page re-confirm
            if not provider_has_action(out):
                confirm = sc.poll_payment_page_after_approve(
                    http,
                    pk,
                    session_id,
                    log,
                    ctx=ctx,
                    max_attempts=4,
                )
                stash_setup_intent_context(ctx, confirm)
                out = extract_provider_result(confirm, provider)
            if provider == "upi" and not provider_has_action(out):
                try:
                    previous_ctx = ctx
                    reinit_data, reinit_version, reinit_ctx = sc.init_checkout(
                        http, session_id, pk, profile, log,
                    )
                    reinit_ctx["billing"] = billing
                    reinit_ctx["original_checkout_amount"] = previous_ctx.get(
                        "original_checkout_amount",
                        checkout_amount,
                    )
                    if payment_method_id:
                        reinit_ctx["payment_method_id"] = payment_method_id
                    stash_setup_intent_context(reinit_ctx, reinit_data)
                    merge_server_provider_options(
                        reinit_ctx,
                        provider,
                        reinit_data if isinstance(reinit_data, dict) else {},
                        reset=True,
                    )
                    reinit_elements = sc.fetch_elements_session(
                        http,
                        pk,
                        session_id,
                        reinit_ctx,
                        reinit_version,
                        profile,
                        log,
                    )
                    merge_server_provider_options(reinit_ctx, provider, reinit_elements)
                    reinit_tax = sc.update_tax_region(
                        http,
                        session_id,
                        pk,
                        reinit_version,
                        reinit_ctx,
                        billing,
                        profile,
                        log,
                    )
                    reinit_options = merge_server_provider_options(
                        reinit_ctx,
                        provider,
                        reinit_tax,
                    )
                    if provider == "upi":
                        reinit_ctx["server_upi_mandate_present"] = bool(
                            reinit_options.get("mandate_options")
                        )
                        reinit_ctx["local_mandate_synthesized"] = False

                    # Re-init is a new transaction snapshot. Carry durable ids
                    # through ctx, but never inherit an old action or error.
                    confirm = dict(reinit_data)
                    init_data = reinit_data
                    version = reinit_version
                    ctx = reinit_ctx
                    checkout_amount = reinit_ctx.get("checkout_amount")
                    methods = reinit_ctx.get("payment_method_types") or methods
                    recovered_pm = extract_payment_method_id(reinit_data, confirm)
                    if recovered_pm:
                        payment_method_id = recovered_pm
                        ctx["payment_method_id"] = recovered_pm
                        log(f"[upi] re-init 回填 payment_method={recovered_pm}")
                    stash_setup_intent_context(ctx, confirm)
                    reinit_out = extract_provider_result(confirm, provider)
                    if provider_has_action(reinit_out):
                        out = reinit_out
                        log("[upi] approval 后 re-init 已提取 UPI QR/跳转")
                    else:
                        out = reinit_out
                except Exception as exc:  # noqa: BLE001
                    log(f"[upi] approval 后 re-init 提示：{type(exc).__name__}: {exc}")
            if not payment_method_id:
                payment_method_id = extract_payment_method_id(
                    confirm.get("setup_intent") or {},
                    confirm.get("submission_attempt") or {},
                    confirm,
                )
                if payment_method_id:
                    ctx["payment_method_id"] = payment_method_id
                    log(f"[{provider}] approval 后回填 payment_method={payment_method_id}")
            if not provider_has_action(out):
                out = extract_provider_result(confirm, provider)
            decline = payment_decline(confirm)
            failure_detail = provider_failure_detail(confirm)
            if failure_detail:
                log(f"[{provider}] approval 后失败详情：{failure_detail}")
            if (
                provider == "upi"
                and not provider_has_action(out)
                and not zero_reconfirm_before_approval
            ):
                log("[upi] 改走 Payment Page 补交（Checkout SetupIntent 不可直连 confirm）")
                recovered = recover_upi_via_payment_page(
                    http,
                    pk,
                    session_id,
                    init_data if isinstance(init_data, dict) else {},
                    ctx,
                    billing,
                    profile,
                    version,
                    log,
                    approve_callback=approve_callback,
                    processor=processor,
                )
                if recovered:
                    confirm = recovered
                    out = extract_provider_result(confirm, provider)
            elif provider != "upi":
                try_setup_intent_recover("approval 后")
            if decline and provider == "pix" and not provider_has_action(out):
                log("[pix] approval 后原始 PaymentMethod 被支付通道拒绝，交给外层更换代理、CPF 并重建完整链路")
        else:
            # Zero-due UPI AutoPay may skip requires_approval yet still leave
            # SetupIntent without next_action.
            if provider == "upi":
                log("[upi] confirm 后无 QR，改走 Payment Page 补交")
                recovered = recover_upi_via_payment_page(
                    http,
                    pk,
                    session_id,
                    init_data if isinstance(init_data, dict) else {},
                    ctx,
                    billing,
                    profile,
                    version,
                    log,
                    approve_callback=approve_callback,
                    processor=processor,
                )
                if recovered:
                    confirm = recovered
                    out = extract_provider_result(confirm, provider)
            else:
                try_setup_intent_recover("confirm 后")
    terminal_failure = provider_terminal_failure_detail(confirm)
    if terminal_failure:
        raise RuntimeError(f"{provider} 支付通道拒绝：{terminal_failure}")
    if provider == "upi" and require_zero_due:
        if promo_applied is not True:
            raise RuntimeError(
                "UPI 优惠链路未完成归零校验："
                "late_promo_requires_approval_before_promo"
            )
        latest_amount = ctx.get("checkout_amount")
        if not amount_is_zero(latest_amount):
            raise RuntimeError("upi_latest_checkout_amount_not_zero")
        checkout_amount = latest_amount
        if (
            provider_has_action(out)
            and str(out.get("next_action_intent_kind") or "") != "setup_intent"
        ):
            raise RuntimeError(
                "upi_zero_due_action_not_setup_intent:"
                f"{out.get('next_action_intent_kind') or 'unknown'}"
            )
    out.update({
        "payment_method_types": ctx.get("payment_method_types") or methods,
        "processor_entity": processor,
        "stripe_publishable_key": pk,
        "checkout_amount": checkout_amount,
        "checkout_currency": str(ctx.get("currency") or "").upper(),
        "promo_requested": require_zero_due,
        "promo_applied": promo_applied,
        "upi_mandate_available": (
            bool((ctx.get("provider_payment_method_options") or {}).get("mandate_options"))
            if provider == "upi"
            else None
        ),
        "upi_mandate_source": (
            ("server" if ctx.get("server_upi_mandate_present") else "missing")
            if provider == "upi"
            else None
        ),
    })
    if provider == "ideal" and out.get("provider_redirect_url"):
        out.update(enrich_ideal_redirect(http, str(out.get("provider_redirect_url") or ""), log))
    if not provider_has_action(out):
        decline = payment_decline(confirm)
        failure_detail = provider_failure_detail(confirm)
        if provider == "upi" and require_zero_due and promo_applied:
            raise RuntimeError(
                "UPI 归零后的 confirm/approval/poll 未产出二维码或跳转："
                f"{failure_detail or 'zero_due_without_upi_result'}"
            )
        if decline or failure_detail:
            raise RuntimeError(
                f"{provider} 支付通道拒绝："
                f"{provider_failure_detail(confirm) or decline.get('message') or decline.get('decline_code') or 'provider decline'}"
            )
        if provider == "ideal":
            setup_status = str(((confirm.get("setup_intent") or {}).get("status") or ""))
            submission_state = str(((confirm.get("submission_attempt") or {}).get("state") or ""))
            raise RuntimeError(
                "iDEAL 未返回银行跳转地址："
                f"setup_status={setup_status or '-'}; submission_state={submission_state or '-'}"
            )
        raise RuntimeError(f"{provider} 未返回跳转或二维码：{json.dumps(confirm, ensure_ascii=False)[:500]}")
    return out
