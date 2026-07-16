"""
test_server_uploader.py

Unit tests for server_uploader.py's connection-handling and buffer logic,
using a mocked MQTT client — no real broker or network required.

Run with:
  python -m unittest discover raspberry_pi/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import server_uploader


def _make_msg_info(published: bool):
    info = MagicMock()
    info.wait_for_publish.return_value = None
    info.is_published.return_value = published
    return info


class BufferTestCase(unittest.TestCase):
    """Base class: points BUFFER_PATH at a temp file for the duration of the test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_buffer_path = server_uploader.BUFFER_PATH
        server_uploader.BUFFER_PATH = Path(self._tmpdir.name) / "buffer.jsonl"

    def tearDown(self):
        server_uploader.BUFFER_PATH = self._orig_buffer_path
        self._tmpdir.cleanup()

    def _buffer_lines(self):
        if not server_uploader.BUFFER_PATH.exists():
            return []
        return server_uploader.BUFFER_PATH.read_text(encoding="utf-8").splitlines()


class TestUpload(BufferTestCase):
    def test_buffers_when_disconnected(self):
        with patch.object(server_uploader._client, "is_connected", return_value=False):
            result = server_uploader.upload({"topic": "t", "body": "b"})

        self.assertFalse(result)
        self.assertEqual(len(self._buffer_lines()), 1)
        self.assertEqual(json.loads(self._buffer_lines()[0]), {"topic": "t", "body": "b"})

    def test_succeeds_when_connected_and_confirmed(self):
        with patch.object(server_uploader._client, "is_connected", return_value=True), \
             patch.object(server_uploader._client, "publish", return_value=_make_msg_info(True)):
            result = server_uploader.upload({"topic": "t", "body": "b"})

        self.assertTrue(result)
        self.assertEqual(self._buffer_lines(), [])

    def test_buffers_when_publish_not_confirmed(self):
        with patch.object(server_uploader._client, "is_connected", return_value=True), \
             patch.object(server_uploader._client, "publish", return_value=_make_msg_info(False)):
            result = server_uploader.upload({"topic": "t", "body": "b"})

        self.assertFalse(result)
        self.assertEqual(len(self._buffer_lines()), 1)

    def test_buffers_on_publish_exception(self):
        with patch.object(server_uploader._client, "is_connected", return_value=True), \
             patch.object(server_uploader._client, "publish", side_effect=RuntimeError("boom")):
            result = server_uploader.upload({"topic": "t", "body": "b"})

        self.assertFalse(result)
        self.assertEqual(len(self._buffer_lines()), 1)


class TestBufferIsBounded(BufferTestCase):
    """A sustained outage must not let buffer.jsonl grow without limit --
    MAX_BUFFERED_MESSAGES caps it, dropping the oldest queued payload(s)
    to make room for the newest."""

    def test_oldest_entries_dropped_once_cap_exceeded(self):
        with patch.object(server_uploader, "MAX_BUFFERED_MESSAGES", 3), \
             patch.object(server_uploader._client, "is_connected", return_value=False):
            for i in range(5):
                server_uploader.upload({"topic": "t", "body": str(i)})

        remaining = [json.loads(l)["body"] for l in self._buffer_lines()]
        # Oldest (0, 1) dropped; newest 3 (bounded) kept, in order.
        self.assertEqual(remaining, ["2", "3", "4"])

    def test_stays_under_cap_when_never_exceeded(self):
        with patch.object(server_uploader, "MAX_BUFFERED_MESSAGES", 100), \
             patch.object(server_uploader._client, "is_connected", return_value=False):
            for i in range(3):
                server_uploader.upload({"topic": "t", "body": str(i)})

        remaining = [json.loads(l)["body"] for l in self._buffer_lines()]
        self.assertEqual(remaining, ["0", "1", "2"])


class TestFlushBuffer(BufferTestCase):
    def _write_lines(self, *payloads):
        text = "\n".join(json.dumps(p) for p in payloads) + "\n"
        server_uploader.BUFFER_PATH.write_text(text, encoding="utf-8")

    def test_removes_only_the_sent_line_no_duplicates(self):
        a = {"topic": "a", "body": "1"}
        b = {"topic": "b", "body": "2"}
        self._write_lines(a, b)

        # a succeeds, b fails -> flush stops after a; b stays queued exactly once
        with patch.object(server_uploader, "_try_upload", side_effect=[True, False]) as mock_upload, \
             patch.object(server_uploader.time, "sleep"):
            server_uploader.flush_buffer()

        self.assertEqual(mock_upload.call_count, 2)
        remaining = [json.loads(l) for l in self._buffer_lines()]
        self.assertEqual(remaining, [b])

    def test_preserves_line_appended_concurrently_during_flush(self):
        a = {"topic": "a", "body": "1"}
        c = {"topic": "c", "body": "3"}
        self._write_lines(a)

        def fake_try_upload(converted):
            # Simulate a live upload failure appending a new line while this
            # flush pass is still running.
            server_uploader._write_to_buffer(c)
            return True

        with patch.object(server_uploader, "_try_upload", side_effect=fake_try_upload), \
             patch.object(server_uploader.time, "sleep"):
            server_uploader.flush_buffer()

        remaining = [json.loads(l) for l in self._buffer_lines()]
        self.assertEqual(remaining, [c])  # a removed (sent), c preserved (not clobbered)

    def test_skips_when_flush_already_in_progress(self):
        self._write_lines({"topic": "a", "body": "1"})

        server_uploader._flush_lock.acquire()
        try:
            with patch.object(server_uploader, "_try_upload") as mock_upload:
                server_uploader.flush_buffer()
            mock_upload.assert_not_called()
        finally:
            server_uploader._flush_lock.release()

    def test_noop_on_empty_or_missing_buffer(self):
        # No file at all
        with patch.object(server_uploader, "_try_upload") as mock_upload:
            server_uploader.flush_buffer()
        mock_upload.assert_not_called()


class TestReconnectBackoffConfigured(unittest.TestCase):
    def test_client_configured_with_configured_backoff_bounds(self):
        # reconnect_delay_set() runs at import time; assert the resulting
        # internal state matches the values read from site_config.yaml.
        self.assertEqual(server_uploader._client._reconnect_min_delay, server_uploader.RECONNECT_MIN_DELAY)
        self.assertEqual(server_uploader._client._reconnect_max_delay, server_uploader.RECONNECT_MAX_DELAY)


class TestOnConnectTriggersFlush(unittest.TestCase):
    def test_on_connect_success_schedules_a_flush(self):
        with patch.object(server_uploader.threading, "Thread") as mock_thread:
            server_uploader._on_connect(server_uploader._client, None, {}, 0, None)

        mock_thread.assert_called_once()
        _, kwargs = mock_thread.call_args
        self.assertEqual(kwargs.get("target"), server_uploader.flush_buffer)

    def test_on_connect_failure_does_not_schedule_a_flush(self):
        with patch.object(server_uploader.threading, "Thread") as mock_thread:
            server_uploader._on_connect(server_uploader._client, None, {}, 1, None)

        mock_thread.assert_not_called()


class TestClientIdIsPerDevice(unittest.TestCase):
    def test_client_id_suffixed_with_site_id(self):
        self.assertTrue(server_uploader.CLIENT_ID.endswith(f"-{server_uploader._cfg.get('site_id')}"))


if __name__ == "__main__":
    unittest.main()
