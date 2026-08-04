from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

import proxy_pool
from proxy_pool import ProxyLeaseRegistry, ProxyPoolOptimizer, ProxyProbe


def probe(proxy_url: str, *, ip: str, country: str, score: float, ok: bool = True) -> ProxyProbe:
    return ProxyProbe(
        proxy_url=proxy_url,
        exit_ip=ip,
        country=country,
        region="",
        city="",
        currency="",
        openai_ok=ok,
        stripe_ok=ok,
        geo_ok=ok,
        latency_ms=100,
        score=score,
    )


class DynamicSessionTests(unittest.TestCase):
    def test_materializes_sticky_session_and_lifetime(self) -> None:
        template = "http://user-session-__rotate__-lifetime-120:secret@gateway.test:6060"
        before = time.time()
        with patch.object(proxy_pool.secrets, "token_urlsafe", return_value="AbCdEf123456"):
            concrete, expires_at = proxy_pool.materialize_proxy_url(template)

        parsed = urlsplit(concrete)
        self.assertEqual(unquote(parsed.username or ""), "user-session-abcdef123456-lifetime-120")
        self.assertEqual(unquote(parsed.password or ""), "secret")
        self.assertGreaterEqual(expires_at, before + 7199)
        self.assertLessEqual(expires_at, time.time() + 7201)

    def test_static_proxy_is_unchanged(self) -> None:
        value = "http://user:secret@gateway.test:6060"
        self.assertEqual(proxy_pool.materialize_proxy_url(value), (value, 0.0))

    def test_encoded_session_placeholder_is_supported(self) -> None:
        template = "http://user-session-%7Bsession%7D-lifetime-5:secret@gateway.test:6060"
        with patch.object(proxy_pool.secrets, "token_urlsafe", return_value="candidate001"):
            concrete, _ = proxy_pool.materialize_proxy_url(template)
        self.assertIn("session-candidate001-lifetime-5", unquote(urlsplit(concrete).username or ""))

    def test_country_is_inserted_before_dynamic_session(self) -> None:
        template = "http://user-session-__rotate__-lifetime-120:secret@gateway.test:6060"
        routed = proxy_pool.set_rotating_gateway_country(template, "IN")
        self.assertIn("country-in-session-__rotate__", unquote(urlsplit(routed).username or ""))

    def test_existing_country_is_replaced(self) -> None:
        template = "http://user-country-us-session-__rotate__-lifetime-120:secret@gateway.test:6060"
        routed = proxy_pool.set_rotating_gateway_country(template, "NL")
        username = unquote(urlsplit(routed).username or "")
        self.assertIn("country-nl-session-__rotate__", username)
        self.assertNotIn("country-us", username)

    def test_provider_country_mapping_reserves_korea_for_kakao(self) -> None:
        self.assertEqual(proxy_pool.PROVIDER_PROXY_COUNTRIES["kakao"], "KR")
        self.assertEqual(proxy_pool.PROVIDER_PROXY_COUNTRIES["upi"], "IN")
        self.assertEqual(proxy_pool.PROVIDER_PROXY_COUNTRIES["ideal"], "NL")


class ProxyInputFormatTests(unittest.TestCase):
    def test_scheme_host_port_username_password_format(self) -> None:
        from app import normalize_proxy

        raw = "socks5://rp.scrapegw.com:6060:sample-user:sample-password"
        self.assertEqual(
            normalize_proxy(raw),
            "http://sample-user-session-__rotate__-lifetime-120:sample-password@rp.scrapegw.com:6060",
        )

    def test_http_vendor_format_preserves_colon_in_password(self) -> None:
        from app import normalize_proxy

        raw = "http://rp.scrapegw.com:6060:sample-user:password:part"
        self.assertEqual(
            normalize_proxy(raw),
            "http://sample-user-session-__rotate__-lifetime-120:password%3Apart@rp.scrapegw.com:6060",
        )

    def test_standard_authenticated_url_still_works(self) -> None:
        from app import normalize_proxy

        raw = "socks5://sample-user:sample-password@proxy.example:6060"
        self.assertEqual(normalize_proxy(raw), raw)

    def test_scrapegw_template_receives_payment_country(self) -> None:
        from app import normalize_proxy

        normalized = normalize_proxy("http://rp.scrapegw.com:6060:sample-user:sample-password")
        self.assertTrue(proxy_pool.is_dynamic_template(normalized))
        routed = proxy_pool.set_rotating_gateway_country(normalized, "IN")
        username = unquote(urlsplit(routed).username or "")
        self.assertEqual(
            username,
            "sample-user-country-in-session-__rotate__-lifetime-120",
        )


class OptimizerSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.optimizer = ProxyPoolOptimizer()
        self.optimizer.probe_count = 4

    def test_expected_country_wins_over_higher_wrong_country_score(self) -> None:
        nl = "http://nl.test:8000"
        us = "http://us.test:8000"

        def fake_probe(url: str, **_: object) -> ProxyProbe:
            return probe(url, ip="1.1.1.1", country="NL", score=91) if url == nl else probe(
                url, ip="8.8.8.8", country="US", score=99
            )

        with patch.object(self.optimizer, "probe", side_effect=fake_probe):
            selected = self.optimizer.select([us, nl], role="payment", provider="ideal", expected_country="NL")
        self.assertEqual(selected.proxy_url, nl)

    def test_duplicate_exit_ip_is_deduplicated(self) -> None:
        first = "http://first.test:8000"
        second = "http://second.test:8000"

        def fake_probe(url: str, **_: object) -> ProxyProbe:
            return probe(url, ip="1.1.1.1", country="IN", score=95 if url == first else 80)

        with patch.object(self.optimizer, "probe", side_effect=fake_probe):
            selected = self.optimizer.select([second, first], role="payment", provider="upi", expected_country="IN")
        self.assertEqual(selected.proxy_url, first)

    def test_failed_proxy_enters_cooldown(self) -> None:
        bad = "http://bad.test:8000"
        good = "http://good.test:8000"
        self.optimizer.report([bad], success=False, error="timeout")
        candidates = self.optimizer._materialize_candidates([bad, good], 2)
        self.assertEqual([item[0] for item in candidates], [good])

    def test_partial_probe_is_not_selected_for_payment(self) -> None:
        candidate = "http://partial.test:8000"
        partial = probe(candidate, ip="1.1.1.1", country="IN", score=70, ok=False)
        partial = ProxyProbe(**{**partial.__dict__, "geo_ok": True, "stripe_ok": True})
        with patch.object(self.optimizer, "probe", return_value=partial):
            with self.assertRaisesRegex(RuntimeError, "没有同时通过深度验证"):
                self.optimizer.select([candidate], role="支付", provider="upi", expected_country="IN")


class OpenAIProbeContractTests(unittest.TestCase):
    @staticmethod
    def response(status: int, *, payload: object = None, text: str = "") -> Mock:
        response = Mock(status_code=status, text=text)
        if payload is None:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = payload
        return response

    def test_csrf_contract_is_preferred(self) -> None:
        http = Mock()
        http.get.return_value = self.response(200, payload={"csrfToken": "ok"})
        self.assertEqual(ProxyPoolOptimizer._probe_openai(http, 4), (True, ""))
        self.assertEqual(http.get.call_count, 1)

    def test_chatgpt_structured_401_is_checkout_reachability_success(self) -> None:
        http = Mock()
        http.get.side_effect = [
            self.response(403, text="blocked"),
            self.response(401, text='{"detail":"Unauthorized: missing bearer credentials"}'),
        ]
        self.assertEqual(ProxyPoolOptimizer._probe_openai(http, 4), (True, ""))

    def test_cloudflare_403_is_not_reachability_success(self) -> None:
        http = Mock()
        http.get.side_effect = [
            self.response(403, text="blocked"),
            self.response(403, text="Just a moment"),
        ]
        ok, error = ProxyPoolOptimizer._probe_openai(http, 4)
        self.assertFalse(ok)
        self.assertIn("chatgpt_403", error)


class AttemptNetworkContextTests(unittest.TestCase):
    def test_reuses_and_closes_one_session_per_proxy(self) -> None:
        from app import AttemptNetworkContext

        first = Mock()
        first.cookies = Mock()
        second = Mock()
        second.cookies = Mock()
        with patch("app.sc.build_http", side_effect=[first, second]) as build:
            context = AttemptNetworkContext("http://entry:1", "http://exit:2", "device", "did")
            self.assertIs(context.http("http://entry:1"), first)
            self.assertIs(context.http("http://entry:1"), first)
            self.assertIs(context.http("http://exit:2"), second)
            context.close()

        self.assertEqual(build.call_count, 2)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_single_chain_builds_only_one_http_session(self) -> None:
        from app import AttemptNetworkContext

        session = Mock()
        session.cookies = Mock()
        proxy_url = "http://sticky.test:8000"
        with patch("app.sc.build_http", return_value=session) as build:
            context = AttemptNetworkContext(proxy_url, proxy_url, "device", "did")
            self.assertIs(context.http(context.entry_proxy), context.http(context.exit_proxy))
            context.close()

        build.assert_called_once_with(proxy_url)
        session.close.assert_called_once_with()


class ProxyLeaseRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "proxy_leases.json"
        self.registry = ProxyLeaseRegistry(self.path, lease_minutes=120)
        self.token = "eyJ.test.access-token-plaintext"
        self.token_hash = self.registry.token_hash(self.token)
        self.proxy = "http://account-country-in-session-session123456-lifetime-120:secret@gateway.test:6060"
        self.probe = probe(self.proxy, ip="1.1.1.1", country="IN", score=95)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_persists_and_reloads_without_plaintext_access_token(self) -> None:
        self.registry.put(self.token_hash, "upi", "IN", self.probe)
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn(self.token, persisted)
        self.assertIn(self.token_hash, persisted)

        reloaded = ProxyLeaseRegistry(self.path, lease_minutes=120)
        row = reloaded.get(self.token_hash, "upi", "IN")
        self.assertIsNotNone(row)
        self.assertEqual(row["exit_ip"], "1.1.1.1")
        self.assertEqual(row["session_id"], "session123456")

    def test_provider_or_country_change_replaces_the_only_active_lease(self) -> None:
        self.registry.put(self.token_hash, "upi", "IN", self.probe)
        self.assertIsNone(self.registry.get(self.token_hash, "ideal", "NL"))
        self.assertEqual(self.registry.public_records(), [])

        nl_probe = probe(self.proxy.replace("country-in", "country-nl"), ip="8.8.8.8", country="NL", score=90)
        self.registry.put(self.token_hash, "ideal", "NL", nl_probe)
        records = self.registry.public_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider"], "ideal")

    def test_expired_lease_is_purged(self) -> None:
        expired = ProxyProbe(**{**self.probe.__dict__, "expires_at": time.time() - 1})
        self.registry.put(self.token_hash, "upi", "IN", expired)
        self.assertEqual(self.registry.public_records(), [])
        self.assertIsNone(self.registry.get(self.token_hash, "upi", "IN"))

    def test_public_record_is_sanitized(self) -> None:
        self.registry.put(self.token_hash, "upi", "IN", self.probe)
        record = self.registry.public_records()[0]
        serialized = json.dumps(record)
        self.assertNotIn("proxy_url", record)
        self.assertNotIn("secret", serialized)
        self.assertNotIn(self.token_hash, serialized)
        self.assertEqual(record["session_id"], "sess...3456")

    def test_invalidate_removes_lease(self) -> None:
        self.registry.put(self.token_hash, "upi", "IN", self.probe)
        self.assertTrue(self.registry.invalidate(self.token_hash, "timeout"))
        self.assertEqual(self.registry.public_records(), [])


