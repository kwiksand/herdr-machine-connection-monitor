import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ConfigLoader  # noqa: E402
from src.types import GlobalDefaults  # noqa: E402


class TestConfigLoader(unittest.TestCase):
    def test_parse_valid_machine_yaml(self):
        content = """
defaults:
  max_retries: 5
  retry_delay_seconds: 3.5
  poll_interval_seconds: 40.0
  probe_timeout_seconds: 8.0
  auto_disable: false
  auto_enable: true

machines:
  - name: test-server
    poll_interval: 15
    server:
      ip: 10.0.0.5
      hostname: test.example.com
      port: 2222
    test:
      online: '[[ "$(echo 1)" = "1" ]]'
      timeout_seconds: 4.0
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            defaults, machines = ConfigLoader.parse_machines_file(Path(temp_path), GlobalDefaults())
            self.assertEqual(defaults.max_retries, 5)
            self.assertEqual(defaults.retry_delay_seconds, 3.5)
            self.assertEqual(defaults.poll_interval_seconds, 40.0)
            self.assertEqual(defaults.probe_timeout_seconds, 8.0)
            self.assertFalse(defaults.auto_disable)
            self.assertTrue(defaults.auto_enable)

            self.assertEqual(len(machines), 1)
            m = machines[0]
            self.assertEqual(m.name, "test-server")
            self.assertEqual(m.poll_interval, 15.0)
            self.assertEqual(m.server.ip, "10.0.0.5")
            self.assertEqual(m.server.hostname, "test.example.com")
            self.assertEqual(m.server.port, 2222)
            self.assertEqual(m.test.online, '[[ "$(echo 1)" = "1" ]]')
            self.assertEqual(m.test.timeout_seconds, 4.0)
        finally:
            os.remove(temp_path)

    def test_env_var_overrides(self):
        os.environ["HERDR_MONITOR_MAX_RETRIES"] = "4"
        os.environ["HERDR_MONITOR_RETRY_DELAY"] = "7.5"
        try:
            defaults, _ = ConfigLoader.load_defaults_and_machines()
            self.assertEqual(defaults.max_retries, 4)
            self.assertEqual(defaults.retry_delay_seconds, 7.5)
        finally:
            os.environ.pop("HERDR_MONITOR_MAX_RETRIES", None)
            os.environ.pop("HERDR_MONITOR_RETRY_DELAY", None)


    def test_invalid_defaults_are_clamped(self):
        """Out-of-range numerics must be clamped, not trusted: a negative
        timeout would instantly mark machines offline and trigger a mass
        auto-disable cascade; a huge retry delay would hang the daemon."""
        content = """
defaults:
  max_retries: -5
  retry_delay_seconds: 99999
  poll_interval_seconds: 0
  probe_timeout_seconds: -1
  auto_disable: "false"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            defaults, _ = ConfigLoader.parse_machines_file(Path(temp_path), GlobalDefaults())
            self.assertEqual(defaults.max_retries, 0)
            self.assertEqual(defaults.retry_delay_seconds, 3600.0)
            self.assertEqual(defaults.poll_interval_seconds, 1.0)
            self.assertEqual(defaults.probe_timeout_seconds, 0.1)
            # A quoted "false" must not become truthy Python
            self.assertFalse(defaults.auto_disable)
        finally:
            os.remove(temp_path)

    def test_malformed_machine_entry_is_skipped_not_fatal(self):
        """A non-mapping entry must be skipped with a warning, not crash the
        whole config load and take all monitoring down with it."""
        content = """
machines:
  - just a string, not a mapping
  - name: good
    server:
      ip: 10.0.0.5
    test:
      type: tcp
  - name: bad-numeric
    poll_interval: not-a-number
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            _, machines = ConfigLoader.parse_machines_file(Path(temp_path), GlobalDefaults())
            # The non-mapping entry is skipped; the bad numeric value is
            # clamped/dropped but the machine itself survives with defaults.
            self.assertEqual(len(machines), 2)
            self.assertEqual([m.name for m in machines], ["good", "bad-numeric"])
            self.assertIsNone(machines[1].poll_interval)
        finally:
            os.remove(temp_path)

    def test_invalid_machine_values_are_clamped(self):
        content = """
machines:
  - name: bad-timeout
    server:
      ip: 10.0.0.5
      port: 99999
    test:
      type: tcp
      timeout_seconds: -2.0
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            _, machines = ConfigLoader.parse_machines_file(Path(temp_path), GlobalDefaults())
            self.assertEqual(len(machines), 1)
            m = machines[0]
            self.assertEqual(m.server.port, 65535)
            self.assertEqual(m.test.timeout_seconds, 0.1)
        finally:
            os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
