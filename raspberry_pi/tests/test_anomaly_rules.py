"""
test_anomaly_rules.py

Unit tests for anomaly_rules.py's rule checks, focused on the
production-hardening changes: RAPID_CHANGE (renamed from SUDDEN_CHANGE),
STUCK_SENSOR, and the missing-data timer being gated on the last
*successful* reading rather than the last polling attempt.

Uses the real rule_config.yaml/modbus_config.yaml (same pattern as
test_modbus_poller.py using the real modbus_config.yaml) but patches
module-level constants directly where a test needs a specific threshold,
so it doesn't depend on -- or need to wait out -- the real production
timeouts (1200s missing-data, etc.).

Run with:
  python -m unittest discover raspberry_pi/tests
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import anomaly_rules


def _indoor_payload(site_id="testBed01", device_id="device01", temperature=25.0,
                     humidity=50.0, co2=500, device_fault="false"):
    payload = {
        "site_id": site_id, "device_id": device_id,
        "timestamp": "2026-01-01T00:00:00", "device_fault": device_fault,
    }
    if device_fault != "true":
        payload.update(temperature=temperature, humidity=humidity, co2=co2)
    return payload


class AnomalyRulesTestCase(unittest.TestCase):
    """Clears all module-level state before each test -- anomaly_rules
    keeps state in module dicts keyed by (site_id, device_id), which would
    otherwise leak between tests."""

    def setUp(self):
        anomaly_rules._last_values.clear()
        anomaly_rules._last_seen.clear()
        anomaly_rules._indoor_latest.clear()
        anomaly_rules._stuck_state.clear()
        anomaly_rules._known_devices.clear()


class TestDeviceFault(AnomalyRulesTestCase):
    def test_flags_when_device_fault_true(self):
        result = anomaly_rules.check(_indoor_payload(device_fault="true"))
        self.assertIn("DEVICE_FAULT", result["rule_flags"])
        self.assertEqual(result["rule_status"], "anomaly")

    def test_no_flag_when_device_fault_false(self):
        result = anomaly_rules.check(_indoor_payload())
        self.assertNotIn("DEVICE_FAULT", result["rule_flags"])


class TestOutOfRange(AnomalyRulesTestCase):
    def test_flags_temperature_above_max(self):
        result = anomaly_rules.check(_indoor_payload(temperature=999.0))
        self.assertIn("TEMPERATURE_OUT_OF_RANGE", result["rule_flags"])

    def test_no_flag_within_range(self):
        result = anomaly_rules.check(_indoor_payload(temperature=25.0))
        self.assertNotIn("TEMPERATURE_OUT_OF_RANGE", result["rule_flags"])


class TestRapidChange(AnomalyRulesTestCase):
    def test_flags_large_jump_from_previous_valid_sample(self):
        anomaly_rules.check(_indoor_payload(temperature=25.0))
        result = anomaly_rules.check(_indoor_payload(temperature=90.0))  # way past the 5.0 threshold
        self.assertIn("TEMPERATURE_RAPID_CHANGE", result["rule_flags"])

    def test_no_flag_for_small_change(self):
        anomaly_rules.check(_indoor_payload(temperature=25.0))
        result = anomaly_rules.check(_indoor_payload(temperature=25.5))
        self.assertNotIn("TEMPERATURE_RAPID_CHANGE", result["rule_flags"])

    def test_no_flag_on_first_ever_reading(self):
        # No previous sample to compare against yet.
        result = anomaly_rules.check(_indoor_payload(temperature=999.0 - 990))
        self.assertNotIn("TEMPERATURE_RAPID_CHANGE", result["rule_flags"])

    def test_faulted_reading_does_not_poison_the_comparison(self):
        # A faulted reading has no temperature field at all (see
        # modbus_poller's no-fabricated-values behavior) -- the next
        # valid reading must not be compared against a missing value.
        anomaly_rules.check(_indoor_payload(temperature=25.0))
        anomaly_rules.check(_indoor_payload(device_fault="true"))
        result = anomaly_rules.check(_indoor_payload(temperature=25.5))
        self.assertNotIn("TEMPERATURE_RAPID_CHANGE", result["rule_flags"])


class TestStuckSensor(AnomalyRulesTestCase):
    def test_flags_after_consecutive_count_identical_readings(self):
        with patch.object(anomaly_rules, "STUCK_COUNT", 3):
            anomaly_rules.check(_indoor_payload(temperature=25.0))
            anomaly_rules.check(_indoor_payload(temperature=25.0))
            result = anomaly_rules.check(_indoor_payload(temperature=25.0))

        self.assertIn("TEMPERATURE_STUCK_SENSOR", result["rule_flags"])

    def test_no_flag_below_consecutive_count(self):
        with patch.object(anomaly_rules, "STUCK_COUNT", 3):
            anomaly_rules.check(_indoor_payload(temperature=25.0))
            result = anomaly_rules.check(_indoor_payload(temperature=25.0))

        self.assertNotIn("TEMPERATURE_STUCK_SENSOR", result["rule_flags"])

    def test_streak_resets_when_value_changes(self):
        with patch.object(anomaly_rules, "STUCK_COUNT", 3):
            anomaly_rules.check(_indoor_payload(temperature=25.0))
            anomaly_rules.check(_indoor_payload(temperature=25.0))
            anomaly_rules.check(_indoor_payload(temperature=26.0))  # streak resets here
            result = anomaly_rules.check(_indoor_payload(temperature=26.0))

        self.assertNotIn("TEMPERATURE_STUCK_SENSOR", result["rule_flags"])

    def test_missing_reading_does_not_count_as_a_repeat(self):
        with patch.object(anomaly_rules, "STUCK_COUNT", 3):
            anomaly_rules.check(_indoor_payload(temperature=25.0))
            anomaly_rules.check(_indoor_payload(device_fault="true"))  # no temperature field
            result = anomaly_rules.check(_indoor_payload(temperature=25.0))

        # Only 2 real identical readings have actually been seen (the
        # faulted cycle in between must not advance the streak).
        self.assertNotIn("TEMPERATURE_STUCK_SENSOR", result["rule_flags"])

    def test_rainfall_is_not_checked_for_device03(self):
        # rainfall=0 for many consecutive polls is normal (dry weather),
        # not a fault -- must never be flagged as stuck.
        outdoor = {
            "site_id": "testBed01", "device_id": "device03",
            "timestamp": "2026-01-01T00:00:00", "device_fault": "false",
            "temperature": 20.0, "humidity": 50.0, "rainfall": 0.0,
        }
        with patch.object(anomaly_rules, "STUCK_COUNT", 2):
            anomaly_rules.check(outdoor)
            result = anomaly_rules.check(outdoor)

        self.assertNotIn("RAINFALL_STUCK_SENSOR", result["rule_flags"])


class TestMissingDataTimerUsesLastSuccess(AnomalyRulesTestCase):
    """The missing-data timer must be based on the last *successful*
    reading, not merely the last polling attempt -- a continuously
    faulting device (device_fault="true" every cycle, never actually
    communicating) must still eventually be flagged as missing valid
    data, not treated as "present" just because it's transmitting."""

    def test_continuously_faulting_device_is_eventually_flagged_missing(self):
        with patch.object(anomaly_rules, "MISSING_TIMEOUT", 0):
            # Every cycle fails -- device_fault is always "true".
            anomaly_rules.check(_indoor_payload(device_fault="true"))
            missing = anomaly_rules.check_missing_data()

        self.assertIn(("testBed01", "device01"), missing)

    def test_device_that_has_never_once_succeeded_has_no_last_seen_entry(self):
        # There's no "last successful reading" timestamp to record --
        # but it must still be checked/flaggable (see the test above),
        # via _known_devices rather than _last_seen.
        anomaly_rules.check(_indoor_payload(device_fault="true"))
        self.assertNotIn(("testBed01", "device01"), anomaly_rules._last_seen)
        self.assertIn(("testBed01", "device01"), anomaly_rules._known_devices)

    def test_recent_successful_reading_is_not_flagged_missing(self):
        with patch.object(anomaly_rules, "MISSING_TIMEOUT", 3600):
            anomaly_rules.check(_indoor_payload(device_fault="false"))
            missing = anomaly_rules.check_missing_data()

        self.assertNotIn(("testBed01", "device01"), missing)

    def test_stale_successful_reading_is_flagged_missing(self):
        with patch.object(anomaly_rules, "MISSING_TIMEOUT", 3600):
            anomaly_rules.check(_indoor_payload(device_fault="false"))
        # Simulate time passing well beyond the timeout since that last
        # successful reading, without waiting for it in real time.
        anomaly_rules._last_seen[("testBed01", "device01")] -= 7200

        with patch.object(anomaly_rules, "MISSING_TIMEOUT", 3600):
            missing = anomaly_rules.check_missing_data()

        self.assertIn(("testBed01", "device01"), missing)

    def test_recovering_after_a_fault_clears_the_missing_flag(self):
        with patch.object(anomaly_rules, "MISSING_TIMEOUT", 3600):
            anomaly_rules.check(_indoor_payload(device_fault="true"))
            self.assertIn(("testBed01", "device01"), anomaly_rules.check_missing_data())

            anomaly_rules.check(_indoor_payload(device_fault="false"))  # communication recovers
            self.assertNotIn(("testBed01", "device01"), anomaly_rules.check_missing_data())


if __name__ == "__main__":
    unittest.main()
