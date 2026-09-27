import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .adapter import HerdrClientInterface
from .test_runner import MachineTestRunner
from .types import (
    GlobalDefaults,
    HerdrMachineProfile,
    MachineConfig,
    MachineRuntimeState,
    ServerInfo,
    TestConfig,
)

logger = logging.getLogger("herdr.machine_monitor")


class MachineConnectionMonitor:
    """Core watchdog and retry state machine for Herdr machines."""

    def __init__(
        self,
        herdr_adapter: HerdrClientInterface,
        test_runner: Optional[MachineTestRunner] = None,
        defaults: Optional[GlobalDefaults] = None,
        machines: Optional[List[MachineConfig]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.time,
        state_file: Optional[Path] = None,
    ):
        self.herdr_adapter = herdr_adapter
        self.test_runner = test_runner or MachineTestRunner(herdr_adapter=herdr_adapter)
        self.defaults = defaults or GlobalDefaults()
        self.machines = machines or []
        self.sleep_fn = sleep_fn
        self.time_fn = time_fn
        self.state: Dict[str, MachineRuntimeState] = {}
        self.state_file = state_file or self._resolve_state_file()
        self.load_state()

    def _resolve_state_file(self) -> Optional[Path]:
        state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        if state_dir:
            p = Path(state_dir)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p / "state.json"
            except Exception:
                pass

        config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        if config_dir:
            p = Path(config_dir)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p / "state.json"
            except Exception:
                pass

        default_dir = Path.home() / ".local" / "state" / "herdr" / "plugins" / "machine-connection-monitor"
        try:
            default_dir.mkdir(parents=True, exist_ok=True)
            return default_dir / "state.json"
        except Exception:
            return None

    def load_state(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, s_dict in data.items():
                st = MachineRuntimeState(
                    name=s_dict.get("name", name),
                    retry_count=int(s_dict.get("retry_count", 0)),
                    is_online=s_dict.get("is_online"),
                    herdr_profile_id=s_dict.get("herdr_profile_id"),
                    herdr_enabled=s_dict.get("herdr_enabled"),
                    last_check_ts=s_dict.get("last_check_ts"),
                    last_success_ts=s_dict.get("last_success_ts"),
                    last_failure_ts=s_dict.get("last_failure_ts"),
                    last_error_message=s_dict.get("last_error_message"),
                    disabled_ts=s_dict.get("disabled_ts"),
                    next_check_ts=float(s_dict.get("next_check_ts", 0.0)),
                )
                self.state[name] = st
        except Exception as e:
            logger.warning("Failed to load state from %s: %s", self.state_file, e)

    def save_state(self) -> None:
        if not self.state_file:
            return
        try:
            data = {}
            for name, st in self.state.items():
                data[name] = {
                    "name": st.name,
                    "retry_count": st.retry_count,
                    "is_online": st.is_online,
                    "herdr_profile_id": st.herdr_profile_id,
                    "herdr_enabled": st.herdr_enabled,
                    "last_check_ts": st.last_check_ts,
                    "last_success_ts": st.last_success_ts,
                    "last_failure_ts": st.last_failure_ts,
                    "last_error_message": st.last_error_message,
                    "disabled_ts": st.disabled_ts,
                    "next_check_ts": st.next_check_ts,
                }
            tmp_path = self.state_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(self.state_file)
        except Exception as e:
            logger.warning("Failed to save state to %s: %s", self.state_file, e)

    def sync_machines(self) -> None:
        """
        Synchronize configured machines with live Herdr machine profiles.
        If no explicit machines were configured, discover all active Herdr profiles.
        """
        herdr_profiles = self.herdr_adapter.list_machines()
        profile_by_label: Dict[str, HerdrMachineProfile] = {p.label: p for p in herdr_profiles}
        profile_by_target: Dict[str, HerdrMachineProfile] = {p.target: p for p in herdr_profiles}
        profile_by_id: Dict[str, HerdrMachineProfile] = {p.id: p for p in herdr_profiles}

        # If no explicit machines configured via machine.yml, auto-populate from Herdr
        if not self.machines:
            for p in herdr_profiles:
                m_cfg = MachineConfig(
                    name=p.label,
                    server=ServerInfo(hostname=p.target),
                    test=TestConfig(type="herdr"),
                )
                self.machines.append(m_cfg)

        # Update runtime states and link with Herdr profiles
        for m in self.machines:
            if m.name not in self.state:
                self.state[m.name] = MachineRuntimeState(name=m.name)

            st = self.state[m.name]
            # Match Herdr profile by label, target, or ID
            matched = (
                profile_by_label.get(m.name)
                or profile_by_target.get(m.name)
                or profile_by_id.get(m.name)
                or (m.server.hostname and profile_by_target.get(m.server.hostname))
            )
            if matched:
                st.herdr_profile_id = matched.id
                st.herdr_enabled = matched.enabled
            else:
                st.herdr_profile_id = None
                st.herdr_enabled = None

    def _safe_check_availability(self, machine: MachineConfig) -> tuple[bool, Optional[str]]:
        try:
            return self.test_runner.check_availability(machine)
        except Exception as e:
            logger.exception("Unexpected exception checking machine '%s': %s", machine.name, e)
            return False, f"Exception during check: {e}"

    def check_machine(self, machine: MachineConfig) -> bool:
        """
        Execute an availability check and retry sequence for a single machine.
        Returns True if machine is online, False if offline/disabled.
        """
        now = self.time_fn()
        st = self.state.setdefault(machine.name, MachineRuntimeState(name=machine.name))
        st.last_check_ts = now

        max_retries = machine.max_retries if machine.max_retries is not None else self.defaults.max_retries
        retry_delay = machine.retry_delay if machine.retry_delay is not None else self.defaults.retry_delay_seconds
        auto_disable = machine.auto_disable if machine.auto_disable is not None else self.defaults.auto_disable
        auto_enable = machine.auto_enable if machine.auto_enable is not None else self.defaults.auto_enable

        logger.debug("Checking availability for machine '%s'...", machine.name)
        is_online, err_msg = self._safe_check_availability(machine)

        if is_online:
            # Handle recovery if previous failures were tracked
            if st.retry_count > 0:
                logger.info(
                    "[%s] Machine '%s' recovered after %d failed attempt(s). Resetting failure counter.",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    machine.name,
                    st.retry_count,
                )
                st.retry_count = 0

            st.is_online = True
            st.last_success_ts = now
            st.last_error_message = None

            # Optional auto-enable if machine was previously disabled in Herdr
            if auto_enable and st.herdr_profile_id and st.herdr_enabled is False:
                logger.info(
                    "[%s] Auto-enabling machine '%s' (%s) because it is back online.",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    machine.name,
                    st.herdr_profile_id,
                )
                try:
                    if self.herdr_adapter.enable_machine(st.herdr_profile_id):
                        st.herdr_enabled = True
                except Exception as e:
                    logger.exception("Error auto-enabling machine '%s': %s", machine.name, e)

            self.save_state()
            return True

        # Initial check failed!
        st.is_online = False
        st.last_failure_ts = now
        st.last_error_message = err_msg
        logger.warning(
            "[%s] Initial connection check failed for machine '%s': %s",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            machine.name,
            err_msg or "Connection refused / check failed",
        )

        # Retry logic: retry up to max_retries times
        for attempt in range(1, max_retries + 1):
            st.retry_count = attempt
            logger.info(
                "[%s] Retrying connection to machine '%s' (retry %d/%d) after %.1fs delay...",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                machine.name,
                attempt,
                max_retries,
                retry_delay,
            )
            self.sleep_fn(retry_delay)

            retry_online, retry_err = self._safe_check_availability(machine)
            if retry_online:
                logger.info(
                    "[%s] Machine '%s' recovered on retry attempt %d/%d! Resetting failure counter.",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                    machine.name,
                    attempt,
                    max_retries,
                )
                st.retry_count = 0
                st.is_online = True
                st.last_success_ts = self.time_fn()
                st.last_error_message = None
                self.save_state()
                return True

            logger.warning(
                "[%s] Retry %d/%d failed for machine '%s': %s",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                attempt,
                max_retries,
                machine.name,
                retry_err or "Offline",
            )

        # Retries exhausted!
        logger.error(
            "[%s] Machine '%s' failed after %d retries. Connection is lost.",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
            machine.name,
            max_retries,
        )

        # Disable machine in Herdr
        if auto_disable and st.herdr_profile_id:
            if st.herdr_enabled is not False:
                logger.info(
                    "[%s] Disabling machine '%s' (profile ID: %s) in Herdr...",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                    machine.name,
                    st.herdr_profile_id,
                )
                try:
                    disabled = self.herdr_adapter.disable_machine(st.herdr_profile_id)
                except Exception as e:
                    logger.exception("Error invoking disable_machine for '%s': %s", machine.name, e)
                    disabled = False

                if disabled:
                    st.herdr_enabled = False
                    st.disabled_ts = self.time_fn()
                    logger.info(
                        "[%s] Successfully disabled machine '%s' (%s) in Herdr.",
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                        machine.name,
                        st.herdr_profile_id,
                    )
                else:
                    logger.error(
                        "[%s] Failed to disable machine '%s' (%s) in Herdr.",
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                        machine.name,
                        st.herdr_profile_id,
                    )
            else:
                logger.debug("Machine '%s' is already disabled in Herdr; skipping disable call.", machine.name)
        elif not st.herdr_profile_id:
            logger.warning(
                "[%s] Cannot disable machine '%s' in Herdr: no matching profile ID found.",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time_fn())),
                machine.name,
            )

        self.save_state()
        return False

    def check_all_once(self) -> Dict[str, bool]:
        """Run an availability check across all configured machines once."""
        self.sync_machines()
        results: Dict[str, bool] = {}
        for m in self.machines:
            try:
                results[m.name] = self.check_machine(m)
            except Exception as e:
                logger.exception("Unexpected error checking machine '%s': %s", m.name, e)
                results[m.name] = False
        self.save_state()
        return results

    def run_daemon(self, max_iterations: Optional[int] = None) -> None:
        """
        Run continuous monitoring loop honoring individual machine poll intervals.
        """
        logger.info("Starting machine connection monitor daemon...")
        iterations = 0

        while max_iterations is None or iterations < max_iterations:
            self.sync_machines()
            now = self.time_fn()
            min_wait = self.defaults.poll_interval_seconds

            for m in self.machines:
                st = self.state.setdefault(m.name, MachineRuntimeState(name=m.name))
                interval = m.poll_interval if m.poll_interval is not None else self.defaults.poll_interval_seconds

                if now >= st.next_check_ts:
                    try:
                        self.check_machine(m)
                    except Exception as e:
                        logger.exception("Unexpected error monitoring machine '%s': %s", m.name, e)
                    st.next_check_ts = self.time_fn() + interval

                remaining = max(0.1, st.next_check_ts - self.time_fn())
                min_wait = min(min_wait, remaining)

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

            sleep_time = max(0.5, min_wait)
            logger.debug("Sleeping for %.1fs until next scheduled check...", sleep_time)
            self.sleep_fn(sleep_time)
