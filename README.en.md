# codex-whipper

English · [繁體中文](README.md)

Watch local Codex sessions for model-capacity failures and submit `keep doing` so interrupted work can continue.

The watcher only handles actual turns that fail with `Selected model is at capacity. Please try a different model`. It checks session state again before submitting, avoids replaying consumed failures, and keeps application logs within a 1 MB budget. It does not change models or answer approval dialogs.

## Requirements

- Linux and Python 3.10 or newer.
- An authenticated, working Codex CLI/shared app-server.
- The server must support Unix WebSocket transport and `thread/read`, `thread/turns/list`, `thread/queue/list`, and `turn/start`. Turn pagination is experimental; compatibility may change across Codex versions.
- Optional: Herdr for explicitly selected local standalone (`--no-daemon`) CLI sessions. Its CLI must support the `agent get/read/prompt` and `pane process-info` commands used here.

The only Python dependency is `websockets`. No separate API key is needed: the watcher uses the existing authenticated Codex app-server.

## Quick start

```bash
git clone https://github.com/littlehsun/codex-whipper.git
cd codex-whipper
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env`. The example starts with `WHIPPER_DRY_RUN=true`, so you can inspect behavior without submitting messages.

```bash
.venv/bin/python whipper.py --doctor
.venv/bin/python whipper.py --once
```

`--doctor` checks the database, socket, and loaded threads. It neither starts a daemon nor resumes work. The second command scans once using `.env` and logs eligible due events as `dry_run would_send` or `skip`.

After checking the paths and scope, set `WHIPPER_DRY_RUN=false` and run:

```bash
.venv/bin/python whipper.py
```

Stop with `Ctrl+C`. Automatic retries continue the existing session's work; use `WHIPPER_THREADS` to limit monitoring to sessions you want the watcher to continue.

## Private `.env` configuration

Keep personal paths, session UUIDs, the retry message, and other settings in `.env`. Git ignores actual environment files; the repository includes only the sanitized `.env.example`.

Precedence: **CLI options > process environment > `.env` > built-in defaults**.

- By default, the file is `.env` beside the script, regardless of your shell's current directory.
- Use `--env-file /path/to/private.env` to select another file.
- Relative paths are based on the project directory; `~` is supported.
- Quote values containing spaces. Only documented keys are accepted. The parser does not execute shell commands, expand `$VARIABLE`, or support multiline values.
- Thread lists are comma-separated session UUIDs, not titles or pane IDs.

| Variable | Purpose / built-in default |
| --- | --- |
| `WHIPPER_CODEX_HOME` | Codex home; otherwise `CODEX_HOME` or `~/.codex`. |
| `WHIPPER_DATABASE` | Blank derives `logs_2.sqlite` from Codex home. |
| `WHIPPER_SOCKET` | Blank derives `app-server-control/app-server-control.sock` from Codex home. |
| `WHIPPER_STATE` | Blank uses `~/.local/state/codex-whipper/state.json`; dry-run gets separate state. |
| `WHIPPER_THREADS` | Blank monitors eligible shared-server root sessions; UUIDs restrict the scope, including Herdr targets. |
| `WHIPPER_HERDR_THREADS` | Explicit allowlist for Herdr-controlled standalone CLI sessions. Blank disables the fallback. |
| `WHIPPER_HERDR_BIN` | Herdr executable; default `~/.local/bin/herdr`, or set `herdr` to use PATH. |
| `WHIPPER_MESSAGE` | Retry prompt; default `keep doing`. |
| `WHIPPER_DELAY` | Initial delay in seconds; default `60`. |
| `WHIPPER_MAX_DELAY` | Maximum delay; default `300`, at least the initial delay. |
| `WHIPPER_MAX_RETRIES` | Maximum retries per continuous capacity failure chain; default `5`. |
| `WHIPPER_MAX_AGE` | Failure lifetime in seconds; default `1800`. |
| `WHIPPER_POLL` | Scan interval in seconds; default `5`. |
| `WHIPPER_LOOKBACK` | Historical window for new state; built-in `0`, example `1800`. |
| `WHIPPER_RECONCILE_ON_START` | Rescan the lookback window on every startup; built-in false, example true. Requires positive lookback. |
| `WHIPPER_DRY_RUN` | Inspect without submitting; built-in false, example true. |
| `WHIPPER_ONCE` | Process due events once and exit; default false. |
| `WHIPPER_LOG_FILE` | Blank logs to stderr; example `logs/whipper.log`. |
| `WHIPPER_INSTALL_DIR` | Service project location; blank uses this checkout. |
| `WHIPPER_PYTHON` | Service interpreter; blank uses the project's `.venv/bin/python`. |

Booleans accept `true/false`, `1/0`, `yes/no`, or `on/off`. Override enabled file settings with `--no-dry-run`, `--no-once`, or `--no-reconcile-on-start`. See all options:

```bash
.venv/bin/python whipper.py --help
```

## Session scope and Herdr

The shared-server path handles loaded root sessions that accept direct input. It skips subagents, internal/ephemeral sessions, another server process, changed turns, or other errors. It does not intentionally steer an active session.

For a local standalone CLI without the shared socket, add its UUID to `WHIPPER_HERDR_THREADS` in `.env`. This explicitly permits submitting retry prompts to that Herdr terminal, including from a background service.

In your own Herdr environment, inspect an explicit pane:

```bash
herdr agent get <pane-id>
```

