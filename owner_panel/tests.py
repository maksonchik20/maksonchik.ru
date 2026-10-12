import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from webhook_tg.models import (
    UserTg,
    WhoUpdatePaymentOrder,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePartnerCommission,
    WhoUpdatePartnerWithdrawal,
)
from webhook_tg.user_metrics import collect_daily_user_metrics
from . import stats, push
from .models import (
    PushSubscription,
    PushEvent,
    PushDelivery,
    NotificationState,
    LoginThrottle,
)
from .views import valid_subscription


@override_settings(
    AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]
)
class PanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            "panel-owner", password="local-test-only"
        )
        cls.staff = get_user_model().objects.create_user("panel-staff", is_staff=True)
        cls.partner = UserTg.objects.create(user_id=1, chat_id=1, username="partner")
        cls.buyer = UserTg.objects.create(
            user_id=2,
            chat_id=2,
            username="buyer",
            referred_by=cls.partner,
            access_unlimited=False,
        )

    def order(self, **kwargs):
        defaults = dict(
            user=self.buyer,
            plan="month",
            duration_days=30,
            amount="100.00",
            status="paid",
            paid_at=timezone.now(),
        )
        defaults.update(kwargs)
        return WhoUpdatePaymentOrder.objects.create(**defaults)

    def test_private_api_and_html(self):
        self.assertEqual(self.client.get("/panel/").status_code, 302)
        self.assertEqual(self.client.get("/panel/api/who/dashboard/").status_code, 401)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/panel/api/who/dashboard/").status_code, 403)
        self.assertEqual(self.client.get("/panel/").status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/panel/").status_code, 200)
        response = self.client.get("/panel/api/who/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_csrf_required_for_mutation(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        self.assertEqual(
            c.post(
                "/panel/api/create-token/", data="{}", content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(
            c.post(
                "/panel/api/push/", data="{}", content_type="application/json"
            ).status_code,
            403,
        )

    def test_login_and_throttle(self):
        self.assertEqual(
            self.client.post(
                "/panel/login/",
                {"username": "panel-owner", "password": "local-test-only"},
            ).status_code,
            302,
        )
        self.client.logout()
        with patch("owner_panel.views.authenticate", return_value=None) as auth:
            for _ in range(10):
                self.client.post(
                    "/panel/login/", {"username": "panel-owner", "password": "bad"}
                )
            self.assertEqual(auth.call_count, 8)
        self.assertEqual(LoginThrottle.objects.get().failures, 8)

    def test_paid_only_test_exclusion_and_referral_balances(self):
        order = self.order()
        self.order(is_test=True, amount="9900")
        self.order(status="pending", paid_at=None, amount="500")
        WhoUpdatePartnerCommission.objects.create(
            order=order,
            partner=self.partner,
            buyer=self.buyer,
            purchase_amount=100,
            commission_percent=30,
            amount=30,
        )
        WhoUpdatePartnerWithdrawal.objects.create(
            partner=self.partner, amount=10, status="paid", resolved_at=timezone.now()
        )
        WhoUpdatePartnerWithdrawal.objects.create(
            partner=self.partner, amount=5, status="new"
        )
        result = stats.dashboard({})
        self.assertEqual(result["summary"]["revenue"], "100.00")
        self.assertEqual(result["summary"]["commission"], "30.00")
        self.assertEqual(result["payments"]["total"], 2)
        referral = stats.referrals()
        self.assertEqual(
            referral["totals"],
            dict(earned="30.00", paid="10.00", due="20.00", reserved="5.00"),
        )
        self.assertEqual(referral["partners"][0]["available"], "15.00")
        self.assertEqual(referral["referrals"][0]["revenue"], "100.00")

    def test_metrics_match_stat_and_moscow_boundary(self):
        now = timezone.now()
        today = timezone.localdate(now)
        at = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        past = at - timedelta(minutes=1)
        for moment in (past, past, past - timedelta(days=1)):
            WhoUpdateOnboardingFunnel.objects.create(
                user=self.buyer,
                telegram_started_at=moment,
                connected_at=moment,
                landing_viewed_at=moment,
                metrika_client_id="same",
            )
        self.buyer.business_disconnected_at = past
        self.buyer.access_expires_at = past
        self.buyer.save()
        target = (today - timedelta(days=1)).isoformat()
        row = stats.dashboard({"from": target, "to": target})["series"][0]
        metric = collect_daily_user_metrics(day=today - timedelta(days=1), at=now)
        self.assertEqual(
            [
                row[k]
                for k in (
                    "visitors",
                    "views",
                    "started",
                    "connected",
                    "disconnected",
                    "expired",
                )
            ],
            [
                metric.unique_landing_visitors,
                metric.landing_views,
                metric.started_users,
                metric.connected_users,
                metric.disconnected_users,
                metric.expired_users,
            ],
        )
        self.assertEqual(row["connected"], 1)

    def test_filters_do_not_change_totals(self):
        self.order()
        self.order(amount="50", status="pending", paid_at=None)
        result = stats.dashboard({"q": "unknown", "status": "pending"})
        self.assertEqual(result["summary"]["revenue"], "100.00")
        self.assertEqual(result["payments"]["total"], 0)

    def test_bad_date_period_returns_400(self):
        self.client.force_login(self.admin)
        for params in (
            {"from": "bad"},
            {"from": "2026-02-02", "to": "2026-01-01"},
            {"from": "1900-01-01"},
        ):
            self.assertEqual(
                self.client.get("/panel/api/who/dashboard/", params).status_code, 400
            )

    def test_create_audit_and_bridge_actor(self):
        self.client.force_login(self.admin)
        data = {"request_id": str(uuid.uuid4()), "actor": "spoof", "user_id": 3}
        with patch(
            "owner_panel.views.ghost_request",
            return_value=({"id": 18, "token": "local-example"}, 201),
        ) as bridge:
            response = self.client.post(
                "/panel/api/create-token/",
                data=json.dumps(data),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(bridge.call_args.kwargs["data"]["actor"], "panel-owner")
            self.assertIs(bridge.call_args.kwargs["data"]["allow_ai"], False)
        self.assertEqual(self.admin.panelaudit_set.count(), 1)

    def test_real_and_test_creation_without_ai_defaults_off(self):
        self.client.force_login(self.admin)
        for token_type in ("real", "test"):
            with self.subTest(token_type=token_type), patch(
                "owner_panel.views.ghost_request", return_value=({"id": 18}, 201)
            ) as bridge:
                response = self.client.post(
                    "/panel/api/create-token/",
                    data=json.dumps({"request_id": str(uuid.uuid4()), "token_type": token_type}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 201)
                self.assertIs(bridge.call_args.kwargs["data"]["allow_ai"], False)

    def test_create_ai_access_requires_explicit_boolean(self):
        self.client.force_login(self.admin)
        for token_type in ("real", "test"):
            for allow_ai in (False, True):
                with self.subTest(token_type=token_type, allow_ai=allow_ai), patch(
                    "owner_panel.views.ghost_request",
                    return_value=({"id": 18}, 201),
                ) as bridge:
                    response = self.client.post(
                        "/panel/api/create-token/",
                        data=json.dumps({"request_id": str(uuid.uuid4()), "user_id": 3,
                                         "token_type": token_type, "allow_ai": allow_ai}),
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 201)
                    self.assertIs(bridge.call_args.kwargs["data"]["allow_ai"], allow_ai)
        with patch("owner_panel.views.ghost_request") as bridge:
            for allow_ai in ("false", "true", 0, 1, None, [], {}):
                response = self.client.post(
                    "/panel/api/create-token/",
                    data=json.dumps({"request_id": str(uuid.uuid4()), "allow_ai": allow_ai}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
            bridge.assert_not_called()

    def test_public_pwa_assets_reveal_no_data(self):
        manifest = self.client.get("/panel/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["scope"], "/panel/")
        sw = self.client.get("/panel/sw.js")
        self.assertEqual(sw.status_code, 200)
        self.assertIn("no-store", sw["Cache-Control"])
        self.assertEqual(sw["Service-Worker-Allowed"], "/panel/")


@override_settings(
    OWNER_PANEL_CONFIG={
        "vapid_private_key": "/tmp/local-test-key",
        "vapid_subject": "https://maksonchik.ru",
        "vapid_public_key": "test",
    }
)
class PushTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("owner")
        self.now = timezone.now()
        self.subscription = {
            "endpoint": "https://web.push.apple.com/test-local",
            "keys": {
                "p256dh": base64.urlsafe_b64encode(b"\x04" + b"x" * 64)
                .decode()
                .rstrip("="),
                "auth": base64.urlsafe_b64encode(b"x" * 16).decode().rstrip("="),
            },
        }
        self.sub = PushSubscription.objects.create(
            user=self.user,
            subscription=self.subscription,
            endpoint_hash=hashlib.sha256(
                self.subscription["endpoint"].encode()
            ).hexdigest(),
            created_at=self.now - timedelta(minutes=1),
        )

    def event(self, **kwargs):
        item = {
            "key": "event-1",
            "category": "who.paid",
            "occurred_at": self.now.isoformat(),
            "payload": {"title": "Payment", "body": "100 ₽", "url": "/panel/"},
        }
        item.update(kwargs)
        return item

    def test_validation_rejects_arbitrary_fetch_targets(self):
        self.assertEqual(valid_subscription(self.subscription), self.subscription)
        for endpoint in [
            "http://web.push.apple.com/a",
            "https://127.0.0.1/a",
            "https://evilpush.apple.com/a",
            "https://web.push.apple.com@evil.ru/a",
            "https://web.push.apple.com:444/a",
        ]:
            with self.assertRaises(ValueError):
                valid_subscription(dict(self.subscription, endpoint=endpoint))

    def test_no_history_for_new_subscribers_and_no_duplicate(self):
        cutoff = self.now - timedelta(minutes=5)
        push.record_events(
            [self.event(occurred_at=(self.now - timedelta(minutes=6)).isoformat())],
            cutoff,
        )
        self.assertEqual(PushEvent.objects.count(), 0)
        push.record_events([self.event()], cutoff)
        push.record_events([self.event()], cutoff)
        self.assertEqual(PushDelivery.objects.count(), 1)
        self.sub.created_at = self.now + timedelta(minutes=1)
        self.sub.save()
        push.record_events([self.event(key="event-2")], cutoff)
        self.assertEqual(PushDelivery.objects.count(), 1)

    def test_success_is_not_repeated(self):
        push.record_events([self.event()], self.now - timedelta(minutes=5))
        with patch("pywebpush.webpush") as send:
            self.assertEqual(push.deliver(), 1)
            self.assertEqual(push.deliver(), 0)
            self.assertEqual(send.call_count, 1)
        row = PushDelivery.objects.get()
        self.assertEqual(row.status, "sent")
        self.assertIsNotNone(row.sent_at)

    def test_retry_and_expired_subscription(self):
        push.record_events([self.event()], self.now - timedelta(minutes=5))
        with patch("pywebpush.webpush", side_effect=TimeoutError("secret endpoint")):
            push.deliver()
        row = PushDelivery.objects.get()
        self.assertEqual(row.status, "pending")
        self.assertNotIn("secret", row.last_error)
        row.next_attempt_at = self.now
        row.save()
        from pywebpush import WebPushException
        import requests

        response = requests.Response()
        response.status_code = 410
        with patch(
            "pywebpush.webpush", side_effect=WebPushException("gone", response=response)
        ):
            push.deliver()
        self.sub.refresh_from_db()
        row.refresh_from_db()
        self.assertFalse(self.sub.enabled)
        self.assertEqual(row.status, "skipped")

    def test_source_outage_keeps_cursor_and_healthy_source_progresses(self):
        past = self.now - timedelta(hours=1)
        NotificationState.objects.create(
            name="ghost", activated_at=past, last_poll_at=past
        )
        with patch(
            "owner_panel.views.ghost_request", return_value=({"error": "offline"}, 502)
        ):
            push.poll_sources()
        self.assertEqual(NotificationState.objects.get(name="ghost").last_poll_at, past)
        self.assertTrue(NotificationState.objects.get(name="ghost").last_error)
        self.assertIsNotNone(NotificationState.objects.get(name="who").last_poll_at)

    def test_who_checkout_and_payment_separate_and_tests_excluded(self):
        buyer = UserTg.objects.create(user_id=123, chat_id=123, username="buyer")
        for is_test in (False, True):
            WhoUpdatePaymentOrder.objects.create(
                user=buyer,
                amount=100,
                plan="month",
                duration_days=30,
                status="paid",
                is_test=is_test,
                checkout_at=self.now,
                paid_at=self.now,
            )
        events = push.who_events(self.now - timedelta(seconds=1))
        self.assertEqual({e["category"] for e in events}, {"who.checkout", "who.paid"})
        self.assertEqual(len(events), 2)

    def test_subscription_and_logout_disable_current_device_only(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/panel/api/push/",
            data=json.dumps(self.subscription),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.client.post(
            "/panel/logout/",
            data=json.dumps({"endpoint": self.subscription["endpoint"]}),
            content_type="application/json",
        )
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.enabled)

    def test_real_webpush_encryption_and_vapid_without_network(self):
        import tempfile
        import requests
        import http_ece
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization

        key = ec.generate_private_key(ec.SECP256R1())
        client_key = ec.generate_private_key(ec.SECP256R1())
        public = client_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        sub = dict(
            self.subscription,
            keys={
                "p256dh": base64.urlsafe_b64encode(public).decode().rstrip("="),
                "auth": self.subscription["keys"]["auth"],
            },
        )
        self.sub.subscription = sub
        self.sub.save()
        push.record_events([self.event()], self.now - timedelta(minutes=5))
        response = requests.Response()
        response.status_code = 201
        response._content = b""
        with tempfile.NamedTemporaryFile() as private_file:
            private_file.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            private_file.flush()
            with override_settings(
                OWNER_PANEL_CONFIG={
                    "vapid_private_key": private_file.name,
                    "vapid_subject": "https://maksonchik.ru",
                }
            ), patch("requests.post", return_value=response) as send:
                self.assertEqual(push.deliver(), 1)
                kwargs = send.call_args.kwargs
                self.assertEqual(kwargs["headers"]["content-encoding"], "aes128gcm")
                self.assertTrue(kwargs["headers"]["Authorization"].startswith("vapid "))
                decoded = http_ece.decrypt(
                    kwargs["data"],
                    private_key=client_key,
                    auth_secret=b"x" * 16,
                    version="aes128gcm",
                )
                self.assertEqual(json.loads(decoded)["tag"], "event-1")


@override_settings(
    AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]
)
class GhostPayoutTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            "payout-owner", password="isolated-test"
        )
        self.staff = get_user_model().objects.create_user("payout-staff", is_staff=True)
        self.payload = dict(id=12, is_paid=True, revision="a" * 64)

    def post(self, data=None):
        return self.client.post(
            "/panel/api/ghost-payout/",
            data=json.dumps(self.payload if data is None else data),
            content_type="application/json",
        )

    def test_only_superuser_with_csrf_can_save(self):
        with patch("owner_panel.views.ghost_request") as bridge:
            self.assertEqual(self.post().status_code, 401)
            self.client.force_login(self.staff)
            self.assertEqual(self.post().status_code, 403)
            c = Client(enforce_csrf_checks=True)
            c.force_login(self.owner)
            self.assertEqual(
                c.post(
                    "/panel/api/ghost-payout/",
                    data=json.dumps(self.payload),
                    content_type="application/json",
                ).status_code,
                403,
            )
            self.client.force_login(self.owner)
            self.assertEqual(
                self.client.get("/panel/api/ghost-payout/").status_code, 405
            )
            bridge.assert_not_called()

    def test_save_proxies_only_validated_fields_and_audits_actor(self):
        from .models import PanelAudit

        self.client.force_login(self.owner)
        for desired in (True, False):
            data = dict(self.payload, is_paid=desired, actor="spoof", amount="99999")
            result = dict(id=12, is_paid=desired, data={"totals": {"due": "100.00"}})
            with patch(
                "owner_panel.views.ghost_request", return_value=(result, 200)
            ) as bridge:
                response = self.post(data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), result)
                self.assertIn("no-store", response["Cache-Control"])
                bridge.assert_called_once_with(
                    "set-payout", data=dict(self.payload, is_paid=desired)
                )
        self.assertEqual(PanelAudit.objects.count(), 2)
        self.assertEqual(
            set(PanelAudit.objects.values_list("action", flat=True)),
            {"ghost.payout_paid", "ghost.payout_unpaid"},
        )
        self.assertFalse(
            PanelAudit.objects.exclude(user=self.owner, object_id="12").exists()
        )

    def test_invalid_payloads_and_bridge_failures_do_not_create_audit(self):
        from .models import PanelAudit

        self.client.force_login(self.owner)
        with patch("owner_panel.views.ghost_request") as bridge:
            for bad in [
                [],
                {},
                dict(self.payload, id=True),
                dict(self.payload, id=2**64),
                dict(self.payload, is_paid="true"),
                dict(self.payload, revision=None),
            ]:
                self.assertEqual(self.post(bad).status_code, 400)
            bridge.assert_not_called()
        for status in (403, 404, 409, 502):
            with patch(
                "owner_panel.views.ghost_request",
                return_value=({"error": "Test failure"}, status),
            ):
                self.assertEqual(self.post().status_code, status)
        self.assertFalse(PanelAudit.objects.exists())


@override_settings(
    AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]
)
class TokenManagementProxyTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            "token-manager", password="isolated"
        )
        self.staff = get_user_model().objects.create_user("token-staff", is_staff=True)
        self.data = dict(
            id=1,
            revision="a" * 64,
            label="Meeting",
            starts_at="2026-09-15T15:00:00+03:00",
            expires_at="2026-09-15T16:00:00+03:00",
            is_active=True,
            allow_local=True,
            allow_remote=False,
        )

    def post(self, data=None):
        return self.client.post(
            "/panel/api/update-token/",
            data=json.dumps(self.data if data is None else data),
            content_type="application/json",
        )

    def test_permissions_and_csrf(self):
        with patch("owner_panel.views.ghost_request") as bridge:
            self.assertEqual(self.post().status_code, 401)
            self.assertEqual(
                self.client.get("/panel/api/ghost/token/?id=1").status_code, 401
            )
            self.client.force_login(self.staff)
            self.assertEqual(self.post().status_code, 403)
            self.assertEqual(
                self.client.get("/panel/api/ghost/token/?id=1").status_code, 403
            )
            c = Client(enforce_csrf_checks=True)
            c.force_login(self.owner)
            self.assertEqual(
                c.post(
                    "/panel/api/update-token/",
                    data=json.dumps(self.data),
                    content_type="application/json",
                ).status_code,
                403,
            )
            bridge.assert_not_called()

    def test_valid_update_audited_to_authenticated_user_and_uncached(self):
        from .models import PanelAudit

        self.client.force_login(self.owner)
        with patch(
            "owner_panel.views.ghost_request",
            return_value=({"id": 1, "token": "isolated-token"}, 200),
        ) as bridge:
            r = self.post()
            self.assertEqual(r.status_code, 200)
            self.assertIn("no-store", r["Cache-Control"])
            bridge.assert_called_once_with("update-token", data=self.data)
            self.assertNotIn("allow_ai", bridge.call_args.kwargs["data"])
        audit = PanelAudit.objects.get()
        self.assertEqual(audit.user, self.owner)
        self.assertEqual((audit.action, audit.object_id), ("ghost.update_token", "1"))
        with patch(
            "owner_panel.views.ghost_request",
            return_value=({"id": 1, "token": "isolated-token"}, 200),
        ):
            r = self.client.get("/panel/api/ghost/token/?id=1")
            self.assertEqual(r.status_code, 200)
            self.assertIn("no-store", r["Cache-Control"])

    def test_invalid_or_failed_updates_have_no_audit(self):
        from .models import PanelAudit

        self.client.force_login(self.owner)
        with patch("owner_panel.views.ghost_request") as bridge:
            for bad in [
                [],
                {},
                dict(self.data, payment_amount=10),
                dict(self.data, token_type="test"),
                dict(self.data, id=True),
                dict(self.data, is_active="false"),
            ]:
                self.assertEqual(self.post(bad).status_code, 400)
            bridge.assert_not_called()
        for status in (400, 404, 409, 502):
            with patch(
                "owner_panel.views.ghost_request",
                return_value=({"error": "Test"}, status),
            ):
                self.assertEqual(self.post().status_code, status)
        self.assertFalse(PanelAudit.objects.exists())

    def test_ai_toggle_preserves_other_access_flags(self):
        self.client.force_login(self.owner)
        for allow_ai in (False, True):
            with self.subTest(allow_ai=allow_ai), patch(
                "owner_panel.views.ghost_request", return_value=({"id": 1}, 200)
            ) as bridge:
                data = dict(self.data, allow_ai=allow_ai)
                self.assertEqual(self.post(data).status_code, 200)
                bridge.assert_called_once_with("update-token", data=data)

    def test_ai_toggle_rejects_non_boolean(self):
        self.client.force_login(self.owner)
        with patch("owner_panel.views.ghost_request") as bridge:
            for allow_ai in ("false", "true", 0, 1, None, [], {}):
                self.assertEqual(self.post(dict(self.data, allow_ai=allow_ai)).status_code, 400)
            bridge.assert_not_called()
