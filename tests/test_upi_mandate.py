from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

import provider_checkout


class MandateHelpersTests(unittest.TestCase):
    def test_upi_provider_action_requires_real_next_action(self) -> None:
        self.assertFalse(provider_checkout.provider_has_action({
            "provider": "upi",
            "provider_redirect_url": "https://hooks.stripe.com/upi/real",
        }))
        for checkout_url in (
            "https://pay.openai.com/c/pay/cs_test",
            "https://checkout.stripe.com/c/pay/cs_test",
        ):
            with self.subTest(checkout_url=checkout_url):
                self.assertFalse(provider_checkout.provider_has_action({
                    "provider": "upi",
                    "provider_redirect_url": checkout_url,
                    "next_action_type": provider_checkout.UPI_NEXT_ACTION_TYPE,
                }))
        self.assertTrue(provider_checkout.provider_has_action({
            "provider": "upi",
            "qr_image_svg": "https://example.com/upi.svg",
            "next_action_type": provider_checkout.UPI_NEXT_ACTION_TYPE,
        }))

    def test_action_source_is_bound_to_the_selected_action(self) -> None:
        selected = {
            "type": provider_checkout.UPI_NEXT_ACTION_TYPE,
            provider_checkout.UPI_NEXT_ACTION_TYPE: {
                "hosted_instructions_url": "https://hooks.stripe.com/upi/top-level",
                "qr_code": {},
            },
        }
        payload = {
            "next_action": selected,
            "setup_intent": {
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": "https://example.com/unrelated"},
                }
            },
        }

        result = provider_checkout.extract_provider_result(payload, "upi")

        self.assertEqual(result["next_action_type"], provider_checkout.UPI_NEXT_ACTION_TYPE)
        self.assertEqual(result["next_action_intent_kind"], "payment_page")
        self.assertEqual(
            result["provider_redirect_url"],
            "https://hooks.stripe.com/upi/top-level",
        )

    def test_terminal_failure_is_detected_even_with_stale_upi_action(self) -> None:
        payload = {
            "submission_attempt": {"state": "failed"},
            "setup_intent": {
                "status": "requires_payment_method",
                "last_setup_error": {"code": "setup_attempt_failed"},
                "next_action": {
                    "type": provider_checkout.UPI_NEXT_ACTION_TYPE,
                    provider_checkout.UPI_NEXT_ACTION_TYPE: {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/stale",
                        "qr_code": {},
                    },
                },
            },
        }

        result = provider_checkout.extract_provider_result(payload, "upi")

        self.assertTrue(provider_checkout.provider_has_action(result))
        detail = provider_checkout.provider_terminal_failure_detail(payload)
        self.assertIn("submission_state=failed", detail)
        self.assertIn("decline_code=setup_attempt_failed", detail)

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
    @staticmethod
    def _upi_ctx(amount, *, currency: str = "inr", methods=None) -> dict:
        return {
            "checkout_amount": amount,
            "payment_method_types": list(methods or ["card", "upi"]),
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": currency,
        }

    @staticmethod
    def _upi_action(url: str, *, image_url: str = "") -> dict:
        return {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": provider_checkout.UPI_NEXT_ACTION_TYPE,
                    provider_checkout.UPI_NEXT_ACTION_TYPE: {
                        "hosted_instructions_url": url,
                        "qr_code": {"image_url_png": image_url} if image_url else {},
                    },
                },
            },
            "submission_attempt": {"state": "processing"},
        }

    def test_missing_server_mandate_raises_without_fabricated_hosted_url(self) -> None:
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
            with self.assertRaisesRegex(RuntimeError, "缺少服务端 AutoPay mandate"):
                pc.stripe_to_provider(
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
        self.assertTrue(any("无法安全提交" in line for line in logs))

    def test_terminal_failure_overrides_stale_upi_action(self) -> None:
        import provider_checkout as pc

        ctx = self._upi_ctx(199900)
        init_data = {"total_summary": {"due": 199900}}
        failed = self._upi_action("https://hooks.stripe.com/upi/stale-failed")
        failed["submission_attempt"] = {"state": "failed"}
        failed["setup_intent"].update({
            "status": "requires_payment_method",
            "last_setup_error": {"code": "setup_attempt_failed"},
        })

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(pc.sc, "init_checkout", return_value=(init_data, "version", ctx)), \
             patch.object(pc.sc, "fetch_elements_session", return_value={}), \
             patch.object(pc.sc, "update_tax_region", return_value={}), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "confirm_upi_hosted_minimal", return_value=failed):
            with self.assertRaisesRegex(
                RuntimeError,
                "submission_state=failed.*decline_code=setup_attempt_failed",
            ):
                pc.stripe_to_provider(
                    Mock(),
                    "cs_test",
                    "upi",
                    billing={"name": "A", "address": {"country": "IN"}},
                    country="IN",
                    chatgpt_http=Mock(),
                    access_token="token",
                    stage1={},
                    require_zero_due=False,
                    local_method_strategy="hosted_minimal",
                    log=lambda _message: None,
                )

    def test_reinit_uses_fresh_snapshot_without_old_failure(self) -> None:
        import provider_checkout as pc

        initial_ctx = self._upi_ctx(199900)
        reinit_ctx = self._upi_ctx(199900)
        initial_data = {"total_summary": {"due": 199900}}
        old_failure = {
            "submission_attempt": {"state": "processing"},
            "setup_intent": {
                "status": "requires_payment_method",
                "last_setup_error": {"code": "setup_attempt_failed"},
            },
        }
        fresh_action = self._upi_action("https://hooks.stripe.com/upi/fresh-reinit")
        approve = Mock(return_value={"result": "approved"})

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(
                 pc.sc,
                 "init_checkout",
                 side_effect=[
                     (initial_data, "initial-version", initial_ctx),
                     (fresh_action, "reinit-version", reinit_ctx),
                 ],
             ), \
             patch.object(pc.sc, "fetch_elements_session", side_effect=[{}, {}]), \
             patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}]), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(
                 pc.sc,
                 "poll_payment_page_after_approve",
                 return_value=old_failure,
             ), \
             patch.object(
                 pc,
                 "confirm_upi_hosted_minimal",
                 return_value={"submission_attempt": {"state": "requires_approval"}},
             ), \
             patch.object(pc, "recover_upi_via_payment_page") as recover_mock:
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
                require_zero_due=False,
                local_method_strategy="hosted_minimal",
                log=lambda _message: None,
            )

        approve.assert_called_once_with("openai_llc")
        recover_mock.assert_not_called()
        self.assertEqual(
            result["provider_redirect_url"],
            "https://hooks.stripe.com/upi/fresh-reinit",
        )
        self.assertEqual(result["next_action_intent_kind"], "setup_intent")

    def test_late_promo_missing_server_mandate_continues_approval_and_poll(self) -> None:
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
        poll_payload = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/late-promo",
                        "qr_code": {"image_url_png": "https://example.com/late-promo.png"},
                    },
                },
            },
            "submission_attempt": {"state": "processing"},
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
             patch.object(pc.sc, "fetch_elements_session", side_effect=[{}, {}]) as elements_mock, \
             patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}]) as tax_mock, \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc.sc, "poll_payment_page_after_approve", return_value=poll_payload) as poll_mock, \
             patch.object(pc, "create_provider_payment_method", return_value="pm_late") as create_mock, \
             patch.object(
                 pc,
                 "recover_upi_via_payment_page",
                 return_value={"submission_attempt": {"state": "requires_approval"}},
             ) as reconfirm_mock, \
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

        create_mock.assert_called_once()
        confirm_mock.assert_called_once()
        apply_promo.assert_called_once()
        elements_mock.assert_called()
        self.assertEqual(elements_mock.call_count, 2)
        self.assertEqual(tax_mock.call_count, 2)
        reconfirm_mock.assert_called_once()
        self.assertIsNone(reconfirm_mock.call_args.kwargs["approve_callback"])
        self.assertEqual(reconfirm_mock.call_args.args[4].get("payment_method_id"), "pm_late")
        approve.assert_called_once_with("openai_llc")
        poll_mock.assert_called_once()
        self.assertIs(poll_mock.call_args.kwargs["ctx"], reconfirm_mock.call_args.args[4])
        self.assertNotIn("fallback_reason", result)
        self.assertEqual(result.get("provider_redirect_url"), "https://hooks.stripe.com/upi/late-promo")
        self.assertEqual(result.get("qr_image_png"), "https://example.com/late-promo.png")
        self.assertEqual(result.get("checkout_amount"), 0)
        self.assertTrue(result.get("promo_applied"))
        self.assertFalse(result.get("upi_mandate_available"))
        self.assertEqual(result.get("upi_mandate_source"), "missing")

    def test_late_promo_reuses_standalone_payment_method_through_recovery(self) -> None:
        import provider_checkout as pc

        initial_ctx = self._upi_ctx(199900)
        promo_ctx = self._upi_ctx(0)
        reinit_ctx = self._upi_ctx(0)
        initial_data = {
            "payment_method_options": {},
            "total_summary": {"due": 199900},
        }
        promo_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_promo", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        reinit_data = self._upi_action(
            "https://hooks.stripe.com/upi/recovered-pm",
            image_url="https://example.com/recovered-pm.png",
        )
        reinit_data["total_summary"] = {"due": 0}
        lifecycle: list[str] = []

        def create_payment_method(*_args, **_kwargs):
            lifecycle.append("create")
            return "pm_late"

        def initial_confirm(*_args, **_kwargs):
            lifecycle.append("initial_confirm")
            return {"submission_attempt": {"state": "requires_approval"}}

        def apply_promo(_processor):
            lifecycle.append("promo")

        def zero_reconfirm(*_args, **_kwargs):
            lifecycle.append("zero_reconfirm")
            return {"submission_attempt": {"state": "requires_approval"}}

        def approve(_processor):
            lifecycle.append("approve")

        def poll_after_approve(*_args, **_kwargs):
            lifecycle.append("poll")
            return {"submission_attempt": {"state": "processing"}}

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(
                 pc.sc,
                 "init_checkout",
                 side_effect=[
                     (initial_data, "initial-version", initial_ctx),
                     (promo_data, "promo-version", promo_ctx),
                     (reinit_data, "reinit-version", reinit_ctx),
                 ],
             ), \
             patch.object(pc.sc, "fetch_elements_session", side_effect=[{}, {}, {}]), \
             patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}, {}]), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(
                 pc.sc,
                 "poll_payment_page_after_approve",
                 side_effect=poll_after_approve,
             ) as poll_mock, \
             patch.object(
                 pc,
                 "create_provider_payment_method",
                 side_effect=create_payment_method,
             ) as create_mock, \
             patch.object(
                 pc,
                 "confirm_provider_payment",
                 side_effect=initial_confirm,
             ) as confirm_mock, \
             patch.object(
                 pc,
                 "recover_upi_via_payment_page",
                 side_effect=zero_reconfirm,
             ) as recover_mock:
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
                log=lambda _message: None,
            )

        create_mock.assert_called_once()
        confirm_mock.assert_called_once()
        self.assertEqual(
            lifecycle,
            ["create", "initial_confirm", "promo", "zero_reconfirm", "approve", "poll"],
        )
        self.assertEqual(confirm_mock.call_args.kwargs["payment_method_id"], "pm_late")
        self.assertEqual(promo_ctx.get("payment_method_id"), "pm_late")
        self.assertIs(poll_mock.call_args.kwargs["ctx"], promo_ctx)
        recover_mock.assert_called_once()
        recover_ctx = recover_mock.call_args.args[4]
        self.assertIs(recover_ctx, promo_ctx)
        self.assertEqual(recover_ctx.get("payment_method_id"), "pm_late")
        self.assertIsNone(recover_mock.call_args.kwargs["approve_callback"])
        self.assertEqual(reinit_ctx.get("payment_method_id"), "pm_late")
        self.assertEqual(result.get("provider_redirect_url"), "https://hooks.stripe.com/upi/recovered-pm")

    def test_late_promo_none_or_empty_promo_amount_is_not_zero(self) -> None:
        import provider_checkout as pc

        for promo_amount in (None, ""):
            with self.subTest(promo_amount=promo_amount):
                initial_ctx = self._upi_ctx(199900)
                promo_ctx = self._upi_ctx(promo_amount)
                initial_data = {
                    "payment_method_options": {},
                    "total_summary": {"due": 199900},
                }
                promo_data = {
                    "payment_method_options": {},
                    "setup_intent": {"id": "seti_promo", "usage": "off_session"},
                    "total_summary": {"due": promo_amount},
                }
                approve = Mock()
                apply_promo = Mock()

                with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
                     patch.object(
                         pc.sc,
                         "init_checkout",
                         side_effect=[
                             (initial_data, "version", dict(initial_ctx)),
                             (promo_data, "promo-version", dict(promo_ctx)),
                         ],
                     ), \
                     patch.object(pc.sc, "fetch_elements_session", side_effect=[{}, {}]), \
                     patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}]), \
                     patch.object(pc.sc, "snapshot_billing", return_value=None), \
                     patch.object(pc.sc, "poll_payment_page_after_approve") as poll_mock, \
                     patch.object(
                         pc,
                         "create_provider_payment_method",
                         return_value="pm_late",
                     ) as create_mock, \
                     patch.object(
                         pc,
                         "confirm_provider_payment",
                         return_value={"submission_attempt": {"state": "requires_approval"}},
                     ):
                    with self.assertRaisesRegex(RuntimeError, "延后应用优惠未归零"):
                        pc.stripe_to_provider(
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
                            log=lambda _message: None,
                        )

                create_mock.assert_called_once()
                apply_promo.assert_called_once_with("openai_llc")
                approve.assert_not_called()
                poll_mock.assert_not_called()

    def test_late_promo_requires_explicit_positive_initial_amount(self) -> None:
        import provider_checkout as pc

        for initial_amount in (None, "", 0, "0.00", -1, "unknown"):
            with self.subTest(initial_amount=initial_amount):
                initial_ctx = self._upi_ctx(initial_amount)
                init_data = {
                    "payment_method_options": {},
                    "total_summary": {"due": initial_amount},
                }
                approve = Mock()
                apply_promo = Mock()

                with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
                     patch.object(
                         pc.sc,
                         "init_checkout",
                         return_value=(init_data, "version", dict(initial_ctx)),
                     ), \
                     patch.object(pc.sc, "fetch_elements_session", return_value={}), \
                     patch.object(pc.sc, "update_tax_region", return_value={}), \
                     patch.object(pc.sc, "snapshot_billing", return_value=None), \
                     patch.object(
                         pc,
                         "create_provider_payment_method",
                         return_value="pm_late",
                     ) as create_mock, \
                     patch.object(pc, "confirm_provider_payment") as confirm_mock:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "late_promo_requires_positive_initial_amount",
                    ):
                        pc.stripe_to_provider(
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
                            log=lambda _message: None,
                        )

                create_mock.assert_not_called()
                confirm_mock.assert_not_called()
                apply_promo.assert_not_called()
                approve.assert_not_called()

    def test_late_promo_rejects_nonzero_amount_from_post_approval_reinit(self) -> None:
        import provider_checkout as pc

        initial_ctx = self._upi_ctx(199900)
        promo_ctx = self._upi_ctx(0)
        reinit_ctx = self._upi_ctx(199900)
        initial_data = {"payment_method_options": {}, "total_summary": {"due": 199900}}
        promo_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_promo", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        reinit_data = self._upi_action("https://hooks.stripe.com/upi/repriced")
        reinit_data["total_summary"] = {"due": 199900}
        approve = Mock()
        apply_promo = Mock()

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(
                 pc.sc,
                 "init_checkout",
                 side_effect=[
                     (initial_data, "initial-version", dict(initial_ctx)),
                     (promo_data, "promo-version", dict(promo_ctx)),
                     (reinit_data, "reinit-version", reinit_ctx),
                 ],
             ), \
             patch.object(pc.sc, "fetch_elements_session", side_effect=[{}, {}, {}]), \
             patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}, {}]), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_late") as create_mock, \
             patch.object(
                 pc,
                 "recover_upi_via_payment_page",
                 return_value={"submission_attempt": {"state": "requires_approval"}},
             ) as reconfirm_mock, \
             patch.object(
                 pc.sc,
                 "poll_payment_page_after_approve",
                 return_value={"submission_attempt": {"state": "processing"}},
             ), \
             patch.object(
                 pc,
                 "confirm_provider_payment",
                 return_value={"submission_attempt": {"state": "requires_approval"}},
             ):
            with self.assertRaisesRegex(RuntimeError, "upi_latest_checkout_amount_not_zero"):
                pc.stripe_to_provider(
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
                    log=lambda _message: None,
                )

        create_mock.assert_called_once()
        reconfirm_mock.assert_called_once()
        apply_promo.assert_called_once_with("openai_llc")
        approve.assert_called_once_with("openai_llc")

    def test_post_approval_reinit_adopts_new_ctx_and_provider_options(self) -> None:
        import provider_checkout as pc

        initial_ctx = self._upi_ctx(199900, currency="usd")
        promo_ctx = self._upi_ctx(0, currency="usd")
        reinit_ctx = self._upi_ctx(0, currency="inr", methods=["upi"])
        initial_data = {"payment_method_options": {}, "total_summary": {"due": 199900}}
        promo_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_promo", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        reinit_data = self._upi_action("https://hooks.stripe.com/upi/reinit")
        reinit_data["total_summary"] = {"due": 0}
        reinit_elements = {
            "payment_method_options": {
                "upi": {
                    "setup_future_usage": "off_session",
                    "mandate_options": {
                        "amount": 177000,
                        "amount_type": "maximum",
                    },
                }
            }
        }
        reinit_tax = {
            "setup_intent": {
                "payment_method_options": {
                    "upi": {
                        "mandate_options": {"description": "Re-init mandate"},
                    }
                }
            }
        }

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(
                 pc.sc,
                 "init_checkout",
                 side_effect=[
                     (initial_data, "initial-version", dict(initial_ctx)),
                     (promo_data, "promo-version", dict(promo_ctx)),
                     (reinit_data, "reinit-version", reinit_ctx),
                 ],
             ), \
             patch.object(
                 pc.sc,
                 "fetch_elements_session",
                 side_effect=[{}, {}, reinit_elements],
             ), \
             patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}, reinit_tax]), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_late") as create_mock, \
             patch.object(
                 pc,
                 "recover_upi_via_payment_page",
                 return_value={"submission_attempt": {"state": "requires_approval"}},
             ) as reconfirm_mock, \
             patch.object(
                 pc.sc,
                 "poll_payment_page_after_approve",
                 return_value={"submission_attempt": {"state": "processing"}},
             ), \
             patch.object(
                 pc,
                 "confirm_provider_payment",
                 return_value={"submission_attempt": {"state": "requires_approval"}},
             ):
            result = pc.stripe_to_provider(
                Mock(),
                "cs_test",
                "upi",
                billing={"name": "A", "address": {"country": "IN"}},
                country="IN",
                chatgpt_http=Mock(),
                access_token="token",
                stage1={},
                approve_callback=Mock(),
                apply_promo_callback=Mock(),
                require_zero_due=True,
                local_method_strategy="late_promo",
                log=lambda _message: None,
            )

        create_mock.assert_called_once()
        reconfirm_mock.assert_called_once()
        options = reinit_ctx["provider_payment_method_options"]
        self.assertEqual(options["setup_future_usage"], "off_session")
        self.assertEqual(options["mandate_options"]["amount"], 177000)
        self.assertEqual(options["mandate_options"]["description"], "Re-init mandate")
        self.assertEqual(result.get("checkout_amount"), 0)
        self.assertEqual(result.get("checkout_currency"), "INR")
        self.assertEqual(result.get("payment_method_types"), ["upi"])
        self.assertTrue(result.get("upi_mandate_available"))
        self.assertEqual(result.get("upi_mandate_source"), "server")
        self.assertEqual(result.get("provider_redirect_url"), "https://hooks.stripe.com/upi/reinit")

    def test_late_promo_does_not_accept_action_before_promo_is_applied(self) -> None:
        import provider_checkout as pc

        ctx = {
            "checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        init_data = {
            "payment_method_options": {},
            "total_summary": {"due": 199900},
        }
        paid_action = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/paid",
                        "qr_code": {},
                    },
                },
            }
        }
        approve = Mock()
        apply_promo = Mock()

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(pc.sc, "init_checkout", return_value=(init_data, "version", dict(ctx))), \
             patch.object(pc.sc, "fetch_elements_session", return_value={}), \
             patch.object(pc.sc, "update_tax_region", return_value={}), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_late") as create_mock, \
             patch.object(pc, "confirm_provider_payment", return_value=paid_action):
            with self.assertRaisesRegex(RuntimeError, "late_promo_requires_approval"):
                pc.stripe_to_provider(
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
                    log=lambda _message: None,
                )

        create_mock.assert_called_once()
        apply_promo.assert_not_called()
        approve.assert_not_called()

    def test_zero_due_upi_accepts_mandate_only_from_elements_response(self) -> None:
        import provider_checkout as pc

        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        init_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_elements", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        elements_data = {
            "payment_method_object": {
                "setup_intent": {
                    "payment_method_options": {
                        "upi": {
                            "mandate_options": {
                                "amount": 175000,
                                "amount_type": "maximum",
                                "description": "Elements mandate",
                            }
                        }
                    }
                }
            }
        }
        confirm_payload = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/elements",
                        "qr_code": {},
                    },
                },
            }
        }

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(pc.sc, "init_checkout", return_value=(init_data, "version", dict(ctx))), \
             patch.object(pc.sc, "fetch_elements_session", return_value=elements_data), \
             patch.object(pc.sc, "update_tax_region", return_value={}), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_upi") as create_mock, \
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
                require_zero_due=True,
                local_method_strategy="standalone",
                log=lambda _m: None,
            )

        create_mock.assert_called_once()
        confirm_mock.assert_called_once()
        confirm_ctx = confirm_mock.call_args.args[6]
        mandate = confirm_ctx["provider_payment_method_options"]["mandate_options"]
        self.assertEqual(mandate["amount"], 175000)
        self.assertEqual(mandate["description"], "Elements mandate")
        self.assertTrue(result.get("upi_mandate_available"))
        self.assertEqual(result.get("upi_mandate_source"), "server")

    def test_zero_due_upi_accepts_mandate_only_from_tax_response(self) -> None:
        import provider_checkout as pc

        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        init_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_tax", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        tax_data = {
            "setup_intent": {
                "payment_method_options": {
                    "upi": {
                        "mandate_options": {
                            "amount": 185000,
                            "amount_type": "fixed",
                            "description": "Tax update mandate",
                        }
                    }
                }
            }
        }
        confirm_payload = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/tax",
                        "qr_code": {},
                    },
                },
            }
        }

        with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
             patch.object(pc.sc, "init_checkout", return_value=(init_data, "version", dict(ctx))), \
             patch.object(pc.sc, "fetch_elements_session", return_value={}), \
             patch.object(pc.sc, "update_tax_region", return_value=tax_data), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_upi") as create_mock, \
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
                require_zero_due=True,
                local_method_strategy="standalone",
                log=lambda _m: None,
            )

        create_mock.assert_called_once()
        confirm_mock.assert_called_once()
        confirm_ctx = confirm_mock.call_args.args[6]
        mandate = confirm_ctx["provider_payment_method_options"]["mandate_options"]
        self.assertEqual(mandate["amount"], 185000)
        self.assertEqual(mandate["description"], "Tax update mandate")
        self.assertTrue(result.get("upi_mandate_available"))
        self.assertEqual(result.get("upi_mandate_source"), "server")

    def test_promo_reinit_resets_old_intent_mandate_before_new_cycle(self) -> None:
        import provider_checkout as pc

        old_options = {
            "setup_future_usage": "off_session",
            "mandate_options": {
                "amount": 199900,
                "amount_type": "fixed",
                "description": "Stale intent mandate",
                "end_date": 1893456000,
            },
        }
        initial_ctx = {
            "checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        promo_ctx = {
            **initial_ctx,
            "checkout_amount": 0,
            # Simulate a reused context object; reset=True must discard this.
            "provider_payment_method_options": dict(old_options),
        }
        initial_data = {
            "setup_intent": {
                "id": "seti_old",
                "payment_method_options": {"upi": old_options},
            },
            "total_summary": {"due": 199900},
        }
        promo_data = {
            "payment_method_options": {},
            "setup_intent": {"id": "seti_new", "usage": "off_session"},
            "total_summary": {"due": 0},
        }
        new_elements_data = {
            "payment_method_options": {
                "upi": {
                    "mandate_options": {
                        "amount": 160000,
                        "amount_type": "maximum",
                    }
                }
            }
        }
        confirm_payload = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://hooks.stripe.com/upi/new-cycle",
                        "qr_code": {},
                    },
                },
            }
        }
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
             patch.object(pc.sc, "fetch_elements_session", side_effect=[{}, new_elements_data]), \
             patch.object(pc.sc, "update_tax_region", side_effect=[{}, {}]), \
             patch.object(pc.sc, "snapshot_billing", return_value=None), \
             patch.object(pc, "create_provider_payment_method", return_value="pm_upi"), \
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
                apply_promo_callback=apply_promo,
                require_zero_due=True,
                local_method_strategy="standalone",
                log=lambda _m: None,
            )

        apply_promo.assert_called_once_with("openai_llc")
        confirm_mock.assert_called_once()
        confirm_ctx = confirm_mock.call_args.args[6]
        options = confirm_ctx["provider_payment_method_options"]
        self.assertEqual(options["mandate_options"]["amount"], 160000)
        self.assertEqual(options["mandate_options"]["amount_type"], "maximum")
        self.assertNotIn("description", options["mandate_options"])
        self.assertNotIn("end_date", options["mandate_options"])
        self.assertNotIn("setup_future_usage", options)
        self.assertEqual(result.get("provider_redirect_url"), "https://hooks.stripe.com/upi/new-cycle")

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

    def test_zero_due_upi_rejects_payment_intent_action_and_accepts_setup_intent_action(self) -> None:
        import provider_checkout as pc

        ctx = {
            "checkout_amount": 0,
            "original_checkout_amount": 199900,
            "payment_method_types": ["card", "upi"],
            "return_url": "https://chatgpt.com/checkout/openai_llc/session",
            "currency": "inr",
        }
        init_data = {
            "setup_intent": {
                "id": "seti_zero",
                "usage": "off_session",
                "payment_method_options": {
                    "upi": {
                        "mandate_options": {
                            "amount": 199900,
                            "amount_type": "maximum",
                        }
                    }
                },
            },
            "total_summary": {"due": 0},
        }

        def run_with_action(intent_kind: str) -> dict:
            action = self._upi_action(f"https://hooks.stripe.com/upi/{intent_kind}")
            confirm_payload = {intent_kind: action["setup_intent"]}
            with patch.object(pc.sc, "verify_pk", return_value="pk_test"), \
                 patch.object(
                     pc.sc,
                     "init_checkout",
                     return_value=(init_data, "version", dict(ctx)),
                 ), \
                 patch.object(pc.sc, "fetch_elements_session", return_value={}), \
                 patch.object(pc.sc, "update_tax_region", return_value={}), \
                 patch.object(pc.sc, "snapshot_billing", return_value=None), \
                 patch.object(pc, "create_provider_payment_method", return_value="pm_upi"), \
                 patch.object(pc, "confirm_provider_payment", return_value=confirm_payload):
                return pc.stripe_to_provider(
                    Mock(),
                    "cs_test",
                    "upi",
                    billing={"name": "A", "address": {"country": "IN"}},
                    country="IN",
                    chatgpt_http=Mock(),
                    access_token="token",
                    stage1={},
                    require_zero_due=True,
                    local_method_strategy="standalone",
                    log=lambda _message: None,
                )

        with self.assertRaisesRegex(
            RuntimeError,
            "upi_zero_due_action_not_setup_intent:payment_intent",
        ):
            run_with_action("payment_intent")

        result = run_with_action("setup_intent")
        self.assertEqual(result.get("next_action_intent_kind"), "setup_intent")
        self.assertEqual(
            result.get("provider_redirect_url"),
            "https://hooks.stripe.com/upi/setup_intent",
        )


if __name__ == "__main__":
    unittest.main()
