import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapter import HerdrClientInterface  # noqa: E402
from src.test_runner import MachineTestRunner  # noqa: E402
from src.types import MachineConfig, ServerInfo, TestConfig  # noqa: E402


class TestMachineTestRunner(unittest.TestCase):
    def test_shell_test_success_and_variable_substitution(self):
        runner = MachineTestRunner()
        machine = MachineConfig(
            name="srv1",
            server=ServerInfo(ip="127.0.0.1", hostname="localhost", port=8080),
            test=TestConfig(
                type="shell",
                online='[[ "{ip}" = "127.0.0.1" && "{port}" = "8080" ]]',
                timeout_seconds=2.0,
            ),
        )
        ok, err = runner.check_availability(machine)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_shell_test_failure_exit_code(self):
        runner = MachineTestRunner()
        machine = MachineConfig(
            name="srv1",
            server=ServerInfo(ip="127.0.0.1"),
            test=TestConfig(
                type="shell",
                online="exit 1",
                timeout_seconds=2.0,
            ),
        )
        ok, err = runner.check_availability(machine)
        self.assertFalse(ok)
        self.assertIn("failed with exit code 1", err or "")

    def test_shell_test_timeout(self):
        runner = MachineTestRunner()
        machine = MachineConfig(
            name="srv1",
            server=ServerInfo(ip="127.0.0.1"),
            test=TestConfig(
                type="shell",
                online="sleep 3",
                timeout_seconds=0.2,
            ),
        )
        ok, err = runner.check_availability(machine)
        self.assertFalse(ok)
        self.assertIn("timed out", err or "")

    def test_herdr_probe_delegation(self):
        mock_adapter = MagicMock(spec=HerdrClientInterface)
        mock_adapter.probe_machine.return_value = True

        runner = MachineTestRunner(herdr_adapter=mock_adapter)
        machine = MachineConfig(
            name="server4",
            test=TestConfig(type="herdr", timeout_seconds=4.0),
        )
        ok, err = runner.check_availability(machine)
        self.assertTrue(ok)
        mock_adapter.probe_machine.assert_called_once_with("server4", timeout_seconds=4.0)


if __name__ == "__main__":
    unittest.main()
