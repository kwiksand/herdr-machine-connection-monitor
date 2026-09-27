from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None

try:
    import tomllib
except ImportError:
    tomllib = None

from .types import GlobalDefaults, MachineConfig, ServerInfo, TestConfig

logger = logging.getLogger("herdr.machine_monitor.config")

# Sanity bounds for numeric settings. Out-of-range values are clamped (with a
# warning) instead of being trusted: a mistyped or tampered value such as
# timeout_seconds: -1 would otherwise turn every probe into an immediate
# failure and trigger a mass auto-disable cascade, and an unbounded retry
# delay would hang the monitor daemon.
MAX_RETRIES_RANGE: Tuple[int, int] = (0, 20)
RETRY_DELAY_RANGE: Tuple[float, float] = (0.0, 3600.0)
POLL_INTERVAL_RANGE: Tuple[float, float] = (1.0, 86400.0)
PROBE_TIMEOUT_RANGE: Tuple[float, float] = (0.1, 3600.0)
PORT_RANGE: Tuple[int, int] = (1, 65535)

_KNOWN_TEST_TYPES = ("shell", "ping", "tcp", "herdr")

# Machine names end up in log lines, the state file and shell test
# interpolation; flag names containing characters that make those contexts
# ambiguous (shell metacharacters, whitespace, control characters, ...).
_MACHINE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_TRUE_STRINGS = ("1", "true", "yes", "on")
_FALSE_STRINGS = ("0", "false", "no", "off")


def _clamp_float(
    name: str, value: object, bounds: Tuple[float, float], default: Optional[float]
) -> Optional[float]:
    """Coerce value to float and clamp it into bounds, falling back to default."""
    lo, hi = bounds
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Invalid value for %s: %r; using %s", name, value, default)
        return default
    if v != v:  # NaN
        logger.warning("Invalid value for %s: NaN; using %s", name, default)
        return default
    if v < lo or v > hi:
        clamped = min(max(v, lo), hi)
        logger.warning(
            "Value for %s out of range [%s, %s]: %s clamped to %s", name, lo, hi, v, clamped
        )
        return clamped
    return v


def _clamp_int(
    name: str, value: object, bounds: Tuple[int, int], default: Optional[int]
) -> Optional[int]:
    """Coerce value to int and clamp it into bounds, falling back to default."""
    lo, hi = bounds
    try:
        v = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid value for %s: %r; using %s", name, value, default)
        return default
    if v < lo or v > hi:
        clamped = min(max(v, lo), hi)
        logger.warning(
            "Value for %s out of range [%s, %s]: %s clamped to %s", name, lo, hi, v, clamped
        )
        return clamped
    return v


def _coerce_bool(name: str, value: object, default: Optional[bool]) -> Optional[bool]:
    """Interpret YAML/env booleans strictly.

    A bare string such as "false" is truthy in Python; treating it as True
    would silently invert options like auto_disable, so parse it explicitly.
    """
    if isinstance(value, bool) or value is None:
        return value if value is not None else default
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
    logger.warning("Invalid boolean for %s: %r; using %s", name, value, default)
    return default


