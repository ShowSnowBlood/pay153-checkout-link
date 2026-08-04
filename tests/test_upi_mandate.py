from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

import provider_checkout


class MandateHelpersTests(unittest.TestCase):
    def test_resolve_mandate_amount_prefers_original_checkout(self) -> None:
        amount = provider_checkout.resolve_mandate_amount(
            "upi",
            {"original_checkout_amount": 199900, "checkout_amount": 0},
        )
        self.assertEqual(amount, 199900)

    def test_resolve_mandate_amount_defaults_for_zero_due(self) -> None:
        self.assertEqual(provider_checkout.resolve_mandate_amount("upi", {"checkout_amount": 0}), 199900)
        self.assertEqual(provider_checkout.resolve_mandate_amount("pix", {"checkout_amount": 0}), 9990)

    def test_build_local_upi_mandate_does_not_synthesize_when_server_missing(self) -> None:
        options = provider_checkout.build_local_mandate_options(
            "upi",
            {"original_checkout_amount": 199900, "checkout_amount": 0},
            {},
        )
        self.assertEqual(options, {})

    def test_build_local_upi_mandate_keeps_server_fields(self) -> None:
        options = provider_checkout.build_local_mandate_options(
            "upi",
            {"original_checkout_amount": 199900},
            {"mandate_options": {"amount": 150000, "amount_type": "fixed", "description": "Plus"}},
        )
        mandate = options["mandate_options"]
        self.assertEqual(mandate["amount"], 150000)
        self.assertEqual(mandate["amount_type"], "fixed")
        self.assertEqual(mandate["description"], "Plus")
        self.assertNotIn("end_date", mandate)

    def test_build_local_pix_mandate_when_server_missing(self) -> None:
        options = provider_checkout.build_local_mandate_options(
            "pix",
            {"original_checkout_amount": 9990, "checkout_amount": 0},
            {},
        )
        mandate = options["mandate_options"]
        self.assertEqual(mandate["amount"], 9990)
        self.assertEqual(mandate["amount_type"], "maximum")
        self.assertEqual(mandate["payment_schedule"], "monthly")

    def test_extract_server_upi_options_merges_nested_setup_intent(self) -> None:
        options = provider_checkout.extract_server_provider_options(
            {
                "payment_method_options": {
                    "upi": {
                        "setup_future_usage": "off_session",
                        "mandate_options": {
                            "amount": 100000,
                            "amount_type": "maximum",
                        },
                    }
                },
                "setup_intent": {
                    "payment_method_options": {
                        "upi": {
                            "mandate_options": {
                                "amount": 150000,
                                "description": "Server mandate",
                            }
                        }
                    }
                },
            },
            "upi",
        )
        self.assertEqual(options["setup_future_usage"], "off_session")
        self.assertEqual(
            options["mandate_options"],
            {
                "amount": 150000,
                "amount_type": "maximum",
                "description": "Server mandate",
            },
        )


