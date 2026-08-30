from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import TestCase
from django.utils import timezone

from .metrika_offline import (
    reconcile_submitted_conversions,
    sync_conversion_queue,
    upload_pending_conversions,
)
from .models import WhoUpdateMetrikaConversion, WhoUpdateOnboardingFunnel


class MetrikaOfflineConversionTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.funnel = WhoUpdateOnboardingFunnel.objects.create(
            utm_source="yandex",
            yclid="yclid-test-123",
            metrika_client_id="client-test-456",
            landing_viewed_at=self.now - timedelta(minutes=10),
            telegram_started_at=self.now - timedelta(minutes=8),
            connected_at=self.now - timedelta(minutes=5),
            connection_stage=WhoUpdateOnboardingFunnel.ConnectionStage.IMMEDIATE,
        )

    def test_sync_backfills_start_and_connection_and_prefers_yclid(self):
        self.assertEqual(sync_conversion_queue(now=self.now), 2)
        rows = list(
            WhoUpdateMetrikaConversion.objects.order_by("event_type").values(
                "event_type",
                "target",
                "identifier_type",
                "identifier",
                "status",
            )
        )
        self.assertEqual(
            {row["event_type"] for row in rows},
            {
                WhoUpdateMetrikaConversion.EventType.START,
                WhoUpdateMetrikaConversion.EventType.CONNECTED,
            },
        )
        self.assertEqual(
            {row["target"] for row in rows},
            {"who_update_start", "who_update_connected"},
        )
        self.assertTrue(
            all(
                row["identifier_type"] == WhoUpdateMetrikaConversion.IdentifierType.YCLID
                and row["identifier"] == "yclid-test-123"
                and row["status"] == WhoUpdateMetrikaConversion.Status.PENDING
                for row in rows
            )
        )
        self.assertEqual(sync_conversion_queue(now=self.now), 0)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 2)

    @patch("webhook_tg.metrika_offline.METRIKA_SESSION.post")
    def test_upload_sends_utf8_csv_and_marks_rows_submitted(self, post):
        sync_conversion_queue(now=self.now)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "uploading": {"id": 778899, "status": "UPLOADED"}
        }
        post.return_value = response

        submitted = upload_pending_conversions(
            token="oauth-test",
            counter_id=111680333,
            now=self.now,
        )

        self.assertEqual(submitted, 2)
        self.assertEqual(post.call_count, 1)
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["Authorization"], "OAuth oauth-test")
        filename, payload, mime = request["files"]["file"]
        csv_text = payload.decode("utf-8")
        self.assertEqual(filename, "who-update-yclid.csv")
        self.assertEqual(mime, "text/csv")
        self.assertIn("Yclid,Target,DateTime", csv_text)
        self.assertIn("yclid-test-123,who_update_start", csv_text)
        self.assertIn("yclid-test-123,who_update_connected", csv_text)
        self.assertFalse(
            WhoUpdateMetrikaConversion.objects.exclude(
                status=WhoUpdateMetrikaConversion.Status.SUBMITTED,
                api_upload_id=778899,
                attempts=1,
            ).exists()
        )

    @patch("webhook_tg.metrika_offline.METRIKA_SESSION.get")
    def test_reconcile_marks_processed_upload(self, get):
        sync_conversion_queue(now=self.now)
        WhoUpdateMetrikaConversion.objects.update(
            status=WhoUpdateMetrikaConversion.Status.SUBMITTED,
            api_upload_id=778899,
            api_status="UPLOADED",
            submitted_at=self.now - timedelta(minutes=2),
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "uploading": {"id": 778899, "status": "PROCESSED"}
        }
        get.return_value = response

        self.assertEqual(
            reconcile_submitted_conversions(
                token="oauth-test",
                counter_id=111680333,
                now=self.now,
            ),
            2,
        )
        self.assertFalse(
            WhoUpdateMetrikaConversion.objects.exclude(
                status=WhoUpdateMetrikaConversion.Status.PROCESSED,
                api_status="PROCESSED",
                processed_at=self.now,
            ).exists()
        )

    @patch("webhook_tg.metrika_offline.METRIKA_SESSION.post")
    def test_failed_request_remains_pending_with_backoff(self, post):
        sync_conversion_queue(now=self.now)
        post.side_effect = requests.Timeout("timeout")

        self.assertEqual(
            upload_pending_conversions(
                token="oauth-test",
                counter_id=111680333,
                now=self.now,
            ),
            0,
        )
        rows = WhoUpdateMetrikaConversion.objects.all()
        self.assertFalse(rows.exclude(status=WhoUpdateMetrikaConversion.Status.PENDING).exists())
        self.assertFalse(rows.exclude(attempts=1).exists())
        self.assertTrue(all(row.next_attempt_at > self.now for row in rows))
        self.assertTrue(all("timeout" in row.last_error for row in rows))
