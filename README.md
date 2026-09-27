# Herdr Machine Connection Monitor Plugin

A robust watchdog and connection management plugin for **Herdr** (terminal workspace manager for AI coding agents).

This plugin continuously or on-demand monitors remote machine connections, executes independent availability tests (such as Tailscale network status, custom shell commands, ICMP ping, TCP port checks, or Herdr CLI probes), automatically retries failed checks up to 3 times with configurable delays, and auto-disables machines in Herdr if they remain unavailable after the final retry. If a machine recovers before retries are exhausted, its counter is reset and it remains enabled.

---

## Features

- **Automated Failure Detection**: Detects unavailable machines via customizable test methods or Herdr's native CLI probe.
- **3-Retry Watchdog**: On failure, attempts exactly 3 retries (configurable) with configurable delay before taking action.
- **Auto-Disable**: Automatically disables unreachable machines in Herdr using `herdr machine disable <profile_id>`.
- **Graceful Recovery**: Automatically resets retry counters to 0 if a machine recovers during retries.
- **Machine State Isolation**: Every machine tracks retry counters and timestamps independently.
- **Fine-Grained Machine Management (`machine.yml`)**: Configure custom test commands (e.g. Tailscale peer check, ping, socket probe), per-machine polling intervals, and server details.
- **Auto-Discovery Fallback**: In the absence of a `machine.yml`, automatically discovers all active machines configured in Herdr.
- **Resilient**: Traps all timeouts, network errors, and unexpected API exceptions without crashing or entering tight loops.

---

## Installation & Linking

Link the plugin into your local Herdr server:

```bash
herdr plugin link /home/shannon/ai_workspace/herdr_plugins/plugins/machine-connection-monitor
```

Verify that the plugin is installed and enabled:

```bash
herdr plugin list
```

To unlink the plugin:

```bash
herdr plugin unlink machine-connection-monitor
```

---

## Configuration

The plugin searches for machine and monitor configuration in the following priority order:
1. File explicitly passed via `--machines-file /path/to/machine.yml` or `-m`
2. `HERDR_MACHINES_FILE` environment variable
3. `~/.config/herdr/machine.yml` (or `machines.yml`, `machine.yaml`)
4. `${HERDR_PLUGIN_CONFIG_DIR}/machine.yml`
5. `plugins/machine-connection-monitor/machine.yml`
6. Fallback: Auto-discovers machines directly from `herdr machine list --json`

### `machine.yml` Structure