class ConfirmUpiMandateTests(unittest.TestCase):
    def test_zero_due_upi_without_server_mandate_refuses_confirm(self) -> None:
        http = Mock()
        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "billing": {
                "name": "Arjun Sharma",
                "email": "a@example.com",
                "address": {
                    "country": "IN",
                    "line1": "1 MG Road",
                    "city": "Bengaluru",
                    "postal_code": "560001",
                    "state": "KA",
                },
            },
            "runtime_version": "e1fb22ad35",
            "stripe_js_id": "js",
            "elements_session_id": "elements_session_1",
            "elements_session_config_id": "cfg",
            "config_id": "checkout_cfg",
            "locale": "en",
            "guid": "g",
            "muid": "m",
            "sid": "s",
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test",
        }
        init_resp = {
            "init_checksum": "checksum",
            "total_summary": {"due": 0},
            "payment_method_options": {},
            "setup_intent": {"usage": "off_session"},
        }
        logs: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "缺少服务端 AutoPay mandate"):
            provider_checkout.confirm_provider_payment(
                http,
                "pk_test",
                "cs_test",
                "upi",
                init_resp,
                "version",
                ctx,
                {"browser_locale": "en-IN", "browser_timezone": "Asia/Kolkata"},
                logs.append,
                payment_method_id="pm_test_upi",
            )

        http.post.assert_not_called()
        self.assertFalse(ctx.get("local_mandate_synthesized"))
        self.assertFalse(ctx.get("server_upi_mandate_present"))
        self.assertTrue(any("拒绝提交 confirm" in line for line in logs))

    def test_hosted_minimal_uses_server_mandate_without_injecting_it(self) -> None:
        captured: dict = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"submission_attempt": {"state": "requires_approval"}}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["data"] = dict(data or {})
            return FakeResp()

        http = Mock()
        http.post.side_effect = fake_post
        ctx = {"checkout_amount": 0, "init_checksum": "checksum"}
        init_resp = {
            "setup_intent": {
                "payment_method_options": {
                    "upi": {
                        "mandate_options": {
                            "amount": 199900,
                            "amount_type": "maximum",
                        }
                    }
                }
            }
        }
        payload = provider_checkout.confirm_upi_hosted_minimal(
            http,
            "pk_test",
            "cs_test",
            init_resp,
            ctx,
            {"name": "Arjun Sharma", "address": {"country": "IN"}},
            lambda _m: None,
        )

        self.assertEqual(payload["submission_attempt"]["state"], "requires_approval")
        self.assertFalse(any("mandate" in key for key in captured["data"]))
        self.assertTrue(ctx.get("server_upi_mandate_present"))
        self.assertFalse(ctx.get("local_mandate_synthesized"))

    def test_zero_due_upi_uses_server_mandate_when_present(self) -> None:
        captured: dict = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"setup_intent": {"status": "requires_action"}, "submission_attempt": {"state": "processing"}}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["data"] = dict(data or {})
            return FakeResp()

        http = Mock()
        http.post.side_effect = fake_post
        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "billing": {"name": "A", "email": "a@example.com", "address": {"country": "IN"}},
            "runtime_version": "e1fb22ad35",
            "stripe_js_id": "js",
            "elements_session_id": "elements_session_1",
            "elements_session_config_id": "cfg",
            "config_id": "checkout_cfg",
            "locale": "en",
            "guid": "g",
            "muid": "m",
            "sid": "s",
        }
        init_resp = {
            "init_checksum": "checksum",
            "total_summary": {"due": 0},
            "payment_method_options": {
                "upi": {
                    "mandate_options": {
                        "amount": 150000,
                        "amount_type": "fixed",
                        "description": "Server mandate",
                        "end_date": 1893456000,
                    }
                }
            },
            "setup_intent": {"usage": "off_session"},
        }
        provider_checkout.confirm_provider_payment(
            http,
            "pk_test",
            "cs_test",
            "upi",
            init_resp,
            "version",
            ctx,
            {"browser_locale": "en-IN", "browser_timezone": "Asia/Kolkata"},
            lambda _m: None,
            payment_method_id="pm_test_upi",
        )
        self.assertEqual(
            captured["data"].get("payment_method_options[upi][mandate_options][amount]"),
            "150000",
        )
        self.assertEqual(
            captured["data"].get("payment_method_options[upi][mandate_options][amount_type]"),
            "fixed",
        )
        self.assertTrue(ctx.get("server_upi_mandate_present"))
        self.assertFalse(ctx.get("local_mandate_synthesized"))


