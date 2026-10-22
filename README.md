# OpenCode Pushover Monitor

Get push notifications on your phone when [OpenCode](https://opencode.ai) sessions need your attention -- waiting for input, errored, idle, or stalled.

Uses [Pushover](https://pushover.net) to deliver notifications with per-state priority and sound.

Requires **OpenCode v2** (uses the `opencode api` command to talk to the background service).

## Quick Start

```bash
# Store your Pushover credentials in the macOS Keychain (one time, prompts without echo)
./start_monitor.sh --setup-keychain
```
```bash
# Launch (creates a venv, installs deps, starts monitoring in background)
./start_monitor.sh
```

Or run directly without the launcher:

```bash
pip install -r requirements.txt
python3 pushover_monitor.py --setup-keychain   # one time
python3 pushover_monitor.py
```

## Credentials

Credentials are resolved at runtime in this order:

1. `--app-token` / `--user-key` flags (avoid: visible in `ps`)
2. `PUSHOVER_APP_TOKEN` / `PUSHOVER_USER_KEY` environment variables
3. macOS login Keychain, service `oc-pushover-monitor`, accounts `app-token` and `user-key`

The Keychain is recommended. Secrets stay encrypted at rest, out of your shell profile, and out of process arguments. `--setup-keychain` stores them for you, or you can add them by hand (`security add-generic-password -U -s oc-pushover-monitor -a app-token -w` prompts for the value).

To migrate from `~/.zshrc`, run `./start_monitor.sh --setup-keychain`, delete the `export PUSHOVER_*` lines, and open a new shell.

To inspect or remove them:

```bash
security find-generic-password -s oc-pushover-monitor -a app-token      # metadata only
security delete-generic-password -s oc-pushover-monitor -a app-token
security delete-generic-password -s oc-pushover-monitor -a user-key
```

## Prerequisites

1. **Python 3** installed
2. **OpenCode v2** on your `$PATH` (`opencode --version`)
3. **Pushover account** -- install the [Pushover app](https://pushover.net/clients) on your phone and create an account
4. **Pushover credentials:**
   - **User Key** -- on your Pushover dashboard
   - **App Token** -- create one at https://pushover.net/apps/build (name it "OpenCode" or similar)

## Notification Types

| State | Priority | Sound | Trigger |
|-------|----------|-------|---------|
| Waiting for input | High | magic | A `question` tool call is running, a form or permission request is pending, or the turn finished and the last paragraph contains a `?` (prose question) |
| Error | High | siren | Latest assistant/idle message has a non-abort error (e.g. `provider.error`, `unknown`, `output-limit`) |
| Stalled | High | siren | Assistant was working (`finish = "tool-calls"` or in-flight) but `time_updated` hasn't advanced for `--stall-minutes` |
| Idle | Normal | none | `finish = "stop"` and the last paragraph has no `?` (assistant completed its turn without asking anything) |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--idle-minutes` | 10 | Minutes before notifying that a completed session is idle |
| `--wait-minutes` | 2 | Minutes a session must be waiting before sending a notification |
| `--stall-minutes` | 10 | Minutes of no progress before considering a session stalled |
| `--interval` | 30 | Seconds between polling checks |
| `--max-age-hours` | 24 | Only monitor sessions updated within this many hours |

Example with custom settings:

```bash
python3 pushover_monitor.py \
    --idle-minutes 5 \
    --interval 60
```

## Stopping the Monitor

```bash
pkill -f pushover_monitor.py
```

If running in the foreground, `Ctrl+C` will stop it.

## Auto-Start (Optional)

Once your credentials are in the Keychain, add this to your `~/.zshrc` or `~/.bashrc`. No secrets are needed in the file:

```bash
# Start OpenCode Pushover Monitor if not running
if ! pgrep -f "pushover_monitor.py" > /dev/null; then
    /path/to/oc-pushover-monitor/start_monitor.sh > /dev/null 2>&1
fi
```

Verify it's running:

```bash
pgrep -fa pushover_monitor.py
```

## How It Works

1. Detects running OpenCode client processes via `ps` (ignoring the background `opencode serve` service and one-shot subcommands) and resolves their working directories with `lsof`
2. Lists recent top-level sessions from the OpenCode v2 server with `opencode api get /api/session` and keeps the most recent session in each active directory -- one session per running instance
3. Determines session state from API signals: the latest turn message (`assistant` `finish`/`error`, or the `idle` marker's `outcome`/`error`), running `question` tool calls, pending forms (`/api/form`) and permission requests (`/api/permission/request`), prose question detection (last paragraph contains `?`), and `time.updated` staleness for stall detection
4. Sends a Pushover notification when a session has been in a notifiable state long enough (e.g. waiting for `--wait-minutes`, idle for `--idle-minutes`)
5. Tracks per-session state and duration to avoid duplicate notifications and support re-notification after activity

If process detection fails (e.g. `lsof` not available), falls back to monitoring all sessions updated within `--max-age-hours`.

## Testing

Run the smoke tests to verify process detection, session discovery, and state logic:

```bash
./venv/bin/python3 test_monitor.py
```

The Pushover delivery test runs automatically when credentials are available (from env vars or the Keychain), and is skipped otherwise.

At least one `opencode` session must be running for the process detection and session discovery tests to pass.

## Troubleshooting

**Monitor not starting?**
- Check Python 3 is installed: `python3 --version`
- Verify credentials are stored: `security find-generic-password -s oc-pushover-monitor -a app-token`
- Verify OpenCode v2 is reachable: `opencode api get /api/info`

**Not receiving notifications?**
- Confirm the Pushover app is installed and logged in on your phone
- Test delivery directly:
  ```bash
  curl -s --form-string "token=$(security find-generic-password -s oc-pushover-monitor -a app-token -w)" \
       --form-string "user=$(security find-generic-password -s oc-pushover-monitor -a user-key -w)" \
       --form-string "message=Test from OpenCode monitor" \
       https://api.pushover.net/1/messages.json
  ```

**Duplicate notifications?**
- Check only one instance is running: `pgrep -f pushover_monitor.py`
