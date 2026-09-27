import logging
import os
import re
import signal
import socket
import subprocess
from typing import Optional

from .adapter import HerdrClientInterface
from .types import MachineConfig

logger = logging.getLogger("herdr.machine_monitor.test_runner")

# Probe targets are remote hostnames / IP literals. Rejecting anything else
# (notably values starting with "-") prevents argument injection into the
# ping CLI and confusion in socket calls.
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

# Placeholders interpolated into shell test commands. Substitution happens in
# a single pass so interpolated values are never re-scanned for placeholders
# (a machine named "x{port}" must not have its name re-expanded).
_PLACEHOLDER_RE = re.compile(r"\{(name|ip|hostname|port)\}")


def _is_valid_target(target: str) -> bool:
    """Return True if target looks like a hostname or IP literal, not a CLI flag."""
    return bool(target) and not target.startswith("-") and bool(_TARGET_RE.fullmatch(target))


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill a process started in its own session, including its children."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def interpolate_placeholders(cmd_template: str, fmt_vars: dict) -> str:
    """Substitute {name}/{ip}/{hostname}/{port} in a single pass.

    Values are never re-scanned, so a value containing a placeholder token
    (e.g. a machine named "x{port}") stays literal instead of being expanded
    by a later substitution pass.
    """
    return _PLACEHOLDER_RE.sub(lambda m: fmt_vars.get(m.group(1), m.group(0)), cmd_template)


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
        # Defense-in-depth: a malformed MachineConfig (e.g. built directly by
        # a consumer rather than through the config loader) must not produce
        # a negative timeout, which some probes treat as an instant failure.
        timeout = test_cfg.timeout_seconds if (test_cfg.timeout_seconds and test_cfg.timeout_seconds > 0) else 5.0

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
        # Single-pass substitution: interpolated values are never re-scanned,
        # so a value containing a placeholder token stays literal.
        cmd = interpolate_placeholders(cmd_template, fmt_vars)

        logger.debug("Running shell test for %s: %s", machine.name, cmd)
        try:
            # start_new_session puts bash and everything it spawns into a
            # separate process group so a timeout can reap the whole tree
            # instead of orphaning grandchildren.
            proc = subprocess.Popen(
                ["/bin/bash", "-c", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            return False, "Shell test infrastructure error: /bin/bash is not available"
        except Exception as e:
            err_msg = f"Exception executing shell test: {e}"
            return False, err_msg

        try:
            res_out, res_err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.communicate()
            err_msg = f"Shell test timed out after {timeout} seconds"
            return False, err_msg
        except Exception as e:
            _kill_process_group(proc)
            err_msg = f"Exception executing shell test: {e}"
            return False, err_msg

        if proc.returncode == 0:
            return True, None
        stderr = (res_err or "").strip() or (res_out or "").strip()
        err_msg = f"Shell test failed with exit code {proc.returncode}: {stderr}"
        return False, err_msg

    def _run_ping_test(self, target: str, timeout: float) -> tuple[bool, Optional[str]]:
        """Run ping test."""
        if not _is_valid_target(target):
            msg = f"Invalid ping target {target!r} (expected a hostname or IP address)"
            logger.error(msg)
            return False, msg
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
        except FileNotFoundError:
            return False, f"Ping test infrastructure error: 'ping' is not installed"
        except Exception as e:
            return False, f"Exception during ping to {target}: {e}"

    def _run_tcp_test(self, host: str, port: int, timeout: float) -> tuple[bool, Optional[str]]:
        """Run TCP connect test."""
        if not _is_valid_target(host):
            msg = f"Invalid TCP target {host!r} (expected a hostname or IP address)"
            logger.error(msg)
            return False, msg
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