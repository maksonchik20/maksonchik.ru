from unittest.mock import Mock, patch

from django.test import SimpleTestCase
import requests

from . import telegram


class TelegramFallbackTests(SimpleTestCase):
    def setUp(self):
        self.failures = patch.dict(telegram._endpoint_failures, {}, clear=True)
        self.failures.start()
        self.addCleanup(self.failures.stop)
        preferred = patch.object(telegram, "_preferred_endpoint", None)
        preferred.start()
        self.addCleanup(preferred.stop)

    def test_ip_connections_keep_telegram_sni_and_certificate_verification(self):
        for endpointFieldToValue in telegram.TELEGRAM_API_ENDPOINTS:
            self.assertTrue(endpointFieldToValue["verify"])
            if not endpointFieldToValue["host"]:
                continue
            url = endpointFieldToValue["base"] + "/bot-test/getFile"
            adapter = telegram.TELEGRAM_SESSION.get_adapter(url)
            request = requests.Request("POST", url).prepare()
            pool = adapter.get_connection_with_tls_context(request, verify=True)
            self.assertEqual(pool.host, url.split("/")[2])
            self.assertEqual(pool.conn_kw["server_hostname"], "api.telegram.org")
            self.assertEqual(pool.assert_hostname, "api.telegram.org")
            self.assertEqual(pool.cert_reqs, "CERT_REQUIRED")

    def test_failed_primary_uses_verified_fallback_and_cools_down(self):
        response = Mock()
        response.json.return_value = {"ok": True}
        with patch.object(telegram.TELEGRAM_SESSION, "post", side_effect=[
            requests.ConnectionError("primary unreachable"), response, response,
        ]) as post:
            self.assertIs(telegram._telegram_post("getFile", json={"file_id": "test"}), response)
            self.assertIs(telegram._telegram_post("getFile", json={"file_id": "test"}), response)
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list[1:]:
            self.assertTrue(call.kwargs["verify"])
            self.assertEqual(call.kwargs["headers"]["Host"], "api.telegram.org")
            self.assertIn("149.154.167.220", call.args[0])

    def test_open_circuit_skips_http_and_retries_after_cooldown(self):
        with patch("webhook_tg.telegram.time.monotonic", return_value=100):
            for endpointFieldToValue in telegram.TELEGRAM_API_ENDPOINTS:
                telegram._mark_endpoint_failed(endpointFieldToValue["base"])
            with patch.object(telegram.TELEGRAM_SESSION, "post") as post:
                with self.assertRaisesMessage(requests.ConnectionError, "circuit breaker"):
                    telegram._telegram_post("getFile", json={"file_id": "test"})
                post.assert_not_called()
        with (
            patch("webhook_tg.telegram.time.monotonic", return_value=131),
            patch.object(telegram.TELEGRAM_SESSION, "post") as post,
        ):
            telegram._telegram_post("getFile", json={"file_id": "test"})
            post.assert_called_once()

    def test_known_working_route_stays_first_after_failed_primary_cooldown(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True}
        with patch("webhook_tg.telegram.time.monotonic", return_value=100), patch.object(
            telegram.TELEGRAM_SESSION, "post", side_effect=[requests.ConnectionError("primary unreachable"), response],
        ):
            telegram._telegram_post("getMe", json={})
        with patch("webhook_tg.telegram.time.monotonic", return_value=200), patch.object(
            telegram.TELEGRAM_SESSION, "post", return_value=response,
        ) as post:
            telegram._telegram_post("getMe", json={})
        self.assertIn("149.154.167.220", post.call_args.args[0])
        self.assertTrue(post.call_args.kwargs["verify"])

    def test_server_error_uses_another_route(self):
        failed = Mock(status_code=502)
        success = Mock(status_code=200)
        success.json.return_value = {"ok": True}
        with patch.object(telegram.TELEGRAM_SESSION, "post", side_effect=[failed, success]) as post:
            self.assertIs(telegram._telegram_post("getMe", json={}), success)
        self.assertEqual(post.call_count, 2)