class ProxyLeaseApiTests(unittest.TestCase):
    @staticmethod
    def jwt() -> str:
        def encode(value: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return f"{encode({'alg': 'none'})}.{encode({'exp': int(time.time()) + 3600})}.signature"

    def test_config_exposes_managed_gateway_without_requiring_proxy_inputs(self) -> None:
        import app as checkout_app

        with patch.object(checkout_app, "CONFIGURED_PROXY_GATEWAY", "managed-gateway"):
            response = checkout_app.app.test_client().get("/api/config")
        policy = response.get_json()["proxy_policy"]
        self.assertFalse(policy["entry_required"])
        self.assertEqual(policy["exit_required_for"], [])
        self.assertTrue(policy["managed_gateway"])
        self.assertIn("upi_without_promo", policy["single_chain_for"])
        self.assertIn("ideal", policy["single_chain_for"])
        self.assertEqual(policy["dual_region_for"]["upi_promo"], {"entry": "JP", "payment": "IN"})

    def test_managed_gateway_creates_upi_request_without_client_proxy(self) -> None:
        import app as checkout_app

        captured: dict = {}
        gateway = "http://rp.scrapegw.com:6060:sample-user:sample-password"

        def create(options: dict) -> str:
            captured.update(options)
            return "job-managed"

        payload = {"token": self.jwt(), "plan": "plus", "link_type": "upi"}
        with patch.object(checkout_app, "CONFIGURED_PROXY_GATEWAY", gateway), \
             patch.object(checkout_app.STORE, "create", side_effect=create), \
             patch.object(checkout_app.STORE, "queue_position", return_value=0), \
             patch.object(checkout_app.IP_TASK_LIMITER, "acquire", return_value=(True, 0)):
            response = checkout_app.app.test_client().post("/api/checkout", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["entry_proxies"], captured["exit_proxies"])
        self.assertTrue(proxy_pool.is_dynamic_template(captured["entry_proxies"][0]))
        self.assertEqual(captured["promo_proxy_country"], "JP")

    def test_frontend_has_no_proxy_pool_inputs(self) -> None:
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="entryProxy"', source)
        self.assertNotIn('id="exitProxy"', source)
        self.assertNotIn("entry_proxies:", script)

    def test_upi_request_uses_entry_pool_for_both_sides(self) -> None:
        import app as checkout_app

        captured: dict = {}

        def create(options: dict) -> str:
            captured.update(options)
            return "job123"

        payload = {
            "token": self.jwt(),
            "plan": "plus",
            "link_type": "upi",
            "country": "US",
            "currency": "USD",
            "entry_proxies": ["http://user:secret@gateway.test:6060"],
        }
        with patch.object(checkout_app.STORE, "create", side_effect=create), \
             patch.object(checkout_app.STORE, "queue_position", return_value=0), \
             patch.object(checkout_app.IP_TASK_LIMITER, "acquire", return_value=(True, 0)):
            response = checkout_app.app.test_client().post("/api/checkout", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["entry_proxies"], captured["exit_proxies"])
        self.assertEqual((captured["country"], captured["currency"]), ("IN", "INR"))
        self.assertNotIn("token_raw", captured)
        self.assertEqual(len(captured["token_lease_key"]), 64)

    def test_upi_promo_routes_japan_entry_and_india_payment(self) -> None:
        import app as checkout_app

        route = checkout_app.checkout_proxy_route({
            "link_type": "upi",
            "country": "IN",
            "use_promo": True,
            "promo_proxy_country": "JP",
        })

        self.assertEqual(route, ("JP", "IN", False))

    def test_proxy_lease_api_never_returns_proxy_credentials(self) -> None:
        import app as checkout_app

        with tempfile.TemporaryDirectory() as directory:
            registry = ProxyLeaseRegistry(Path(directory) / "leases.json", lease_minutes=120)
            token_hash = registry.token_hash(self.jwt())
            credentialed = "http://user-session-abcdef123456-lifetime-120:secret@gateway.test:6060"
            registry.put(token_hash, "ideal", "NL", probe(credentialed, ip="8.8.8.8", country="NL", score=96))
            with patch.object(checkout_app, "LEASES", registry):
                response = checkout_app.app.test_client().get("/api/proxy-leases")

        body = response.get_json()
        serialized = json.dumps(body)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("proxy_url", serialized)
        self.assertNotIn(token_hash, serialized)


class ProviderBillingOrderTests(unittest.TestCase):
    def test_upi_india_billing_is_synced_before_promo_update(self) -> None:
        import provider_checkout

        events: list[str] = []
        billing = {
            "name": "Arjun Sharma",
            "email": "test@example.com",
            "address": {
                "country": "IN", "line1": "1 MG Road", "city": "Bengaluru",
                "postal_code": "560001", "state": "KA",
            },
        }
        ctx = {
            "checkout_amount": 169407,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
        }

        def update_tax(*args, **kwargs):
            events.append("tax_region")
            args[4]["checkout_amount"] = 199900
            self.assertEqual(args[5]["address"]["country"], "IN")
            return {}

        def snapshot(*args, **kwargs):
            events.append("snapshot")
            self.assertEqual(args[4]["address"]["postal_code"], "560001")

        def apply_promo(_processor: str):
            events.append("promo")
            raise RuntimeError("stop-after-order-check")

        with patch.object(provider_checkout.sc, "verify_pk", return_value="pk_test"), \
             patch.object(provider_checkout.sc, "init_checkout", return_value=({}, "version", ctx)), \
             patch.object(provider_checkout.sc, "fetch_elements_session", return_value={}), \
             patch.object(provider_checkout.sc, "update_tax_region", side_effect=update_tax), \
             patch.object(provider_checkout.sc, "snapshot_billing", side_effect=snapshot):
            with self.assertRaisesRegex(RuntimeError, "stop-after-order-check"):
                provider_checkout.stripe_to_provider(
                    Mock(), "cs_test", "upi", billing=billing, country="IN",
                    chatgpt_http=Mock(), access_token="token", stage1={},
                    apply_promo_callback=apply_promo, require_zero_due=True,
                    local_method_strategy="standalone",
                )

        self.assertEqual(events, ["tax_region", "snapshot", "promo"])


if __name__ == "__main__":
    unittest.main()