Use the returned `agent.agent_session.value` as the UUID. The watcher obtains the pane/socket from the original process, then checks the foreground PID, Codex session UUID, idle/done state, root-session scope, latest capacity failure, absence of queued input, and empty composer. Blocked, unknown, replaced sessions, or pending typed input are not prompted. It does not change focus, restart the CLI, or answer approval dialogs.

A successful `agent prompt` submission is not sufficient: a new turn ID must be observed before the retry is recorded as accepted. Other standalone app-servers and remote hosts are not automatically controlled through this local fallback.

## Retries and persistent state

1. Read new Codex SQLite records without modifying the database. Only `codex_core::session::turn` records containing an actual `Turn error` qualify; mentioning the error in a conversation does not.
2. Extract process/thread/turn IDs and verify the control endpoint and latest turn.
3. Confirm the latest turn still failed for capacity, the session is idle or in the resulting systemError state, and no user input is queued.
4. Persist consumption before submitting, then read back the new turn ID.

Default delays are **60, 120, 240, 300, and 300 seconds**, at most five retries. Only a capacity failure of the watcher's own next turn continues the retry count. A new user turn starts a separate chain. Submission does not guarantee capacity is available; another capacity failure returns to this flow.

State stores the cursor, pending events, and retry chains using atomic rename, fsync, and a file lock. Do not run multiple live watchers with different state files against the same session.

After a restart, existing pending events also obey the current thread scope and retry limit. Narrowing scope or lowering the limit does not preserve old permission to submit. Keep any custom state path outside the repository or in an ignored directory.

Dry-run never shares live state. Its default file is `dry-run.json`; explicitly configuring `live.json` selects a sibling `live.dry-run.json` during dry-run. `--once` does not wait for events whose delay has not elapsed: those remain in that state's pending list for a later run.

A timeout, disconnect, or rejected dispatch marks the thread `uncertain_or_rejected` and prevents further automatic submission. `dispatching` means the process may have stopped during submission. **Do not blindly resend**. Check the session, stop the watcher, back up state, and confirm the outcome before removing that thread's `retries` entry so future events can be handled.

## Logs

```bash
tail -F logs/whipper.log
```

`capacity` means a detected failure, `sent` means submitted with a new turn observed, and `skip` includes the reason. Example format:

```text
INFO capacity thread=<session-uuid> cwd="/work/project" turn=<turn-uuid> retry_in=60s
INFO sent thread=<session-uuid> cwd="/work/project" turn=<new-turn-uuid> attempt=1
```

`cwd` comes from session metadata, not the shared daemon's process directory. Missing metadata is shown as `<unknown>` and does not block retries.

The active file and one `.1` backup are each limited to **500,000 bytes**, for a combined **1,000,000-byte (1 MB)** budget. Rotation counts UTF-8 bytes; oversized records are truncated. On startup, oversized existing files are trimmed by removing the oldest records. Logs and state can contain paths, messages, and session IDs, and should not be uploaded or committed.

## Optional systemd service

Create `.venv` and configure `.env` (the service requires `WHIPPER_LOG_FILE`), then generate a private user service:

```bash
.venv/bin/python install_service.py
systemctl --user daemon-reload
systemctl --user start codex-whipper.service
systemctl --user status codex-whipper.service
```

The installer only writes configuration; it does not start or enable the service. The repository's `codex-whipper.service` is a template: **do not copy it directly**. Installation paths are derived from `.env` and written outside the repository.

Enable automatic startup at login:

```bash
systemctl --user enable codex-whipper.service
```

Stop and disable automatic startup:

```bash
systemctl --user disable --now codex-whipper.service
```

The installer rejects existing `.service.d/*.conf` drop-ins: review and remove or relocate them before installing, so old arguments cannot override `.env`. After changing `.env`, restart the service. Changing the installation location or interpreter requires regenerating the service and running daemon-reload. Application logs go to `WHIPPER_LOG_FILE`; use journal for service lifecycle and startup failures.

## Troubleshooting and limitations

- **No retry:** check `skip` reasons, UUID allowlists, latest turn, process/socket, failure age, and retry limit. Older failures need lookback/reconciliation; standalone CLI sessions need explicit Herdr opt-in.
- **Connection/method errors:** run `--doctor`. CLI and daemon versions may differ. The watcher does not restart or upgrade Codex.
- **State mismatch or IDs moving backwards:** stop the watcher and verify the database/socket. Back up existing state before creating state for a new target.
- **Configuration not taking effect:** check CLI/process-environment overrides and old systemd drop-ins overriding ExecStart.
- **Repeated capacity failures:** retries stop at the limit. This tool cannot increase or guarantee model capacity.
- State checks and submission are separate operations. Concurrent manual input can still race; only monitor sessions you intend to continue automatically.
- `/proc` and `fcntl` currently restrict this project to Linux. macOS/Windows, remote hosts, and subagent recovery are not implemented.

## Development and validation

```bash
.venv/bin/python -m unittest -v
```

Tests use synthetic UUIDs and temporary databases/sockets, without a real account. They cover error filtering, restart deduplication, backoff/limits, active/new turns and queued input, Herdr identity/approval/composer guards, a real Unix WebSocket mock server, configuration precedence/dry-run isolation, service path quoting, and UTF-8/oversized-log byte limits.

## References

- [OpenAI: Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Herdr](https://herdr.dev/)
