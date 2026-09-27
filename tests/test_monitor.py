import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapter import HerdrClientInterface  # noqa: E402
from src.monitor import MachineConnectionMonitor  # noqa: E402
from src.test_runner import MachineTestRunner  # noqa: E402
from src.types import (  # noqa: E402
    HerdrMachineProfile,
    MachineConfig,
    MachineRuntimeState,
)


class FakeHerdrAdapter(HerdrClientInterface):
    """In-memory fake Herdr adapter for unit testing."""

    def __init__(self, profiles: Optional[List[HerdrMachineProfile]] = None):
        self.profiles = profiles or []
        self.disabled_calls: List[str] = []
        self.enabled_calls: List[str] = []
        self.probe_calls: List[str] = []
        self.should_raise_on_disable: bool = False
        self.should_raise_on_probe: bool = False

    def list_machines(self) -> List[HerdrMachineProfile]:
        return list(self.profiles)

    def disable_machine(self, profile_id: str) -> bool:
        if self.should_raise_on_disable:
            raise RuntimeError("Herdr API communication error")
        self.disabled_calls.append(profile_id)
        for p in self.profiles:
            if p.id == profile_id:
                p.enabled = False
        return True

    def enable_machine(self, profile_id: str) -> bool:
        self.enabled_calls.append(profile_id)
        for p in self.profiles:
            if p.id == profile_id:
                p.enabled = True
        return True

    def probe_machine(self, target: str, timeout_seconds: float = 5.0) -> bool:
        if self.should_raise_on_probe:
            raise TimeoutError("Probe timed out")
        self.probe_calls.append(target)
        return True


