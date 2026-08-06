from __future__ import annotations

import base64
import json
import socket
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
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
        self.assertTrue(kc.is_allowed_kakao_url("https://pay.kakaopay.com:443/x"))
        self.assertFalse(kc.is_allowed_kakao_url("http://pay.kakaopay.com/x"))
        self.assertFalse(kc.is_allowed_kakao_url("https://evil.kakaopay.com/x"))
        self.assertFalse(kc.is_allowed_kakao_url("https://pay.kakaopay.com:4444/x"))
        self.assertFalse(kc.is_allowed_kakao_url("https://pay.kakaopay.com:99999/x"))
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
        captured: dict = {}

        def extract(*_args: object, **kwargs: object) -> dict:
            captured.update(kwargs)
            return mapped

        with patch.object(checkout_app, "extract_kakao_link", side_effect=extract), \
             patch.object(checkout_app, "DEFAULT_KAKAO_WORKER_TIMEOUT", 321):
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
        self.assertEqual(captured["timeout"], 321.0)

    def test_kakao_success_link_is_not_persisted(self) -> None:
        valid = {
            "link_type": "kakao",
            "checkout_amount": 0,
            "checkout_currency": "KRW",
            "provider_redirect_url": "https://pay.kakaopay.com/checkout/abc",
            "promo_requested": True,
            "promo_applied": True,
        }
        with TemporaryDirectory() as tempdir, patch.object(
            checkout_app,
            "ROOT",
            Path(tempdir),
        ):
            checkout_app.STORE._record_success("job-kakao", valid)
            self.assertFalse((Path(tempdir) / "data" / "success_links.jsonl").exists())


class KakaoWorkerSourceTests(unittest.TestCase):
    def test_worker_is_repo_local_and_compiles(self) -> None:
        source = Path(__file__).parents[1] / "kakao_worker.py"
        self.assertTrue(source.is_file())
        compile(source.read_text(encoding="utf-8"), str(source), "exec")

    def test_worker_stdin_protocol_returns_sanitized_failure(self) -> None:
        with self.assertRaisesRegex(kc.KakaoWorkerError, "kakao_access_token_required"):
            kc._run_worker({"protocol_version": 1}, timeout=5)

    def test_worker_boundary_removes_payment_method_id(self) -> None:
        import kakao_worker

        now = datetime.now(timezone.utc)
        request = {
            "protocol_version": 1,
            "operation": "extract",
            "route": "reference",
            "proxy_url": "http://user-country-kr:secret@gateway.test:6060",
            "requested_at": now.isoformat(),
            "deadline": (now + timedelta(minutes=1)).isoformat(),
            "access_token": "test-token",
            "trial_eligibility_confirmed": True,
        }
        worker_result = {
            "link": "https://pay.kakaopay.com/checkout/abc",
            "qr_text": "https://pay.kakaopay.com/checkout/abc",
            "checkout_session_id": "cs_test_kakao",
            "payment_method_id": "pm_private",
            "amount": 0,
            "currency": "KRW",
        }
        with patch.object(kakao_worker, "_load_curl_requests", return_value=object()), \
             patch.object(kakao_worker, "extract_kakao_link", return_value=worker_result):
            result = kakao_worker._run(json.dumps(request))
        self.assertNotIn("payment_method_id", result)

    def test_proxy_chain_uses_country_scoped_sticky_sessions(self) -> None:
        import kakao_worker

        seed = (
            "http://user-country-jp-session-seed123-lifetime-120:secret"
            "@gateway.test:6060"
        )
        checkout, promotion, provider = kakao_worker.kakao_proxy_chain(seed)

        self.assertEqual(checkout, provider)
        self.assertIn("country-kr-session-seed123kr-lifetime-120", checkout)
        self.assertIn("country-vn-session-seed123vn-lifetime-120", promotion)
        self.assertEqual(
            kakao_worker._proxy_chain_key(checkout),
            kakao_worker._proxy_chain_key(promotion),
        )

    def test_stripe_pin_changes_proxy_connect_target_and_keeps_sni(self) -> None:
        import kakao_worker

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(5)
        proxy_port = server.getsockname()[1]
        captured: dict[str, object] = {}

        def capture_proxy() -> None:
            try:
                connection, _ = server.accept()
                connect_request = connection.recv(4096)
                captured["connect"] = connect_request.split(b"\r\n", 1)[0]
                connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                client_hello = connection.recv(8192)
                captured["sni"] = b"api.stripe.com" in client_hello
                connection.close()
            finally:
                server.close()

        thread = threading.Thread(target=capture_proxy, daemon=True)
        thread.start()
        client = kakao_worker.KakaoHttpClient(
            f"http://127.0.0.1:{proxy_port}",
            {"api.stripe.com": "1.1.1.1"},
        )
        with patch.object(kakao_worker, "_DEADLINE_TS", time.time() + 5):
            try:
                with self.assertRaises(kakao_worker.WorkerFailure):
                    client.request(
                        "GET",
                        "https://api.stripe.com/v1/test",
                        stage="stripe_connect_test",
                        timeout=3,
                    )
            finally:
                client.close()
        thread.join(timeout=5)
        self.assertEqual(captured.get("connect"), b"CONNECT 1.1.1.1:443 HTTP/1.1")
        self.assertIs(captured.get("sni"), True)
        self.assertEqual(client.session.curl_options, {})

    def test_worker_redirect_rejects_non_default_https_ports(self) -> None:
        import kakao_worker

        for url in (
            "https://pay.kakaopay.com:4444/x",
            "https://pay.kakaopay.com:99999/x",
        ):
            with self.assertRaises(kakao_worker.WorkerFailure) as raised:
                kakao_worker._validate_redirect_url(url, final=True)
            self.assertEqual(raised.exception.code, "kakao_invalid_link")


if __name__ == "__main__":
    unittest.main()
