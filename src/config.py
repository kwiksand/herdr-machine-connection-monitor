from __future__ import annotations

import logging
import os
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
            herdr_config_dir = Path(herdr_config_path).expanduser().parent
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
            defaults, machines = ConfigLoader.parse_machines_file(resolved_file, defaults)
        else:
            logger.info("No machine.yml found in standard locations. Operating in discovery mode.")

        # 2. Apply environment variable overrides for defaults (takes precedence over file)
        if "HERDR_MONITOR_MAX_RETRIES" in os.environ:
            try:
                defaults.max_retries = int(os.environ["HERDR_MONITOR_MAX_RETRIES"])
            except ValueError:
                pass
        if "HERDR_MONITOR_RETRY_DELAY" in os.environ:
            try:
                defaults.retry_delay_seconds = float(os.environ["HERDR_MONITOR_RETRY_DELAY"])
            except ValueError:
                pass
        if "HERDR_MONITOR_POLL_INTERVAL" in os.environ:
            try:
                defaults.poll_interval_seconds = float(os.environ["HERDR_MONITOR_POLL_INTERVAL"])
            except ValueError:
                pass
        if "HERDR_MONITOR_AUTO_DISABLE" in os.environ:
            defaults.auto_disable = os.environ["HERDR_MONITOR_AUTO_DISABLE"].lower() in ("1", "true", "yes")
        if "HERDR_MONITOR_AUTO_ENABLE" in os.environ:
            defaults.auto_enable = os.environ["HERDR_MONITOR_AUTO_ENABLE"].lower() in ("1", "true", "yes")
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
            defaults.max_retries = int(def_block.get("max_retries", defaults.max_retries))
            defaults.retry_delay_seconds = float(
                def_block.get("retry_delay_seconds", defaults.retry_delay_seconds)
            )
            defaults.poll_interval_seconds = float(
                def_block.get("poll_interval_seconds", defaults.poll_interval_seconds)
            )
            defaults.probe_timeout_seconds = float(
                def_block.get("probe_timeout_seconds", defaults.probe_timeout_seconds)
            )
            defaults.auto_disable = bool(def_block.get("auto_disable", defaults.auto_disable))
            defaults.auto_enable = bool(def_block.get("auto_enable", defaults.auto_enable))

        machines: List[MachineConfig] = []
        raw_machines = data.get("machines", [])
        if not isinstance(raw_machines, list):
            logger.warning("'machines' field in %s is not a list", file_path)
            return defaults, machines

        for entry in raw_machines:
            if not isinstance(entry, dict) or "name" not in entry:
                continue
            name = str(entry["name"]).strip()
            if not name:
                continue

            # Server info
            srv_raw = entry.get("server", {})
            server = ServerInfo(
                ip=str(srv_raw.get("ip")) if srv_raw.get("ip") else None,
                hostname=str(srv_raw.get("hostname")) if srv_raw.get("hostname") else None,
                port=int(srv_raw.get("port", 22)),
            )

            # Test info
            test_raw = entry.get("test", {})
            test = TestConfig(
                type=str(test_raw.get("type", "shell")),
                online=str(test_raw["online"]) if "online" in test_raw and test_raw["online"] is not None else None,
                timeout_seconds=float(test_raw.get("timeout_seconds", defaults.probe_timeout_seconds)),
                port=int(test_raw["port"]) if "port" in test_raw and test_raw["port"] is not None else None,
            )

            poll_interval = float(entry["poll_interval"]) if "poll_interval" in entry else None
            max_retries = int(entry["max_retries"]) if "max_retries" in entry else None
            retry_delay = float(entry["retry_delay"]) if "retry_delay" in entry else None
            auto_disable = bool(entry["auto_disable"]) if "auto_disable" in entry else None
            auto_enable = bool(entry["auto_enable"]) if "auto_enable" in entry else None

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

        return defaults, machines
