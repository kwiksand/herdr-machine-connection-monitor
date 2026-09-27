import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Add plugin root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from src.adapter import HerdrCliAdapter  # noqa: E402
from src.config import ConfigLoader  # noqa: E402
from src.monitor import MachineConnectionMonitor  # noqa: E402
from src.test_runner import MachineTestRunner  # noqa: E402


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=log_format, datefmt="%Y-%m-%d %H:%M:%S")


def format_ts(ts: float | None) -> str:
    if not ts:
        return "Never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def print_status(monitor: MachineConnectionMonitor, as_json: bool = False) -> None:
    monitor.sync_machines()
    data = []
    for m in monitor.machines:
        st = monitor.state.get(m.name)
        data.append({
            "name": m.name,
            "ip": m.server.ip,
            "hostname": m.server.hostname,
            "poll_interval": m.poll_interval or monitor.defaults.poll_interval_seconds,
            "max_retries": m.max_retries or monitor.defaults.max_retries,
            "retry_count": st.retry_count if st else 0,
            "is_online": st.is_online if st else None,
            "herdr_profile_id": st.herdr_profile_id if st else None,
            "herdr_enabled": st.herdr_enabled if st else None,
            "last_check": format_ts(st.last_check_ts) if st else "Never",
            "last_error": st.last_error_message if st else None,
        })

    if as_json:
        print(json.dumps(data, indent=2))
        return

    print("=== Managed Herdr Machines ===")
    print(f"{'Machine':<18} {'Online':<8} {'Retries':<8} {'Herdr ID':<34} {'Herdr Enabled':<14} {'Last Check'}")
    print("-" * 105)
    for row in data:
        online_str = "Yes" if row["is_online"] is True else ("No" if row["is_online"] is False else "Unknown")
        enabled_str = (
            "Yes" if row["herdr_enabled"] is True else ("No" if row["herdr_enabled"] is False else "Not Linked")
        )
        herdr_id = row["herdr_profile_id"] or "None"
        retries_str = f"{row['retry_count']}/{row['max_retries']}"
        print(
            f"{row['name']:<18} {online_str:<8} {retries_str:<8} "
            f"{herdr_id:<34} {enabled_str:<14} {row['last_check']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Herdr Machine Connection Monitor and Watchdog",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=["check", "daemon", "status", "sync"],
        help="Action to perform (default: check)",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="Run a single availability check of all machines and exit",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously as a monitoring daemon",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current machine status and exit",
    )
    parser.add_argument(
        "-m",
        "--machines-file",
        dest="machines_file",
        default=None,
        help="Path to machine.yml configuration file",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Determine command action
    action = args.action
    if args.daemon:
        action = "daemon"
    elif args.status:
        action = "status"
    elif args.check_once:
        action = "check"

    try:
        defaults, machines = ConfigLoader.load_defaults_and_machines(
            machines_file_path=args.machines_file,
        )
    except Exception as e:
        logging.error("Failed to load plugin configuration: %s", e)
        return 2

    adapter = HerdrCliAdapter(herdr_bin=defaults.herdr_bin)
    test_runner = MachineTestRunner(herdr_adapter=adapter)
    monitor = MachineConnectionMonitor(
        herdr_adapter=adapter,
        test_runner=test_runner,
        defaults=defaults,
        machines=machines,
    )

    if action == "status":
        print_status(monitor, as_json=args.output_json)
        return 0

    if action == "sync":
        monitor.sync_machines()
        print_status(monitor, as_json=args.output_json)
        return 0

    if action == "daemon":
        try:
            monitor.run_daemon()
            return 0
        except KeyboardInterrupt:
            logging.info("Monitor daemon stopped by user.")
            return 0

    # Default action: check
    results = monitor.check_all_once()
    if args.output_json:
        print(json.dumps(results, indent=2))
    all_ok = all(results.values()) if results else True
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