class SetupIntentRecoverTests(unittest.TestCase):
    def test_extract_payment_method_from_last_setup_error(self) -> None:
        pm = provider_checkout.extract_payment_method_id({
            "setup_intent": {
                "id": "seti_x",
                "payment_method": None,
                "last_setup_error": {
                    "payment_method": {"id": "pm_from_error", "type": "upi"},
                },
            }
        })
        self.assertEqual(pm, "pm_from_error")

    def test_confirm_local_uses_stashed_client_secret(self) -> None:
        captured: dict = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "id": "seti_test",
                    "status": "requires_action",
                    "next_action": {
                        "type": "upi_handle_redirect_or_display_qr_code",
                        "upi_handle_redirect_or_display_qr_code": {
                            "hosted_instructions_url": "https://hooks.stripe.com/upi/recover",
                            "qr_code": {"image_url_png": "https://example.com/recover.png"},
                        },
                    },
                }

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = dict(data or {})
            return FakeResp()

        http = Mock()
        http.post.side_effect = fake_post
        ctx = {
            "original_checkout_amount": 199900,
            "checkout_amount": 0,
            "setup_intent_id": "seti_test",
            "setup_intent_client_secret": "seti_test_secret_abc",
            "provider_payment_method_options": {
                "mandate_options": {
                    "amount": 199900,
                    "amount_type": "fixed",
                    "description": "Subscription payment",
                    "end_date": int(time.time()) + 86400,
                }
            },
            "server_upi_mandate_present": True,
            "local_mandate_synthesized": False,
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test",
        }
        page = {
            "setup_intent": {
                "id": "seti_test",
                # client_secret intentionally omitted on payment page payload
                "status": "requires_payment_method",
            },
            "submission_attempt": {"state": "failed"},
        }
        logs: list[str] = []
        merged = provider_checkout.confirm_local_setup_intent(
            http, "pk_test", "upi", page, "pm_upi", ctx, logs.append,
        )
        self.assertIn("/v1/setup_intents/seti_test/confirm", captured["url"])
        self.assertEqual(captured["data"]["client_secret"], "seti_test_secret_abc")
        self.assertEqual(
            captured["data"]["payment_method_options[upi][mandate_options][amount]"],
            "199900",
        )
        self.assertEqual(
            captured["data"]["payment_method_options[upi][mandate_options][amount_type]"],
            "fixed",
        )
        self.assertEqual(
            merged["setup_intent"]["next_action"]["type"],
            "upi_handle_redirect_or_display_qr_code",
        )
        self.assertTrue(any("直连补交" in line for line in logs))

    def test_seed_setup_intent_mandate_skips_publishable_key(self) -> None:
        http = Mock()
        ctx = {
            "original_checkout_amount": 199900,
            "checkout_amount": 0,
            "setup_intent_id": "seti_seed",
            "setup_intent_client_secret": "seti_seed_secret_xyz",
            "local_mandate_synthesized": False,
        }
        ok = provider_checkout.seed_setup_intent_mandate(
            http, "pk_test", "upi", ctx, lambda _m: None,
        )
        self.assertFalse(ok)
        http.post.assert_not_called()

    def test_seed_setup_intent_mandate_keeps_pix_local_options_with_secret_key(self) -> None:
        captured: dict = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "id": "seti_seed",
                    "client_secret": "seti_seed_secret_xyz",
                    "status": "requires_payment_method",
                }

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = dict(data or {})
            return FakeResp()

        http = Mock()
        http.post.side_effect = fake_post
        ctx = {
            "original_checkout_amount": 199900,
            "checkout_amount": 0,
            "setup_intent_id": "seti_seed",
            "setup_intent_client_secret": "seti_seed_secret_xyz",
            "local_mandate_synthesized": True,
        }
        ok = provider_checkout.seed_setup_intent_mandate(
            http, "sk_test", "pix", ctx, lambda _m: None,
        )
        self.assertTrue(ok)
        self.assertTrue(str(captured["url"]).endswith("/v1/setup_intents/seti_seed"))
        self.assertEqual(
            captured["data"]["payment_method_options[pix][mandate_options][amount]"],
            "199900",
        )
        self.assertEqual(
            captured["data"]["payment_method_options[pix][mandate_options][amount_type]"],
            "maximum",
        )

    def test_seed_setup_intent_mandate_does_not_create_upi_options(self) -> None:
        http = Mock()
        ctx = {
            "original_checkout_amount": 199900,
            "checkout_amount": 0,
            "setup_intent_id": "seti_seed",
            "setup_intent_client_secret": "seti_seed_secret_xyz",
        }
        ok = provider_checkout.seed_setup_intent_mandate(
            http, "sk_test", "upi", ctx, lambda _m: None,
        )
        self.assertFalse(ok)
        http.post.assert_not_called()