class ConfigLoader:
    """Loads configuration and machine lists from YAML/TOML and environment variables."""

    @staticmethod
    def resolve_machines_file(custom_path: Optional[str] = None) -> Optional[Path]:
        """Resolve the path to machine.yml based on standard search locations."""
        if custom_path:
            p = Path(custom_path).expanduser().resolve()
            if p.exists():
                return p
            logger.warning("Specified machines file does not exist: %s", custom_path)

        env_path = os.environ.get("HERDR_MACHINES_FILE")
        if env_path:
            p = Path(env_path).expanduser().resolve()
            if p.exists():
                return p

        # Check in ~/.config/herdr/ (or HERDR_CONFIG_PATH directory)
        herdr_config_path = os.environ.get("HERDR_CONFIG_PATH")
        if herdr_config_path:
            candidate = Path(herdr_config_path).expanduser()
            # Accept both a directory and a file path for backwards compatibility.
            herdr_config_dir = candidate if candidate.is_dir() else candidate.parent
        else:
            herdr_config_dir = Path.home() / ".config" / "herdr"

        candidates = [
            herdr_config_dir / "machine.yml",
            herdr_config_dir / "machines.yml",
            herdr_config_dir / "machine.yaml",
            herdr_config_dir / "machines.yaml",
        ]

        # Check plugin config dir if set
        plugin_config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        if plugin_config_dir:
            candidates.extend([
                Path(plugin_config_dir) / "machine.yml",
                Path(plugin_config_dir) / "machines.yml",
            ])

        # Check plugin root
        plugin_root = os.environ.get("HERDR_PLUGIN_ROOT")
        if plugin_root:
            candidates.append(Path(plugin_root) / "machine.yml")

        for c in candidates:
            if c.exists():
                return c.resolve()

        return None

    @staticmethod
    def load_defaults_and_machines(
        machines_file_path: Optional[str] = None,
        config_file_path: Optional[str] = None,
    ) -> Tuple[GlobalDefaults, List[MachineConfig]]:
        """Load global defaults and the list of machines to monitor."""
        defaults = GlobalDefaults()
        machines: List[MachineConfig] = []

        # 1. Check for machine.yml
        resolved_file = ConfigLoader.resolve_machines_file(machines_file_path)
        if resolved_file:
            logger.info("Loading machine configuration from %s", resolved_file)
            if yaml is None:
                # A machines file exists but cannot be parsed. Do not silently
                # fall back to discovery mode with different test semantics:
                # probes validated by the user would be replaced by untested
                # defaults, and destructive automation could act on them.
                logger.error(
                    "PyYAML is not installed; cannot parse machines file %s. "
                    "Disabling auto_disable/auto_enable for this run (fail-safe).",
                    resolved_file,
                )
                defaults.auto_disable = False
                defaults.auto_enable = False
            else:
                defaults, machines = ConfigLoader.parse_machines_file(resolved_file, defaults)
        else:
            logger.info("No machine.yml found in standard locations. Operating in discovery mode.")

        # 2. Apply environment variable overrides for defaults (takes precedence over file)
        if "HERDR_MONITOR_MAX_RETRIES" in os.environ:
            defaults.max_retries = _clamp_int(
                "HERDR_MONITOR_MAX_RETRIES",
                os.environ["HERDR_MONITOR_MAX_RETRIES"],
                MAX_RETRIES_RANGE,
                defaults.max_retries,
            )
        if "HERDR_MONITOR_RETRY_DELAY" in os.environ:
            defaults.retry_delay_seconds = _clamp_float(
                "HERDR_MONITOR_RETRY_DELAY",
                os.environ["HERDR_MONITOR_RETRY_DELAY"],
                RETRY_DELAY_RANGE,
                defaults.retry_delay_seconds,
            )
        if "HERDR_MONITOR_POLL_INTERVAL" in os.environ:
            defaults.poll_interval_seconds = _clamp_float(
                "HERDR_MONITOR_POLL_INTERVAL",
                os.environ["HERDR_MONITOR_POLL_INTERVAL"],
                POLL_INTERVAL_RANGE,
                defaults.poll_interval_seconds,
            )
        if "HERDR_MONITOR_AUTO_DISABLE" in os.environ:
            defaults.auto_disable = _coerce_bool(
                "HERDR_MONITOR_AUTO_DISABLE",
                os.environ["HERDR_MONITOR_AUTO_DISABLE"],
                defaults.auto_disable,
            )
        if "HERDR_MONITOR_AUTO_ENABLE" in os.environ:
            defaults.auto_enable = _coerce_bool(
                "HERDR_MONITOR_AUTO_ENABLE",
                os.environ["HERDR_MONITOR_AUTO_ENABLE"],
                defaults.auto_enable,
            )
        if "HERDR_BIN_PATH" in os.environ:
            defaults.herdr_bin = os.environ["HERDR_BIN_PATH"]

        return defaults, machines

    @staticmethod
    def parse_machines_file(
        file_path: Path, current_defaults: GlobalDefaults
    ) -> Tuple[GlobalDefaults, List[MachineConfig]]:
        """Parse a machine.yml file."""
        if yaml is None:
            logger.error("PyYAML is not installed. Cannot parse YAML machine file %s", file_path)
            return current_defaults, []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("Failed to read machine file %s: %s", file_path, e)
            return current_defaults, []

        defaults = current_defaults
        # Parse defaults block if present
        def_block = data.get("defaults", {})
        if isinstance(def_block, dict):
            defaults.max_retries = _clamp_int(
                "defaults.max_retries",
                def_block.get("max_retries", defaults.max_retries),
                MAX_RETRIES_RANGE,
                defaults.max_retries,
            )
            defaults.retry_delay_seconds = _clamp_float(
                "defaults.retry_delay_seconds",
                def_block.get("retry_delay_seconds", defaults.retry_delay_seconds),
                RETRY_DELAY_RANGE,
                defaults.retry_delay_seconds,
            )
            defaults.poll_interval_seconds = _clamp_float(
                "defaults.poll_interval_seconds",
                def_block.get("poll_interval_seconds", defaults.poll_interval_seconds),
                POLL_INTERVAL_RANGE,
                defaults.poll_interval_seconds,
            )
            defaults.probe_timeout_seconds = _clamp_float(
                "defaults.probe_timeout_seconds",
                def_block.get("probe_timeout_seconds", defaults.probe_timeout_seconds),
                PROBE_TIMEOUT_RANGE,
                defaults.probe_timeout_seconds,
            )
            defaults.auto_disable = _coerce_bool(
                "defaults.auto_disable",
                def_block.get("auto_disable", defaults.auto_disable),
                defaults.auto_disable,
            )
            defaults.auto_enable = _coerce_bool(
                "defaults.auto_enable",
                def_block.get("auto_enable", defaults.auto_enable),
                defaults.auto_enable,
            )

        machines: List[MachineConfig] = []
        raw_machines = data.get("machines", [])
        if not isinstance(raw_machines, list):
            logger.warning("'machines' field in %s is not a list", file_path)
            return defaults, machines

        for idx, entry in enumerate(raw_machines):
            if not isinstance(entry, dict) or "name" not in entry:
                logger.warning("Skipping machine entry #%d: not a mapping or missing 'name'", idx)
                continue
            try:
                name = str(entry["name"]).strip()
                if not name:
                    continue
                if not _MACHINE_NAME_RE.fullmatch(name):
                    logger.warning(
                        "Machine name '%s' contains unusual characters; it is used in logs, "
                        "the state file and shell test interpolation",
                        name,
                    )

                # Server info
                srv_raw = entry.get("server", {}) or {}
                if not isinstance(srv_raw, dict):
                    srv_raw = {}
                server = ServerInfo(
                    ip=str(srv_raw.get("ip")) if srv_raw.get("ip") else None,
                    hostname=str(srv_raw.get("hostname")) if srv_raw.get("hostname") else None,
                    port=_clamp_int(
                        f"machines[{name}].server.port",
                        srv_raw.get("port", 22),
                        PORT_RANGE,
                        22,
                    ) or 22,
                )

                # Test info
                test_raw = entry.get("test", {}) or {}
                if not isinstance(test_raw, dict):
                    test_raw = {}
                test_type = str(test_raw.get("type", "shell"))
                if test_type.lower() not in _KNOWN_TEST_TYPES and "online" not in test_raw:
                    logger.warning(
                        "Machine '%s' has unknown test type '%s'; probes will fail until fixed",
                        name,
                        test_type,
                    )
                test = TestConfig(
                    type=test_type,
                    online=str(test_raw["online"]) if test_raw.get("online") is not None else None,
                    timeout_seconds=_clamp_float(
                        f"machines[{name}].test.timeout_seconds",
                        test_raw.get("timeout_seconds", defaults.probe_timeout_seconds),
                        PROBE_TIMEOUT_RANGE,
                        defaults.probe_timeout_seconds,
                    ) or defaults.probe_timeout_seconds,
                    port=(
                        _clamp_int(
                            f"machines[{name}].test.port", test_raw["port"], PORT_RANGE, None
                        )
                        if test_raw.get("port") is not None
                        else None
                    ),
                )

                poll_interval = (
                    _clamp_float(
                        f"machines[{name}].poll_interval",
                        entry["poll_interval"],
                        POLL_INTERVAL_RANGE,
                        None,
                    )
                    if "poll_interval" in entry
                    else None
                )
                max_retries = (
                    _clamp_int(
                        f"machines[{name}].max_retries",
                        entry["max_retries"],
                        MAX_RETRIES_RANGE,
                        None,
                    )
                    if "max_retries" in entry
                    else None
                )
                retry_delay = (
                    _clamp_float(
                        f"machines[{name}].retry_delay",
                        entry["retry_delay"],
                        RETRY_DELAY_RANGE,
                        None,
                    )
                    if "retry_delay" in entry
                    else None
                )
                auto_disable = (
                    _coerce_bool(f"machines[{name}].auto_disable", entry["auto_disable"], None)
                    if "auto_disable" in entry
                    else None
                )
                auto_enable = (
                    _coerce_bool(f"machines[{name}].auto_enable", entry["auto_enable"], None)
                    if "auto_enable" in entry
                    else None
                )

                machines.append(
                    MachineConfig(
                        name=name,
                        server=server,
                        test=test,
                        poll_interval=poll_interval,
                        max_retries=max_retries,
                        retry_delay=retry_delay,
                        auto_disable=auto_disable,
                        auto_enable=auto_enable,
                    )
                )
            except Exception as e:
                # One malformed entry must not take down monitoring for every
                # other machine; skip it and keep going.
                logger.warning(
                    "Skipping malformed machine entry #%d ('%s'): %s",
                    idx,
                    entry.get("name", "?") if isinstance(entry, dict) else "?",
                    e,
                )
                continue

        return defaults, machines
