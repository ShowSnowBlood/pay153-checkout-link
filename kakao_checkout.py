"""Isolated adapter for the repository-local Kakao checkout worker.

The worker owns the provider-specific Stripe protocol.  This module keeps the
application task runner small and, importantly, executes each request in a
short-lived process so the worker's deadline state cannot leak between jobs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from kakao_worker import PROVIDER_ALLOWED_HOSTS


WORKER_PATH = Path(__file__).with_name("kakao_worker.py")


def _configured_timeout() -> float:
    try:
        value = float(os.getenv("PAY153_KAKAO_WORKER_TIMEOUT", "180") or 180)
    except (TypeError, ValueError):
        value = 180.0
    return max(30.0, value)


DEFAULT_OPERATION_TIMEOUT = _configured_timeout()
MAX_OPERATION_WINDOW = timedelta(minutes=15)
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$",
    re.IGNORECASE,
)


class KakaoWorkerError(RuntimeError):
    """A sanitized, structured failure returned by the Kakao worker."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        transient: bool = True,
        stage: str = "",
        http_status: int | None = None,
    ) -> None:
        self.code = str(code or "kakao_extraction_failed")[:120]
        self.message = str(message or "Kakao extraction failed")[:500]
        self.transient = bool(transient)
        self.stage = str(stage or "")[:120]
        try:
            self.http_status = int(http_status) if http_status else None
        except (TypeError, ValueError):
            self.http_status = None
        super().__init__(self.safe_message)

    @property
    def safe_message(self) -> str:
        # Codes are useful for retry diagnostics; the worker never receives
        # user-visible credentials in its error payload, but keep this guard
        # in the adapter in case a future worker adds a verbose message.
        return f"{self.code}: {self.message}"

    @classmethod
    def from_payload(cls, payload: Any) -> "KakaoWorkerError":
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return cls("kakao_extraction_failed", "Kakao worker returned an invalid error")
        return cls(
            str(error.get("code") or "kakao_extraction_failed"),
            str(error.get("message") or "Kakao extraction failed"),
            transient=bool(error.get("transient", True)),
            stage=str(error.get("stage") or ""),
            http_status=error.get("http_status"),
        )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _worker_deadline(timeout: float) -> tuple[str, str, float]:
    now = datetime.now(timezone.utc)
    # Leave a small amount of headroom for process startup and JSON parsing,
    # while staying inside the worker's documented 15-minute request window.
    seconds = max(15.0, min(float(timeout), MAX_OPERATION_WINDOW.total_seconds() - 5.0))
    deadline = now + timedelta(seconds=seconds)
    return _timestamp(now), _timestamp(deadline), seconds + 8.0


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def _run_worker(
    request: dict[str, Any],
    *,
    timeout: float = DEFAULT_OPERATION_TIMEOUT,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if not WORKER_PATH.is_file():
        raise KakaoWorkerError("kakao_worker_missing", "Kakao worker is not installed", transient=False)
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
    try:
        process: subprocess.Popen[str] = subprocess.Popen(
            [sys.executable, str(WORKER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(WORKER_PATH.parent),
        )
    except OSError as exc:
        raise KakaoWorkerError(
            "kakao_worker_start_failed",
            "Kakao worker could not be started",
            transient=True,
        ) from exc

    started = time.monotonic()
    try:
        # Send the bounded request once, then poll so a cancelled web task can
        # stop the provider process instead of waiting for its 120s redirect
        # poll to finish.
        assert process.stdin is not None
        process.stdin.write(payload)
        process.stdin.close()
        # ``communicate`` attempts to flush ``stdin`` when it is present;
        # mark it detached after the one bounded write so the final drain is
        # safe on both CPython and Windows.
        process.stdin = None  # type: ignore[assignment]
        while process.poll() is None:
            if cancel_check and cancel_check():
                _terminate_process(process)
                raise KakaoWorkerError("kakao_cancelled", "Kakao extraction was cancelled", transient=False)
            if time.monotonic() - started >= max(1.0, float(timeout)):
                _terminate_process(process)
                raise KakaoWorkerError("kakao_worker_timeout", "Kakao worker timed out", transient=True)
            time.sleep(0.1)
        stdout = process.stdout.read() if process.stdout is not None else ""
        _stderr = process.stderr.read() if process.stderr is not None else ""
    except KakaoWorkerError:
        raise
    except Exception as exc:
        _terminate_process(process)
        raise KakaoWorkerError(
            "kakao_worker_io_failed",
            "Kakao worker I/O failed",
            transient=True,
        ) from exc

    if process.returncode not in (0, 1):
        raise KakaoWorkerError(
            "kakao_worker_process_failed",
            "Kakao worker exited unexpectedly",
            transient=True,
        )
    try:
        result = json.loads(stdout or "")
    except (TypeError, ValueError) as exc:
        raise KakaoWorkerError(
            "kakao_worker_invalid_output",
            "Kakao worker returned invalid JSON",
            transient=True,
        ) from exc
    if not isinstance(result, dict):
        raise KakaoWorkerError("kakao_worker_invalid_output", "Kakao worker returned an invalid response")
    if result.get("ok") is not True:
        raise KakaoWorkerError.from_payload(result)
    value = result.get("result")
    if not isinstance(value, dict):
        raise KakaoWorkerError("kakao_worker_invalid_output", "Kakao worker returned no result")
    return value


def _epoch_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        return datetime.fromisoformat(normalized).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _is_allowed_provider_url(value: Any) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and not parsed.username
        and not parsed.password
        and bool(host)
        and host in PROVIDER_ALLOWED_HOSTS
        and port in (None, 443)
    )


def is_allowed_kakao_url(value: Any) -> bool:
    """Return whether a URL is a final HTTPS Kakao/NicePay destination."""
    return _is_allowed_provider_url(value)


def is_allowed_kakao_qr(value: Any) -> bool:
    """Validate URL QR text or a bounded inline image payload."""
    text = str(value or "").strip()
    if not text:
        return False
    if _is_allowed_provider_url(text):
        return True
    return len(text) <= 2_000_000 and bool(_DATA_IMAGE_RE.fullmatch(text))


def _map_extract_result(raw: dict[str, Any]) -> dict[str, Any]:
    link = str(raw.get("link") or "").strip()
    qr_text = str(raw.get("qr_text") or link).strip()
    if not _is_allowed_provider_url(link):
        raise KakaoWorkerError(
            "kakao_invalid_provider_redirect",
            "Kakao worker returned an invalid provider link",
            transient=True,
            stage="redirect_validation",
        )
    if qr_text != link and not is_allowed_kakao_qr(qr_text):
        raise KakaoWorkerError(
            "kakao_invalid_provider_redirect",
            "Kakao worker returned an invalid QR link",
            transient=True,
            stage="redirect_validation",
        )
    amount = raw.get("amount")
    currency = str(raw.get("currency") or "").upper()
    if currency != "KRW" or not _is_zero(amount):
        raise KakaoWorkerError(
            "kakao_nonzero_checkout",
            "Kakao checkout is not zero KRW",
            transient=False,
            stage="final_init",
        )
    session_id = str(raw.get("checkout_session_id") or "")
    if not session_id.startswith("cs_"):
        raise KakaoWorkerError(
            "kakao_invalid_checkout_session",
            "Kakao worker returned an invalid checkout session",
            transient=True,
        )
    expires_at = _epoch_timestamp(raw.get("expires_at"))
    generated_at = _epoch_timestamp(raw.get("generated_at"))
    if expires_at is None:
        expires_at = time.time() + 15 * 60
    elif expires_at <= time.time():
        raise KakaoWorkerError(
            "kakao_link_expired",
            "Kakao worker returned an expired provider link",
            transient=True,
            stage="redirect_validation",
        )
    qr_image_png = str(raw.get("qr_image_png") or "").strip()
    qr_image_svg = str(raw.get("qr_image_svg") or "").strip()
    for image in (qr_image_png, qr_image_svg):
        if image and not is_allowed_kakao_qr(image):
            raise KakaoWorkerError(
                "kakao_invalid_qr_image",
                "Kakao worker returned an invalid QR image",
                transient=True,
                stage="redirect_validation",
            )
    result: dict[str, Any] = {
        "provider": "kakao",
        "provider_redirect_url": link,
        "qr_data": qr_text,
        "checkout_session_id": session_id,
        "checkout_amount": amount,
        "checkout_currency": currency,
        "amount": amount,
        "currency": currency,
        "promo_requested": True,
        "promo_applied": True,
        "next_action_type": "kakao_pay_redirect",
        "expires_at": expires_at,
        "expiry_source": str(raw.get("expiry_source") or "policy"),
    }
    if qr_image_png:
        result["qr_image_png"] = qr_image_png
    if qr_image_svg:
        result["qr_image_svg"] = qr_image_svg
    if generated_at is not None:
        result["generated_at"] = generated_at
    return result


def _is_zero(value: Any) -> bool:
    try:
        return float(value) == 0.0 and str(value).strip() not in {"", "None"}
    except (TypeError, ValueError):
        return str(value).strip() in {"0", "0.0", "0.00"}


def extract_kakao_link(
    access_token: str,
    proxy_url: str,
    *,
    route: str = "reference",
    timeout: float = DEFAULT_OPERATION_TIMEOUT,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Check eligibility and extract one strict zero-KRW Kakao link."""
    requested_at, deadline, process_timeout = _worker_deadline(timeout)
    common = {
        "protocol_version": 1,
        "route": route,
        "proxy_url": proxy_url,
        "requested_at": requested_at,
        "deadline": deadline,
        "access_token": access_token,
    }
    if cancel_check and cancel_check():
        raise KakaoWorkerError("kakao_cancelled", "Kakao extraction was cancelled", transient=False)
    eligibility = _run_worker(
        {**common, "operation": "eligibility"},
        timeout=process_timeout,
        cancel_check=cancel_check,
    )
    if eligibility.get("eligible") is not True:
        raise KakaoWorkerError(
            "kakao_trial_unavailable",
            "Kakao free-trial eligibility was not confirmed",
            transient=False,
            stage="trial_eligibility",
        )
    if cancel_check and cancel_check():
        raise KakaoWorkerError("kakao_cancelled", "Kakao extraction was cancelled", transient=False)
    # Use a fresh deadline for extraction.  The worker's own 15-minute window
    # is per operation, and a slow eligibility preflight must not consume the
    # entire provider polling budget.
    requested_at, deadline, process_timeout = _worker_deadline(timeout)
    raw = _run_worker(
        {
            **common,
            "operation": "extract",
            "requested_at": requested_at,
            "deadline": deadline,
            "trial_eligibility_confirmed": True,
        },
        timeout=process_timeout,
        cancel_check=cancel_check,
    )
    return _map_extract_result(raw)


__all__ = [
    "DEFAULT_OPERATION_TIMEOUT",
    "KakaoWorkerError",
    "extract_kakao_link",
    "is_allowed_kakao_qr",
    "is_allowed_kakao_url",
    "_run_worker",
    "WORKER_PATH",
]