class StripeToProviderUpiFallbackTests(unittest.TestCase):
    def test_missing_server_mandate_returns_early_hosted_fallback(self) -> None:
        import provider_checkout as pc

        billing = {
            "name": "Arjun Sharma",
            "email": "test@example.com",
            "address": {
                "country": "IN", "line1": "1 MG Road", "city": "Bengaluru",
                "postal_code": "560001", "state": "KA",
            },
        }
        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        init_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_x", "client_secret": "seti_x_secret_y", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        logs: list[str] = []
        approve = Mock()

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(pc.sc, "init_checkout", return_value=(init_data, "version", dict(ctx))), \
             patch.object(pc.sc, "fetch_elements_session", return_value={}), \
             patch.object(pc.sc, "update_tax_region", return_value={}), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_upi") as create_mock, \
             patch.object(pc, "confirm_provider_payment") as confirm_mock, \
             patch.object(pc, "confirm_upi_hosted_minimal") as hosted_confirm_mock:
            result = pc.stripe_to_provider(
                Mock(),
                "cs_test",
                "upi",
                billing=billing,
                country="IN",
                chatgpt_http=Mock(),
                access_token="token",
                stage1={},
                approve_callback=approve,
                require_zero_due=True,
                local_method_strategy="standalone",
                log=logs.append,
            )

        create_mock.assert_not_called()
        confirm_mock.assert_not_called()
        hosted_confirm_mock.assert_not_called()
        approve.assert_not_called()
        self.assertEqual(
            result.get("provider_redirect_url"),
            "https://pay.openai.com/c/pay/cs_test",
        )
        self.assertEqual(
            result.get("fallback_reason"),
            "zero_due_without_server_upi_mandate",
        )
        self.assertFalse(result.get("upi_mandate_available"))
        self.assertEqual(result.get("upi_mandate_source"), "missing")
        self.assertTrue(any("跳过 PaymentMethod" in line for line in logs))

    def test_late_promo_missing_server_mandate_stops_before_approval(self) -> None:
        import provider_checkout as pc

        initial_ctx = {
            "checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        promo_ctx = {**initial_ctx, "checkout_amount": 0}
        initial_data = {
            "payment_method_options": {},
            "total_summary": {"due": 199900},
        }
        promo_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_x", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        confirm_payload = {
            "submission_attempt": {"state": "requires_approval"},
        }
        approve = Mock()
        apply_promo = Mock()

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(
                 pc.sc,
                 "init_checkout",
                 side_effect=[
                     (initial_data, "version", dict(initial_ctx)),
                     (promo_data, "version", dict(promo_ctx)),
                 ],
             ), \
             patch.object(pc.sc, "fetch_elements_session", return_value={}), \
             patch.object(pc.sc, "update_tax_region", return_value={}), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc.sc, "poll_payment_page_after_approve") as poll_mock, \
             patch.object(pc, "confirm_provider_payment", return_value=confirm_payload) as confirm_mock:
            result = pc.stripe_to_provider(
                Mock(),
                "cs_test",
                "upi",
                billing={"name": "A", "address": {"country": "IN"}},
                country="IN",
                chatgpt_http=Mock(),
                access_token="token",
                stage1={},
                approve_callback=approve,
                apply_promo_callback=apply_promo,
                require_zero_due=True,
                local_method_strategy="late_promo",
                log=lambda _m: None,
            )

        confirm_mock.assert_called_once()
        apply_promo.assert_called_once()
        approve.assert_not_called()
        poll_mock.assert_not_called()
        self.assertEqual(
            result.get("fallback_reason"),
            "zero_due_without_server_upi_mandate",
        )
        self.assertEqual(result.get("checkout_amount"), 0)
        self.assertTrue(result.get("promo_applied"))

    def test_nested_server_mandate_continues_to_confirm(self) -> None:
        import provider_checkout as pc

        billing = {
            "name": "Arjun Sharma",
            "email": "test@example.com",
            "address": {"country": "IN"},
        }
        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        init_data = {
            "payment_method_options": {"card": {}},
            "setup_intent": {
                "id": "seti_x",
                "client_secret": "seti_x_secret_y",
                "usage": "off_session",
                "payment_method_options": {
                    "upi": {
                        "mandate_options": {
                            "amount": 199900,
                            "amount_type": "maximum",
                            "description": "Server mandate",
                            "end_date": 1893456000,
                        }
                    }
                },
            },
            "total_summary": {"due": 0},
        }
        confirm_payload = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/ok",
                        "qr_code": {"image_url_png": "https://example.com/upi.png"},
                    },
                },
            },
            "submission_attempt": {"state": "processing"},
        }

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(pc.sc, "init_checkout", return_value=(init_data, "version", dict(ctx))), \
             patch.object(pc.sc, "fetch_elements_session", return_value={}), \
             patch.object(pc.sc, "update_tax_region", return_value={}), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_upi") as create_mock, \
             patch.object(pc, "confirm_provider_payment", return_value=confirm_payload) as confirm_mock:
            result = pc.stripe_to_provider(
                Mock(),
                "cs_test",
                "upi",
                billing=billing,
                country="IN",
                chatgpt_http=Mock(),
                access_token="token",
                stage1={},
                require_zero_due=True,
                local_method_strategy="standalone",
                log=lambda _m: None,
            )

        create_mock.assert_called_once()
        confirm_mock.assert_called_once()
        self.assertEqual(result.get("provider_redirect_url"), "https://hooks.stripe.com/upi/ok")
        self.assertEqual(result.get("qr_image_png"), "https://example.com/upi.png")
        self.assertTrue(result.get("upi_mandate_available"))
        self.assertEqual(result.get("upi_mandate_source"), "server")


if __name__ == "__main__":
    unittest.main()
