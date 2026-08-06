from __future__ import annotations

import base64
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app as checkout_app
import kakao_checkout as kc


def jwt() -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': int(time.time()) + 3600})}.signature"


class KakaoAdapterTests(unittest.TestCase):
    def test_extract_checks_eligibility_before_extract_and_maps_zero_result(self) -> None:
        calls: list[dict] = []

        def worker(request: dict, **_: object) -> dict:
            calls.append(request)
            if request["operation"] == "eligibility":
                return {"eligible": True, "coupon": "plus-1-month-free"}
            generated = datetime.now(timezone.utc)
            return {
                "link": "https://pay.kakaopay.com/checkout/abc",
                "qr_text": "https://pay.kakaopay.com/checkout/abc",
                "checkout_session_id": "cs_test_kakao",
                "payment_method_id": "pm_private",
                "amount": 0,
                "currency": "KRW",
                "generated_at": generated.isoformat(),
                "expires_at": (generated + timedelta(minutes=15)).isoformat(),
                "expiry_source": "policy",
            }

        with patch.object(kc, "_run_worker", side_effect=worker):
            result = kc.extract_kakao_link(
                jwt(),
                "http://user-country-kr-session-abcd1234-lifetime-120:secret@gateway.test:6060",
                timeout=30,
            )

        self.assertEqual([call["operation"] for call in calls], ["eligibility", "extract"])
        self.assertTrue(calls[1]["trial_eligibility_confirmed"])
        self.assertEqual(result["provider"], "kakao")
        self.assertEqual(result["checkout_amount"], 0)
        self.assertEqual(result["checkout_currency"], "KRW")
        self.assertNotIn("payment_method_id", result)
        self.assertIsInstance(result["expires_at"], float)

    def test_mapping_rejects_nonzero_and_lookalike_hosts(self) -> None:
        base = {
            "link": "https://pay.kakaopay.com/checkout/abc",
            "qr_text": "https://pay.kakaopay.com/checkout/abc",
            "checkout_session_id": "cs_test_kakao",
            "amount": 0,
            "currency": "KRW",
        }
        with self.assertRaisesRegex(kc.KakaoWorkerError, "kakao_nonzero_checkout"):
            kc._map_extract_result({**base, "amount": 100})
        with self.assertRaisesRegex(kc.KakaoWorkerError, "kakao_invalid_provider_redirect"):
            kc._map_extract_result({**base, "link": "https://kakaopay.com.attacker.test/x"})
        self.assertTrue(kc.is_allowed_kakao_url(base["link"]))
        self.assertFalse(kc.is_allowed_kakao_url("http://pay.kakaopay.com/x"))
        self.assertFalse(kc.is_allowed_kakao_url("https://evil.kakaopay.com/x"))
        self.assertTrue(kc.is_allowed_kakao_qr("data:image/png;base64,AA=="))


