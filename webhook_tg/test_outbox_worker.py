import signal
from contextlib import ExitStack
from io import StringIO
from unittest import TestCase
from unittest.mock import Mock, call, patch

from django.core.management import call_command

from webhook_tg.management.commands import process_tg_outbox


class OutboxWorkerTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.process = self.stack.enter_context(patch.object(process_tg_outbox, "process_outbox"))
        self.heartbeat = self.stack.enter_context(patch.object(process_tg_outbox, "record_heartbeat"))
        self.close_connections = self.stack.enter_context(patch.object(process_tg_outbox, "close_old_connections"))
        self.logger = self.stack.enter_context(patch.object(process_tg_outbox, "logger"))
        self.signalToHandler = {}
        self.signalToPreviousHandler = {
            signal.SIGTERM: Mock(),
            signal.SIGINT: Mock(),
        }

        def register_signal(signum, handler):
            self.signalToHandler[signum] = handler
            return self.signalToPreviousHandler[signum]

        self.register_signal = self.stack.enter_context(
            patch.object(process_tg_outbox.signal, "signal", side_effect=register_signal)
        )
        self.output = StringIO()
        self.process.return_value = self.counts()

    @staticmethod
    def counts(*, processed=0, sent=0, failed=0, pending=0):
        return {"processed": processed, "sent": sent, "failed": failed, "pending": pending}

    def run_loop(self, *, iterations=2, **optionToValue):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False] * iterations + [True]
        with patch.object(process_tg_outbox.threading, "Event", return_value=stop_event):
            call_command("process_tg_outbox", loop=True, stdout=self.output, **optionToValue)
        return stop_event

    def test_one_shot_preserves_limit_output_and_heartbeat(self):
        self.process.return_value = self.counts(processed=3, sent=2, failed=1, pending=4)
        call_command("process_tg_outbox", limit=7, stdout=self.output)
        self.process.assert_called_once_with(limit=7)
        self.heartbeat.assert_called_once_with("outbox", min_interval_seconds=0)
        self.assertIn("Outbox: processed=3 sent=2 failed=1 pending=4", self.output.getvalue())
        self.register_signal.assert_not_called()
        self.close_connections.assert_not_called()

    def test_one_shot_still_propagates_errors(self):
        self.process.side_effect = RuntimeError("temporary database failure")
        with self.assertRaises(RuntimeError):
            call_command("process_tg_outbox", stdout=self.output)
        self.heartbeat.assert_not_called()

    def test_idle_loop_polls_every_two_seconds_without_log_spam(self):
        stop_event = self.run_loop()
        self.assertEqual(self.process.call_args_list, [call(limit=50), call(limit=50)])
        self.assertEqual(stop_event.wait.call_args_list, [call(2.0), call(2.0)])
        self.assertEqual(self.close_connections.call_count, 4)
        self.assertEqual(self.heartbeat.call_args_list, [
            call("outbox", min_interval_seconds=30),
            call("outbox", min_interval_seconds=30),
        ])
        self.assertEqual(self.output.getvalue(), "")

    def test_loop_keeps_batches_bounded_and_reports_delivery(self):
        self.process.return_value = self.counts(processed=2, sent=2, pending=2)
        stop_event = self.run_loop(iterations=1, limit=11, poll_interval=0.5)
        self.process.assert_called_once_with(limit=11)
        stop_event.wait.assert_called_once_with(0.5)
        self.assertIn("processed=2 sent=2 failed=0 pending=2", self.output.getvalue())

    def test_loop_retries_batch_exception_and_closes_connections(self):
        self.process.side_effect = [RuntimeError("temporary failure"), self.counts()]
        stop_event = self.run_loop()
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(self.close_connections.call_count, 4)
        self.assertEqual(stop_event.wait.call_count, 2)
        self.logger.exception.assert_called_once_with("Outbox worker batch failed; will retry")
        self.heartbeat.assert_called_once_with("outbox", min_interval_seconds=30)

    def test_loop_survives_heartbeat_failure(self):
        self.heartbeat.side_effect = [RuntimeError("metrics unavailable"), None]
        self.run_loop()
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(self.heartbeat.call_count, 2)
        self.logger.exception.assert_called_once()

    def test_loop_survives_connection_cleanup_failure(self):
        self.close_connections.side_effect = [None, RuntimeError("connection failure"), None, None]
        self.run_loop()
        self.assertEqual(self.process.call_count, 2)
        self.logger.exception.assert_called_once_with("Could not close stale outbox database connections")

    def test_loop_retries_initial_connection_failure(self):
        self.close_connections.side_effect = [RuntimeError("connection failure"), None, None, None]
        self.run_loop()
        self.process.assert_called_once_with(limit=50)
        self.logger.exception.assert_called_once_with("Outbox worker batch failed; will retry")

    def test_loop_prevents_busy_wait(self):
        stop_event = self.run_loop(iterations=1, poll_interval=0)
        stop_event.wait.assert_called_once_with(0.1)

    def test_shutdown_finishes_current_batch_and_restores_signal_handlers(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                self.process.reset_mock()
                self.register_signal.reset_mock()

                def finish_batch(*, limit):
                    self.signalToHandler[signum](signum, None)
                    return self.counts(processed=1, sent=1)

                self.process.side_effect = finish_batch
                call_command("process_tg_outbox", loop=True, stdout=self.output)
                self.process.assert_called_once_with(limit=50)
                self.assertEqual(self.register_signal.call_args_list[-2:], [
                    call(signal.SIGTERM, self.signalToPreviousHandler[signal.SIGTERM]),
                    call(signal.SIGINT, self.signalToPreviousHandler[signal.SIGINT]),
                ])

    def test_unexpected_interrupt_also_restores_signal_handlers(self):
        self.process.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_loop(iterations=1)
        self.assertEqual(self.close_connections.call_count, 2)
        self.assertEqual(self.register_signal.call_args_list[-2:], [
            call(signal.SIGTERM, self.signalToPreviousHandler[signal.SIGTERM]),
            call(signal.SIGINT, self.signalToPreviousHandler[signal.SIGINT]),
        ])
