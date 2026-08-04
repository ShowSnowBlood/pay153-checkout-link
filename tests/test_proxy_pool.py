from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

import proxy_pool
from proxy_pool import ProxyPoolOptimizer, ProxyProbe


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


if __name__ == "__main__":
    unittest.main()
