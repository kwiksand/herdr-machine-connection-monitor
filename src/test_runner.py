import logging
import socket
import subprocess
from typing import Optional

from .adapter import HerdrClientInterface
from .types import MachineConfig

logger = logging.getLogger("herdr.machine_monitor.test_runner")


class MachineTestRunner:
    """Executes availability checks on managed machines."""

    def __init__(self, herdr_adapter: Optional[HerdrClientInterface] = None):
        self.herdr_adapter = herdr_adapter

    def check_availability(self, machine: MachineConfig) -> tuple[bool, Optional[str]]:
        """
        Check if a machine is online and available.
        Returns (is_available, error_message).
        """
        test_cfg = machine.test
        server_cfg = machine.server
        test_type = (test_cfg.type or "shell").lower()
        timeout = test_cfg.timeout_seconds or 5.0

        target_host = server_cfg.ip or server_cfg.hostname or machine.name

        # If custom online command is defined, default to shell test
        if test_cfg.online:
            return self._run_shell_test(machine, test_cfg.online, timeout)

        if test_type == "ping":
            return self._run_ping_test(target_host, timeout)
        elif test_type == "tcp":
            port = test_cfg.port or server_cfg.port or 22
            return self._run_tcp_test(target_host, port, timeout)
        elif test_type == "herdr":
            return self._run_herdr_test(machine.name, timeout)
        elif test_type == "shell":
            if test_cfg.online:
                return self._run_shell_test(machine, test_cfg.online, timeout)
            # Default fallback if shell is specified without command: try herdr probe or tcp
            if self.herdr_adapter:
                return self._run_herdr_test(machine.name, timeout)
            return self._run_tcp_test(target_host, server_cfg.port or 22, timeout)
        else:
            msg = f"Unknown test type '{test_type}' for machine '{machine.name}'"
            logger.error(msg)
            return False, msg

    def _run_shell_test(
        self, machine: MachineConfig, cmd_template: str, timeout: float
    ) -> tuple[bool, Optional[str]]:
        """Run custom shell test command with parameter interpolation."""
        server = machine.server
        fmt_vars = {
            "name": machine.name,
            "ip": server.ip or "",
            "hostname": server.hostname or "",
            "port": str(server.port or 22),
        }
        cmd = cmd_template
        for k, v in fmt_vars.items():
            cmd = cmd.replace(f"{{{k}}}", v)

        logger.debug("Running shell test for %s: %s", machine.name, cmd)
        try:
            res = subprocess.run(
                ["/bin/bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if res.returncode == 0:
                return True, None
            stderr = res.stderr.strip() or res.stdout.strip()
            err_msg = f"Shell test failed with exit code {res.returncode}: {stderr}"
            return False, err_msg
        except subprocess.TimeoutExpired:
            err_msg = f"Shell test timed out after {timeout} seconds"
            return False, err_msg
        except Exception as e:
            err_msg = f"Exception executing shell test: {e}"
            return False, err_msg

    def _run_ping_test(self, target: str, timeout: float) -> tuple[bool, Optional[str]]:
        """Run ping test."""
        timeout_int = max(1, int(round(timeout)))
        try:
            res = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout_int), target],
                capture_output=True,
                text=True,
                timeout=timeout + 1.0,
                check=False,
            )
            if res.returncode == 0:
                return True, None
            return False, f"Ping to {target} failed with exit code {res.returncode}"
        except subprocess.TimeoutExpired:
            return False, f"Ping to {target} timed out after {timeout}s"
        except Exception as e:
            return False, f"Exception during ping to {target}: {e}"

    def _run_tcp_test(self, host: str, port: int, timeout: float) -> tuple[bool, Optional[str]]:
        """Run TCP connect test."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, None
        except Exception as e:
            return False, f"TCP connection to {host}:{port} failed: {e}"

    def _run_herdr_test(self, target: str, timeout: float) -> tuple[bool, Optional[str]]:
        """Run Herdr API probe test."""
        if not self.herdr_adapter:
            return False, "Herdr adapter not configured for Herdr probe test"
        success = self.herdr_adapter.probe_machine(target, timeout_seconds=timeout)
        if success:
            return True, None
        return False, f"Herdr machine probe failed for '{target}'"