Copy [machine.example.yml](file:///home/shannon/ai_workspace/herdr_plugins/plugins/machine-connection-monitor/machine.example.yml) to `~/.config/herdr/machine.yml`:

```yaml
defaults:
  max_retries: 3                  # Number of retries before disabling a machine
  retry_delay_seconds: 5.0        # Delay between retries (seconds)
  poll_interval_seconds: 30.0     # Default polling interval in daemon mode (seconds)
  probe_timeout_seconds: 5.0      # Timeout for individual test execution (seconds)
  auto_disable: true              # Set machine to disabled in Herdr after 3 failed retries
  auto_enable: false              # Re-enable machine in Herdr when it comes back online

machines:
  # Example 1: Tailscale status check
  - name: server1
    poll_interval: 30
    server:
      ip: 192.168.0.2
      hostname: server1.mydomain.com
    test:
      online: '[[ "$(tailscale status --json 2>/dev/null | jq -r .BackendState)" = "Running" ]]'

  # Example 2: Ping test with parameter interpolation ({ip}, {hostname}, {name}, {port})
  - name: server2
    poll_interval: 60
    server:
      ip: 192.168.0.3
      hostname: server2
    test:
      online: 'ping -c 1 -W 2 {ip} >/dev/null 2>&1'

  # Example 3: TCP port check (e.g. SSH port 22)
  - name: server3
    poll_interval: 45
    server:
      ip: 192.168.0.4
      port: 22
    test:
      type: tcp
      timeout_seconds: 3.0

  # Example 4: Native Herdr CLI probe (herdr --machine <name> workspace list)
  - name: server4
    poll_interval: 60
    server:
      hostname: server4
    test:
      type: herdr
      timeout_seconds: 5.0
```

### Supported Test Types
- **`shell` (default when `online` script is present)**: Runs a bash command. Exit code `0` is considered online; any non-zero exit code or timeout is considered offline. The placeholders `{name}`, `{ip}`, `{hostname}`, and `{port}` are automatically substituted.
- **`tcp`**: Attempts a direct TCP socket connection to `{ip/hostname}:{port}`.
- **`ping`**: Sends an ICMP echo request (`ping -c 1 -W <timeout>`).
- **`herdr`**: Invokes `herdr --machine <name> workspace list` with a timeout.

### Environment Variable Overrides

| Environment Variable | Description | Default |
| :--- | :--- | :--- |
| `HERDR_MONITOR_MAX_RETRIES` | Overrides default maximum retries | `3` |
| `HERDR_MONITOR_RETRY_DELAY` | Overrides default retry delay (seconds) | `5.0` |
| `HERDR_MONITOR_POLL_INTERVAL` | Overrides default polling interval (seconds) | `30.0` |
| `HERDR_MONITOR_AUTO_DISABLE` | Toggle automatic disabling (`1`/`0`, `true`/`false`) | `true` |
| `HERDR_MONITOR_AUTO_ENABLE` | Toggle automatic re-enabling (`1`/`0`, `true`/`false`) | `false` |
| `HERDR_MACHINES_FILE` | Explicit path to `machine.yml` | None |
| `HERDR_BIN_PATH` | Path to `herdr` executable | `herdr` |

---

## Usage & Actions

### Via Herdr Plugin Actions

```bash
# Check all machine connections once
herdr plugin action invoke check --plugin machine-connection-monitor

# Show current machine status table
herdr plugin action invoke status --plugin machine-connection-monitor

# Sync configuration with Herdr saved profiles
herdr plugin action invoke sync --plugin machine-connection-monitor

# Run as background daemon
herdr plugin action invoke daemon --plugin machine-connection-monitor

# Inspect action logs
herdr plugin log list --plugin machine-connection-monitor
```

### Direct CLI Execution

```bash
# Display formatted status table
python3 plugins/machine-connection-monitor/main.py status

# Output status in JSON format
python3 plugins/machine-connection-monitor/main.py status --json

# Run one-shot check using custom machine.yml
python3 plugins/machine-connection-monitor/main.py check -m ~/.config/herdr/machine.yml

# Run as daemon with debug logging
python3 plugins/machine-connection-monitor/main.py daemon -v
```

---

## Behavior & State Flow

```
[Machine Monitored]
        │
        ▼
  [Check Availability]
    ├── Online ────────> Reset retry_count to 0 (Keep Enabled)
    └── Offline
          │
          ▼
     [Retry 1/3] (waits retry_delay)
       ├── Online ─────> Reset retry_count to 0 (Keep Enabled)
       └── Offline
             │
             ▼
        [Retry 2/3] (waits retry_delay)
          ├── Online ──> Reset retry_count to 0 (Keep Enabled)
          └── Offline
                │
                ▼
           [Retry 3/3] (waits retry_delay)
             ├── Online > Reset retry_count to 0 (Keep Enabled)
             └── Offline
                   │
                   ▼
       [Set State to Disabled]
       Invokes: herdr machine disable <profile_id>
```

---

## Assumptions About Herdr API

1. **Profile Identification**: Machine profiles have a unique 32-character lowercase hexadecimal `id` (e.g., `8724cfc1531e3d6542673387623b8a52`) returned by `herdr machine list --json`.
2. **Disable Command**: Machine disabling is performed via `herdr machine disable <PROFILE_ID>`, which strictly requires the 32-hex `id` (not the label).
3. **Enable Command**: Machine enabling is performed via `herdr machine enable <PROFILE_ID>`.
4. **Availability Notification**: Herdr 0.9.x does not produce push events for SSH disconnects in `SubscriptionEventKind`. Machine availability is evaluated through configured test methods (Tailscale, shell scripts, TCP, ping) or CLI API probes (`herdr --machine <name> workspace list`).
5. **Isolation**: All interactions with the Herdr CLI are isolated within `HerdrCliAdapter`, allowing clean testing and trivial swapping should Herdr introduce direct socket API machine endpoints in the future.

---

## Security Considerations

- **`test.online` commands are arbitrary shell code.** They are executed via `/bin/bash -c` as the user running the plugin, with the plugin's full inherited environment. Whoever can write the resolved `machine.yml` (notably `~/.config/herdr/machine.yml`) effectively obtains recurring, scheduled code execution every time a check runs. In an agent-driven workspace this is a persistence primitive: treat that file like a crontab — keep it owner-writable only (`chmod 600`), review it after exposing an agent to untrusted input, and only configure tests you have read and understood.
- **State file permissions.** `state.json` contains machine names, IPs, Herdr profile IDs and probe error output (which can include stderr from your test commands). The plugin creates it mode `0600` inside a `0700` directory. If you relocate it via `HERDR_PLUGIN_STATE_DIR` or `HERDR_PLUGIN_CONFIG_DIR`, keep those permissions.
- **Fail-safe config handling.** If a machines file is found but cannot be parsed (e.g. PyYAML missing), the plugin disables `auto_disable`/`auto_enable` for that run instead of silently falling back to discovery mode with different probe semantics. Malformed individual machine entries are skipped with a warning rather than aborting the whole load. Numeric settings are clamped to sane bounds, so a negative `timeout_seconds` or `max_retries` can never trigger an immediate false "offline" verdict and auto-disable cascade, and an absurd `retry_delay` cannot hang the daemon.
- **Destructive actions are re-verified.** Before enabling or disabling a machine, the plugin re-reads the live enabled state from `herdr machine list` instead of trusting its cache, validates that profile IDs match the documented 32-hex format before invoking state-changing CLI commands, and warns when multiple Herdr profiles share a label or target (ambiguous matching could act on the wrong profile).
- **Single-pass interpolation.** `{name}`, `{ip}`, `{hostname}` and `{port}` are substituted in one pass and never re-scanned, so a value containing a placeholder token stays literal.
- **Timeouts reap process trees.** Shell tests and Herdr CLI calls run in their own process group; on timeout the whole group is killed so failed probes cannot leave orphaned children behind.
- **Log/state injection.** Probe error messages embed untrusted stderr; control characters are flattened to a single line before anything is logged or persisted.
- **Probe failure is not verified offline.** A failing test (missing `jq`, missing `ping`, broken script) is indistinguishable from a down machine and will still count toward auto-disable. Keep test commands dependency-light and validate them manually before enabling `auto_disable`.
- **`auto_enable` can undo manual decisions.** With `auto_enable: true`, a machine you disabled deliberately will be silently re-enabled once its probe succeeds (default is `false`). Prefer keeping it off unless you understand the implications.