class KakaoApiTests(unittest.TestCase):
    def test_config_exposes_kakao_defaults_and_route(self) -> None:
        response = checkout_app.app.test_client().get("/api/config")
        body = response.get_json()
        self.assertIn("kakao", body["link_types"])
        self.assertEqual(body["provider_defaults"]["kakao"], {"country": "KR", "currency": "KRW"})
        self.assertIn("kakao", body["proxy_policy"]["single_chain_for"])

    def test_kakao_requires_plus_and_promo(self) -> None:
        client = checkout_app.app.test_client()
        for payload in (
            {"token": jwt(), "plan": "pro", "link_type": "kakao"},
            {"token": jwt(), "plan": "plus", "link_type": "kakao", "use_promo": False},
        ):
            response = client.post("/api/checkout", json=payload)
            self.assertEqual(response.status_code, 400)

    def test_kakao_request_forces_krw_single_chain(self) -> None:
        captured: dict = {}

        def create(options: dict) -> str:
            captured.update(options)
            return "job-kakao"

        with patch.object(checkout_app.STORE, "create", side_effect=create), \
             patch.object(checkout_app.STORE, "queue_position", return_value=0), \
             patch.object(checkout_app.IP_TASK_LIMITER, "acquire", return_value=(True, 0)), \
             patch.object(checkout_app, "CONFIGURED_PROXY_GATEWAY", "http://user-country-kr-session-__rotate__-lifetime-120:secret@gateway.test:6060"):
            response = checkout_app.app.test_client().post(
                "/api/checkout",
                json={"token": jwt(), "plan": "plus", "link_type": "kakao"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["country"], "KR")
        self.assertEqual(captured["currency"], "KRW")
        self.assertEqual(captured["entry_proxies"], captured["exit_proxies"])
        self.assertEqual(captured["kakao_route"], "reference")
        self.assertTrue(captured["use_promo"])


class KakaoResultValidationTests(unittest.TestCase):
    def test_validate_provider_result_requires_zero_krw_provider_link(self) -> None:
        valid = {
            "checkout_amount": 0,
            "checkout_currency": "KRW",
            "provider_redirect_url": "https://pay.kakaopay.com/checkout/abc",
            "promo_requested": True,
            "promo_applied": True,
        }
        checkout_app.validate_provider_result("kakao", valid)
        with self.assertRaisesRegex(RuntimeError, "kakao_checkout_amount_not_zero"):
            checkout_app.validate_provider_result("kakao", {**valid, "checkout_amount": 1})
        with self.assertRaisesRegex(RuntimeError, "kakao_invalid_provider_redirect"):
            checkout_app.validate_provider_result("kakao", {**valid, "provider_redirect_url": "https://pay.openai.com/x"})
        checkout_app.validate_provider_result(
            "kakao",
            {**valid, "qr_data": "data:image/png;base64,AA=="},
        )

    def test_job_adapter_marks_only_mapped_zero_result_done(self) -> None:
        store = checkout_app.JobStore.__new__(checkout_app.JobStore)
        updates: list[dict] = []
        store.update = lambda _job_id, **fields: updates.append(fields)
        store.log = lambda *_args, **_kwargs: None
        store.cancelled = lambda _job_id: False
        store.ensure_not_cancelled = lambda _job_id: None
        mapped = {
            "provider": "kakao",
            "provider_redirect_url": "https://pay.kakaopay.com/checkout/abc",
            "qr_data": "https://pay.kakaopay.com/checkout/abc",
            "checkout_session_id": "cs_test_kakao",
            "checkout_amount": 0,
            "checkout_currency": "KRW",
            "promo_requested": True,
            "promo_applied": True,
        }
        with patch.object(checkout_app, "extract_kakao_link", return_value=mapped):
            store._run_kakao_single(
                "job-kakao",
                {"kakao_route": "reference"},
                jwt(),
                {"email": "test@example.com", "account_id": "acct_test"},
                "http://user-country-kr-session-abcd1234-lifetime-120:secret@gateway.test:6060",
                ["proxy-1"],
                None,
            )
        self.assertEqual(updates[-1]["status"], "done")
        self.assertNotIn("payment_method_id", updates[-1]["result"])
        self.assertEqual(updates[-1]["result"]["link_type"], "kakao")


class KakaoWorkerSourceTests(unittest.TestCase):
    def test_worker_is_repo_local_and_compiles(self) -> None:
        source = Path(__file__).parents[1] / "kakao_worker.py"
        self.assertTrue(source.is_file())
        compile(source.read_text(encoding="utf-8"), str(source), "exec")

    def test_worker_stdin_protocol_returns_sanitized_failure(self) -> None:
        with self.assertRaisesRegex(kc.KakaoWorkerError, "kakao_access_token_required"):
            kc._run_worker({"protocol_version": 1}, timeout=5)

    def test_stripe_pin_keeps_hostname_verification(self) -> None:
        from curl_cffi.requests import Session
        from curl_cffi import CurlOpt
        import kakao_worker

        session = Session(impersonate="chrome136")
        try:
            with kakao_worker._stripe_pinned_tls(session, "api.stripe.com", "1.1.1.1"):
                self.assertEqual(session.curl_options[CurlOpt.SSL_VERIFYHOST], 2)
                self.assertEqual(
                    session.curl_options[CurlOpt.RESOLVE],
                    ["api.stripe.com:443:1.1.1.1"],
                )
        finally:
            session.close()
        self.assertEqual(session.curl_options, {})


if __name__ == "__main__":
    unittest.main()