class TestMachineConnectionMonitor(unittest.TestCase):
    def setUp(self):
        self.mock_adapter = FakeHerdrAdapter(
            profiles=[
                HerdrMachineProfile(
                    id="8724cfc1531e3d6542673387623b8a52",
                    label="server1",
                    target="server1.mydomain.com",
                    session="default",
                    enabled=True,
                ),
                HerdrMachineProfile(
                    id="981b368cf2547b1c1e1141e2d039dc94",
                    label="server2",
                    target="server2.mydomain.com",
                    session="default",
                    enabled=True,
                ),
            ]
        )
        self.sleep_calls: List[float] = []

        def mock_sleep(seconds: float):
            self.sleep_calls.append(seconds)

        self.mock_sleep = mock_sleep

    def test_failing_machine_retried_exactly_three_times_and_disabled(self):
        """
        Verify that a failing machine is retried exactly 3 times (no more, no fewer)
        before its state is set to disabled in Herdr.
        """
        test_runner = MagicMock(spec=MachineTestRunner)
        # All checks fail: initial check + 3 retries = 4 check calls total
        test_runner.check_availability.return_value = (False, "Connection refused")

        machine = MachineConfig(
            name="server1",
            max_retries=3,
            retry_delay=2.0,
            auto_disable=True,
        )

        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()

        result = monitor.check_machine(machine)

        # Should report as offline / failed
        self.assertFalse(result)

        # 1 initial check + exactly 3 retries = 4 checks total
        self.assertEqual(test_runner.check_availability.call_count, 4)

        # Exactly 3 sleeps between retries
        self.assertEqual(len(self.sleep_calls), 3)
        self.assertEqual(self.sleep_calls, [2.0, 2.0, 2.0])

        # State should be disabled in Herdr exactly once with the 32-hex ID
        self.assertEqual(self.mock_adapter.disabled_calls, ["8724cfc1531e3d6542673387623b8a52"])
        st = monitor.state["server1"]
        self.assertFalse(st.herdr_enabled)
        self.assertIsNotNone(st.disabled_ts)

    def test_disabled_called_exactly_once_after_exhaustion(self):
        """
        Verify that disable_machine is called exactly once after the 3rd retry,
        even if check_machine is called again when already disabled.
        """
        test_runner = MagicMock(spec=MachineTestRunner)
        test_runner.check_availability.return_value = (False, "Host unreachable")

        machine = MachineConfig(
            name="server1",
            max_retries=3,
            retry_delay=1.0,
            auto_disable=True,
        )

        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()

        monitor.check_machine(machine)
        self.assertEqual(len(self.mock_adapter.disabled_calls), 1)

        # Calling again when already marked disabled should not call disable again
        monitor.check_machine(machine)
        self.assertEqual(len(self.mock_adapter.disabled_calls), 1)

    def test_recovery_on_retry_resets_counter_and_machine_remains_enabled(self):
        """
        Verify that if a machine recovers on retry 1, 2, or 3:
        - It is NOT disabled
        - Its retry counter resets to 0
        - It remains enabled
        """
        for recovery_attempt in [1, 2, 3]:
            with self.subTest(recovery_attempt=recovery_attempt):
                self.setUp()
                test_runner = MagicMock(spec=MachineTestRunner)

                # Sequence: Initial fails, then fails up to recovery_attempt, where it succeeds
                side_effects = [False] * recovery_attempt + [True]
                test_runner.check_availability.side_effect = [
                    (ok, None if ok else "Temporary glitch") for ok in side_effects
                ]

                machine = MachineConfig(
                    name="server1",
                    max_retries=3,
                    retry_delay=1.0,
                    auto_disable=True,
                )

                monitor = MachineConnectionMonitor(
                    herdr_adapter=self.mock_adapter,
                    test_runner=test_runner,
                    machines=[machine],
                    sleep_fn=self.mock_sleep,
                )
                monitor.sync_machines()

                result = monitor.check_machine(machine)

                self.assertTrue(result)
                # Never disabled!
                self.assertEqual(len(self.mock_adapter.disabled_calls), 0)
                # Counter must be reset to 0
                st = monitor.state["server1"]
                self.assertEqual(st.retry_count, 0)
                self.assertTrue(st.is_online)
                self.assertTrue(st.herdr_enabled)

    def test_independent_state_tracking_across_machines(self):
        """
        Verify that failures on one machine do NOT affect the state or retry counters
        of any other machine.
        """
        test_runner = MagicMock(spec=MachineTestRunner)

        def check_by_name(m: MachineConfig):
            if m.name == "server1":
                return False, "Server 1 is down"
            return True, None

        test_runner.check_availability.side_effect = check_by_name

        m1 = MachineConfig(name="server1", max_retries=3, retry_delay=0.1)
        m2 = MachineConfig(name="server2", max_retries=3, retry_delay=0.1)

        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[m1, m2],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()

        results = monitor.check_all_once()

        self.assertFalse(results["server1"])
        self.assertTrue(results["server2"])

        # Server 1 was disabled
        self.assertEqual(self.mock_adapter.disabled_calls, ["8724cfc1531e3d6542673387623b8a52"])

        # Server 2 has 0 retries and remains enabled
        self.assertEqual(monitor.state["server2"].retry_count, 0)
        self.assertTrue(monitor.state["server2"].is_online)
        self.assertTrue(monitor.state["server2"].herdr_enabled)

    def test_api_errors_handled_without_crashing(self):
        """
        Verify that API errors, timeouts, or unexpected exceptions during checks
        or disabling are caught, logged, and do not crash the monitor.
        """
        test_runner = MagicMock(spec=MachineTestRunner)
        # Check raises an unexpected exception
        test_runner.check_availability.side_effect = ConnectionResetError("Connection abruptly closed")

        machine = MachineConfig(name="server1", max_retries=3, retry_delay=0.1)

        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()

        # check_all_once must catch the error and not raise
        results = monitor.check_all_once()
        self.assertIn("server1", results)
        self.assertFalse(results["server1"])

        # Even if Herdr adapter raises on disable, it must not crash
        self.mock_adapter.should_raise_on_disable = True
        test_runner.check_availability.side_effect = None
        test_runner.check_availability.return_value = (False, "Offline")

        # Must run cleanly without throwing
        res = monitor.check_machine(machine)
        self.assertFalse(res)

    def test_auto_enable_behavior(self):
        """
        Verify that if auto_enable is True and a previously disabled machine is back online,
        it calls enable_machine.
        """
        test_runner = MagicMock(spec=MachineTestRunner)
        test_runner.check_availability.return_value = (True, None)

        # server1 starts in disabled state
        self.mock_adapter.profiles[0].enabled = False

        machine = MachineConfig(
            name="server1",
            auto_enable=True,
        )

        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()

        result = monitor.check_machine(machine)
        self.assertTrue(result)
        self.assertEqual(self.mock_adapter.enabled_calls, ["8724cfc1531e3d6542673387623b8a52"])
        self.assertTrue(monitor.state["server1"].herdr_enabled)


    def test_state_file_is_owner_only(self):
        """state.json holds machine details and probe stderr; it must not be
        world-readable, even when the directory pre-exists with loose perms."""
        state_dir = tempfile.mkdtemp(prefix="herdr-mon-test-")
        os.chmod(state_dir, 0o755)
        os.environ["HERDR_PLUGIN_STATE_DIR"] = state_dir
        try:
            monitor = MachineConnectionMonitor(
                herdr_adapter=self.mock_adapter,
                machines=[],
                sleep_fn=self.mock_sleep,
            )
            monitor.state["m"] = MachineRuntimeState(name="m")
            monitor.save_state()
            self.assertTrue(monitor.state_file.exists())
            mode = stat.S_IMODE(os.stat(monitor.state_file).st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)

    def test_error_messages_are_sanitized_in_state(self):
        """Probe stderr is untrusted; it must be flattened to one line so it
        cannot forge log entries or corrupt the state file."""
        test_runner = MagicMock(spec=MachineTestRunner)
        test_runner.check_availability.return_value = (False, "line1\nFORGED LOG LINE\r\nline3")

        machine = MachineConfig(name="server1", max_retries=1, retry_delay=0.0)
        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()
        monitor.check_machine(machine)

        msg = monitor.state["server1"].last_error_message or ""
        self.assertNotIn("\n", msg)
        self.assertNotIn("\r", msg)

    def test_stale_state_entries_are_pruned_on_sync(self):
        """Removed machines must not keep stale state (and stale profile IDs)
        alive in state.json forever."""
        machine = MachineConfig(name="server1")
        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.state["ghost"] = MachineRuntimeState(name="ghost", herdr_profile_id="deadbeef")
        monitor.sync_machines()
        self.assertNotIn("ghost", monitor.state)

    def test_disable_refuses_malformed_profile_id(self):
        """A poisoned/corrupted profile id must never reach the state-changing
        Herdr CLI invocation."""
        test_runner = MagicMock(spec=MachineTestRunner)
        test_runner.check_availability.return_value = (False, "down")

        machine = MachineConfig(name="server1", max_retries=1, retry_delay=0.0, auto_disable=True)
        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()
        monitor.state["server1"].herdr_profile_id = "../../evil; rm -rf /"

        result = monitor.check_machine(machine)
        self.assertFalse(result)
        self.assertEqual(self.mock_adapter.disabled_calls, [])

    def test_disable_skipped_when_live_state_confirms_disabled(self):
        """If Herdr reports the profile is already disabled, the cached state
        must not trigger a redundant disable call."""
        test_runner = MagicMock(spec=MachineTestRunner)
        test_runner.check_availability.return_value = (False, "down")

        machine = MachineConfig(name="server1", max_retries=1, retry_delay=0.0, auto_disable=True)
        monitor = MachineConnectionMonitor(
            herdr_adapter=self.mock_adapter,
            test_runner=test_runner,
            machines=[machine],
            sleep_fn=self.mock_sleep,
        )
        monitor.sync_machines()

        # Simulate the profile having been disabled out-of-band
        self.mock_adapter.profiles[0].enabled = False

        result = monitor.check_machine(machine)
        self.assertFalse(result)
        self.assertEqual(self.mock_adapter.disabled_calls, [])
        self.assertFalse(monitor.state["server1"].herdr_enabled)


if __name__ == "__main__":
    unittest.main()
