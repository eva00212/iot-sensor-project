"""
test_update_site_id.py

Unit tests for tools/update_site_id.py's surgical site_id replacement --
verifies it changes only the site_id value and preserves every other
line (comments, formatting, other settings) byte-for-byte. Everything
runs against temp files; no real system state is touched.

Run with:
  python -m unittest discover raspberry_pi/tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import update_site_id

SAMPLE_CONFIG = '''# This Pi's test bed identity (format: testBed01..testBed08). Every payload
# this collector builds is stamped with this site_id.
site_id: "testBed01"

server:
  host: "mobius.asquare.re.kr"   # hostname, not an IP
  port: 1883
  keepalive: 60
  client_id: "rpi-uploader"
  qos: 1

  flush:
    batch_size: 20
    pacing_seconds: 1
    max_buffered_messages: 10000
'''


class TestUpdateSiteId(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "site_config.yaml"
        self.path.write_text(SAMPLE_CONFIG, encoding="utf-8")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_updates_site_id_value(self):
        update_site_id.update_site_id(str(self.path), "testBed05")
        content = self.path.read_text(encoding="utf-8")
        self.assertIn('site_id: "testBed05"', content)
        self.assertNotIn('site_id: "testBed01"', content)

    def test_preserves_every_other_line_exactly(self):
        before_lines = SAMPLE_CONFIG.splitlines()
        update_site_id.update_site_id(str(self.path), "testBed05")
        after_lines = self.path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(before_lines), len(after_lines))
        for i, (before, after) in enumerate(zip(before_lines, after_lines)):
            if "site_id:" in before:
                continue  # the one line that's supposed to change
            self.assertEqual(before, after, f"line {i} changed unexpectedly")

    def test_preserves_comments(self):
        update_site_id.update_site_id(str(self.path), "testBed05")
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("# This Pi's test bed identity", content)
        self.assertIn("# hostname, not an IP", content)

    def test_result_is_valid_yaml_with_new_site_id(self):
        update_site_id.update_site_id(str(self.path), "testBed05")
        with open(self.path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.assertEqual(cfg["site_id"], "testBed05")
        self.assertEqual(cfg["server"]["host"], "mobius.asquare.re.kr")  # untouched
        self.assertEqual(cfg["server"]["flush"]["max_buffered_messages"], 10000)  # untouched

    def test_repeated_updates_are_idempotent_in_shape(self):
        # Running it twice (e.g. correcting a typo'd site_id) must not
        # accumulate extra lines or duplicate the key.
        update_site_id.update_site_id(str(self.path), "testBed05")
        update_site_id.update_site_id(str(self.path), "testBed06")
        content = self.path.read_text(encoding="utf-8")
        self.assertEqual(content.count("site_id:"), 1)
        self.assertIn('site_id: "testBed06"', content)

    def test_raises_when_no_site_id_line_present(self):
        self.path.write_text("server:\n  host: example.com\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            update_site_id.update_site_id(str(self.path), "testBed05")

    def test_raises_on_missing_file(self):
        with self.assertRaises(OSError):
            update_site_id.update_site_id(str(self.path) + "-does-not-exist", "testBed05")


if __name__ == "__main__":
    unittest.main()
