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


if __name__ == "__main__":
    unittest.main()
