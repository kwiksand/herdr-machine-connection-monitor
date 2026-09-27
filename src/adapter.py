from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
from abc import ABC, abstractmethod
from typing import List, Optional

from .types import HerdrMachineProfile

logger = logging.getLogger("herdr.machine_monitor.adapter")

# Herdr machine profiles are identified by a 32-character lowercase hex id.
# Enforcing the format before invoking state-changing CLI commands is
# defense-in-depth against a poisoned state file or malformed CLI output.
HERDR_PROFILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _is_valid_profile_id(profile_id: object) -> bool:
    return isinstance(profile_id, str) and bool(HERDR_PROFILE_ID_RE.fullmatch(profile_id))


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill a process started in its own session, including its children."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class HerdrClientInterface(ABC):
    """Abstract interface defining all interactions with Herdr."""

    @abstractmethod
    def list_machines(self) -> List[HerdrMachineProfile]:
        """List all saved Herdr machine connection profiles."""
        raise NotImplementedError

    @abstractmethod
    def disable_machine(self, profile_id: str) -> bool:
        """Disable a saved Herdr machine by profile ID."""
        raise NotImplementedError

    @abstractmethod
    def enable_machine(self, profile_id: str) -> bool:
        """Enable a saved Herdr machine by profile ID."""
        raise NotImplementedError

    @abstractmethod
    def probe_machine(self, target: str, timeout_seconds: float = 5.0) -> bool:
        """Probe an active machine connection using Herdr's API."""
        raise NotImplementedError


class HerdrCliAdapter(HerdrClientInterface):
    """Concrete Herdr client adapter communicating via the herdr CLI."""

    def __init__(self, herdr_bin: Optional[str] = None):
        self.herdr_bin = herdr_bin or os.environ.get("HERDR_BIN_PATH") or "herdr"

    def _run_cli(self, args: List[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        cmd = [self.herdr_bin] + args
        logger.debug("Executing CLI command: %s", " ".join(cmd))
        try:
            # start_new_session lets a timeout reap the CLI process together
            # with any children it spawned.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            logger.error("Herdr binary not found at '%s'", self.herdr_bin)
            raise e
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("CLI command timed out after %s seconds: %s", timeout, " ".join(cmd))
            _kill_process_group(proc)
            proc.communicate()
            raise
        except Exception:
            _kill_process_group(proc)
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def list_machines(self) -> List[HerdrMachineProfile]:
        try:
            res = self._run_cli(["machine", "list", "--json"], timeout=10.0)
            if res.returncode != 0:
                logger.error("Failed to list machines from Herdr: %s", res.stderr.strip())
                return []
            data = json.loads(res.stdout)
            profiles = []
            for item in data:
                profiles.append(
                    HerdrMachineProfile(
                        id=str(item.get("id", "")),
                        label=str(item.get("label", "")),
                        target=str(item.get("target", "")),
                        session=str(item.get("session", "default")),
                        enabled=bool(item.get("enabled", False)),
                        selected=bool(item.get("selected", False)),
                    )
                )
            return profiles
        except Exception as e:
            logger.exception("Error while listing machines from Herdr: %s", e)
            return []

    def disable_machine(self, profile_id: str) -> bool:
        if not profile_id:
            logger.error("Cannot disable machine with empty profile_id")
            return False
        if not _is_valid_profile_id(profile_id):
            logger.error(
                "Refusing to disable machine: %r is not a valid 32-hex Herdr profile id",
                profile_id,
            )
            return False
        try:
            logger.info("Disabling machine profile %s in Herdr...", profile_id)
            res = self._run_cli(["machine", "disable", profile_id], timeout=10.0)
            if res.returncode == 0:
                logger.info("Successfully disabled machine %s", profile_id)
                return True
            else:
                logger.error("Failed to disable machine %s: %s", profile_id, res.stderr.strip() or res.stdout.strip())
                return False
        except Exception as e:
            logger.exception("Exception while disabling machine %s: %s", profile_id, e)
            return False

    def enable_machine(self, profile_id: str) -> bool:
        if not profile_id:
            logger.error("Cannot enable machine with empty profile_id")
            return False
        if not _is_valid_profile_id(profile_id):
            logger.error(
                "Refusing to enable machine: %r is not a valid 32-hex Herdr profile id",
                profile_id,
            )
            return False
        try:
            logger.info("Enabling machine profile %s in Herdr...", profile_id)
            res = self._run_cli(["machine", "enable", profile_id], timeout=10.0)
            if res.returncode == 0:
                logger.info("Successfully enabled machine %s", profile_id)
                return True
            else:
                logger.error("Failed to enable machine %s: %s", profile_id, res.stderr.strip() or res.stdout.strip())
                return False
        except Exception as e:
            logger.exception("Exception while enabling machine %s: %s", profile_id, e)
            return False

    def probe_machine(self, target: str, timeout_seconds: float = 5.0) -> bool:
        if not target:
            return False
        try:
            res = self._run_cli(["--machine", target, "workspace", "list"], timeout=timeout_seconds)
            return res.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except Exception as e:
            logger.debug("Exception probing machine %s: %s", target, e)
            return False
