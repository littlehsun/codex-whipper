#!/usr/bin/env python3
"""Retry capacity-failed Codex turns through an existing local app-server."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import time
import uuid

from env_config import (ROOT, boolean_setting, get_setting, list_setting, local_path, read_env)

CAPACITY = "Selected model is at capacity. Please try a different model"
TURN_ID = re.compile(r"\bturn\.id=([0-9a-f-]{36})\b")
PROCESS_PID = re.compile(r"^pid:(\d+):")
LOG = logging.getLogger("codex-whipper")
LOG_TOTAL_BYTES = 1_000_000


class BoundedLogHandler(RotatingFileHandler):
    """Active file + one backup <= 1 MB, including UTF-8 and large errors."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Bound pre-existing files too, before any new record is emitted.
        for file in [path, path.with_name(path.name + ".1")]:
            if file.exists() and file.stat().st_size > LOG_TOTAL_BYTES // 2:
                with file.open("rb") as old:
                    old.seek(-LOG_TOTAL_BYTES // 2, os.SEEK_END)
                    tail = old.read().partition(b"\n")[2]
                file.write_bytes(tail)  # Discard the partial first record / UTF-8 sequence.
        super().__init__(path, maxBytes=LOG_TOTAL_BYTES // 2, backupCount=1, encoding="utf-8")

    def format(self, record):
        text = super().format(record)
        encoded = text.encode("utf-8")
        limit = 16_000
        if len(encoded) > limit:
            suffix = "... [truncated]"
            text = encoded[:limit - len(suffix)].decode("utf-8", errors="ignore") + suffix
        return text

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        # Standard RotatingFileHandler counts characters; count UTF-8 bytes.
        return self.stream.tell() + len((self.format(record) + "\n").encode("utf-8")) > self.maxBytes


def cwd_field(event):
    """Quote paths so spaces and line breaks remain one unambiguous log field."""
    return json.dumps(event.get("cwd") or "<unknown>", ensure_ascii=False)


def excluded_thread(thread):
    """Apply root-session scope to both transports."""
    source = thread.get("source")
    if thread.get("parentThreadId") or (isinstance(source, dict) and any(
            k in source for k in ["subAgent", "subagent", "internal"])):
        return "subagent_or_internal"
    if thread.get("ephemeral"):
        return "ephemeral"
    return None


class RpcError(RuntimeError):
    pass


class Rpc:
    """A bounded, synchronous JSON-RPC client on Codex's Unix WebSocket."""
    def __init__(self, socket: Path, timeout: float = 10):
        self.socket, self.timeout, self.ws, self.seq = socket, timeout, None, 0
        self.connection_context = None

    def close(self):
        if self.connection_context is not None:
            context = self.connection_context
            self.ws, self.connection_context = None, None
            context.__exit__(None, None, None)

    def connect(self):
        from websockets.sync.client import unix_connect
        context = unix_connect(
            path=str(self.socket), uri="ws://localhost", open_timeout=self.timeout,
            close_timeout=1, max_size=4 * 1024 * 1024,
        )
        self.ws = context.__enter__()
        self.connection_context = context
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_whipper", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.ws.send(json.dumps({"method": "initialized"}))
        except Exception:
            self.close()
            raise

    def call(self, method, params):
        if self.ws is None:
            self.connect()
        self.seq += 1
        request_id = self.seq
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"RPC timeout: {method}")
            reply = json.loads(self.ws.recv(timeout=remaining))
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                raise RpcError(f"{method}: {reply['error']}")
            return reply["result"]


def socket_owner_pids(socket: Path) -> set[int]:
    """Match the listening socket, never just a Codex process name."""
    socket_path = str(socket.resolve())
    inodes = set()
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        cols = line.split(maxsplit=7)  # Unix socket paths may contain spaces.
        if len(cols) >= 8 and cols[7] == socket_path and int(cols[3], 16) & 0x10000:
            inodes.add(f"socket:[{cols[6]}]")
    owners = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    if os.readlink(fd) in inodes:
                        owners.add(int(proc.name))
                        break
                except OSError:
                    pass
        except OSError:
            pass
    return owners


class LogStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        self.db.execute("PRAGMA query_only=ON")

    def close(self):
        self.db.close()

    def latest_id(self):
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]

    def initial_cursor(self, now, lookback):
        if lookback:
            first = self.db.execute("SELECT MIN(id) FROM logs WHERE ts >= ?", (now - lookback,)).fetchone()[0]
            if first is not None:
                return max(0, first - 1)
        return self.latest_id()

    def read(self, cursor, batch_size=50000, end_id=None):
        latest = self.latest_id()
        if latest < cursor:
            raise RuntimeError("Log database moved backwards; inspect/reset the watcher state")
        upper = min(latest, cursor + batch_size, latest if end_id is None else end_id)
        rows = self.db.execute(
            "SELECT id, ts, thread_id, process_uuid, feedback_log_body FROM logs "
            "WHERE id > ? AND id <= ? AND target = 'codex_core::session::turn' "
            "AND instr(feedback_log_body, ?) > 0 ORDER BY id",
            (cursor, upper, "Turn error: " + CAPACITY),
        ).fetchall()
        events = []
        for log_id, ts, thread_id, process_uuid, body in rows:
            match = TURN_ID.search(body)
            if thread_id and process_uuid and match:
                events.append({"log_id": log_id, "ts": ts, "thread_id": thread_id,
                               "process_uuid": process_uuid, "turn_id": match.group(1)})
        return upper, events


class State:
    def __init__(self, path: Path, db: Path, socket: Path, cursor: int):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = path.with_suffix(path.suffix + ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.lock.close()
            raise RuntimeError("Another watcher is using this state file") from e
        identity = {"database": str(db.resolve()), "socket": str(socket.resolve())}
        try:
            if path.exists():
                self.data = json.loads(path.read_text())
                if self.data.get("version") != 1 or self.data.get("identity") != identity:
                    raise RuntimeError("State version/database/socket mismatch; use a separate state file")
            else:
                self.data = {"version": 1, "identity": identity, "cursor": cursor,
                             "pending": {}, "retries": {}}
                self.save()
        except Exception:
            self.lock.close()
            raise

    def save(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as f:
            os.chmod(tmp, 0o600)
            json.dump(self.data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        directory = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def close(self):
        self.lock.close()


class HerdrFallback:
    """Explicitly allowlisted local CLI sessions; never use a focused pane."""
    def __init__(self, threads, binary):
        self.threads, self.binary, self.targets = set(threads), binary, {}

    def run(self, env, *args, text=False):
        result = subprocess.run([self.binary, *args], env=env, capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise RuntimeError(f"Herdr {args[0]} failed: {result.stderr[:1500]}")
        return result.stdout if text else json.loads(result.stdout)["result"]

    def target(self, event):
        match = PROCESS_PID.match(event["process_uuid"])
        if not match or event["thread_id"] not in self.threads:
            raise RuntimeError("Herdr target isn't explicitly authorized")
        pid = int(match.group(1))
        proc = Path(f"/proc/{pid}")
        if proc.stat().st_uid != os.getuid():
            raise RuntimeError("Herdr process belongs to another user")
        inherited = {}
        for entry in (proc / "environ").read_bytes().split(b"\0"):
            key, sep, value = entry.partition(b"=")
            if sep and key in {b"HERDR_ENV", b"HERDR_PANE_ID", b"HERDR_SOCKET_PATH"}:
                inherited[key.decode()] = value.decode()
        if inherited.get("HERDR_ENV") != "1" or not inherited.get("HERDR_PANE_ID") or not inherited.get("HERDR_SOCKET_PATH"):
            raise RuntimeError("Target process isn't in an identifiable Herdr pane")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["HERDR_SOCKET_PATH"] = inherited["HERDR_SOCKET_PATH"]
        return env, inherited["HERDR_PANE_ID"], pid

    def identity(self, env, pane, pid, tid):
        process = self.run(env, "pane", "process-info", "--pane", pane)["process_info"]
        if process.get("foreground_process_group_id") != pid:
            return "foreground_process_changed"
        agent = self.run(env, "agent", "get", pane)["agent"]
        if agent.get("agent") != "codex" or (agent.get("agent_session") or {}).get("value") != tid:
            return "herdr_session_changed"
        status = agent.get("agent_status")
        if status == "working":
            return "wait"
        if status not in {"idle", "done"}:
            return "herdr_blocked_or_unknown"
        return "ready"

    def latest(self, rpc, tid):
        turns = rpc.call("thread/turns/list", {"threadId": tid, "limit": 1,
                         "sortDirection": "desc", "itemsView": "notLoaded"})["data"]
        return turns[0] if turns else None

    def screen(self, env, pane):
        text = self.run(env, "agent", "read", pane, "--source", "detection", "--lines", "60", text=True)
        prompts = [line.strip()[1:].strip() for line in text.splitlines() if line.strip().startswith("›")]
        if not prompts or prompts[-1] not in {"", "Ask Codex to do anything"}:
            return "herdr_input_not_empty"
        return "ready" if CAPACITY in text else "capacity_not_on_screen"

    def inspect(self, event, rpc):
        tid = event["thread_id"]
        env, pane, pid = self.target(event)
        decision = self.identity(env, pane, pid, tid)
        if decision != "ready":
            return decision
        thread = rpc.call("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
        reason = excluded_thread(thread)
        if reason:
            return reason
        if isinstance(thread.get("cwd"), str) and thread["cwd"]:
            event["cwd"] = thread["cwd"]
        turn = self.latest(rpc, tid)
        if not turn or turn["id"] != event["turn_id"]:
            return "newer_turn_or_no_turn"
        if turn.get("status") != "failed" or CAPACITY not in (turn.get("error") or {}).get("message", ""):
            return "latest_turn_not_capacity_failed"
        if rpc.call("thread/queue/list", {"threadId": tid, "limit": 1})["data"]:
            return "user_input_queued"
        decision = self.screen(env, pane)
        if decision == "ready":
            self.targets[tid] = env, pane, pid
        return decision

    def send(self, event, message, rpc):
        tid = event["thread_id"]
        if self.inspect(event, rpc) != "ready":
            raise RuntimeError("Herdr session changed before submission")
        env, pane, _ = self.targets[tid]
        self.run(env, "agent", "prompt", pane, message)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            turn = self.latest(rpc, tid)
            if turn and turn["id"] != event["turn_id"]:
                LOG.info("herdr_turn_started thread=%s cwd=%s pane=%s turn=%s", tid, cwd_field(event), pane, turn["id"])
                return {"turn": turn}
            time.sleep(0.2)
        raise TimeoutError("Herdr submitted input, but a new turn wasn't observed")


class Watcher:
    def __init__(self, args, store, state, rpc, owners=socket_owner_pids, herdr=None):
        self.args, self.store, self.state, self.rpc, self.owners = args, store, state, rpc, owners
        self.herdr = herdr

    def delay(self, attempts):
        return min(self.args.max_delay, self.args.delay * 2 ** min(attempts, 20))

    def session_cwd(self, tid):
        # The daemon's /proc/PID/cwd belongs to the server, not the session.
        # Metadata enrichment must never prevent capacity handling.
        try:
            thread = self.rpc.call("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
            cwd = thread.get("cwd")
            return cwd if isinstance(cwd, str) and cwd else None
        except Exception:
            return None

    def ingest(self, now):
        d = self.state.data
        snapshot = self.store.latest_id()
        cursor = d["cursor"]
        events = []
        folders = {}
        # Catch up to a fixed snapshot, including all batches in --once mode.
        while cursor < snapshot:
            cursor, batch = self.store.read(cursor, end_id=snapshot)
            events.extend(batch)
        if cursor > snapshot:
            raise RuntimeError("Log database moved backwards; inspect/reset the watcher state")
        for event in events:
            tid = event["thread_id"]
            if self.args.thread and tid not in self.args.thread:
                continue
            if now - event["ts"] > self.args.max_age:
                continue
            pending = d["pending"].get(tid)
            if pending and pending["turn_id"] == event["turn_id"]:
                continue
            previous = d["retries"].get(tid, {})
            if previous.get("failure_turn") == event["turn_id"]:
                continue
            if tid not in folders:
                folders[tid] = self.session_cwd(tid)
            event["cwd"] = folders[tid]
            if previous.get("outcome") in {"dispatching", "uncertain_or_rejected"}:
                LOG.warning("unresolved_dispatch thread=%s cwd=%s; inspect before resetting state", tid, cwd_field(event))
                continue
            # Only failures of our own retry turn continue the retry count.
            attempts = previous.get("attempts", 0) if previous.get("expected_turn") == event["turn_id"] else 0
            if attempts >= self.args.max_retries:
                d["pending"].pop(tid, None)
                LOG.warning("retry_limit thread=%s cwd=%s turn=%s attempts=%s", tid, cwd_field(event), event["turn_id"], attempts)
                continue
            event.update(attempts=attempts, due=max(now, event["ts"] + self.delay(attempts)))
            d["pending"][tid] = event
            LOG.info("capacity thread=%s cwd=%s turn=%s retry_in=%.0fs", tid, cwd_field(event), event["turn_id"], event["due"] - now)
        d["cursor"] = cursor
        self.state.save()

    def discard(self, tid, reason):
        event = self.state.data["pending"].pop(tid, {})
        self.state.save()
        LOG.info("skip thread=%s cwd=%s reason=%s", tid, cwd_field(event), reason)

    def inspect(self, event):
        tid = event["thread_id"]
        thread = self.rpc.call("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
        if isinstance(thread.get("cwd"), str) and thread["cwd"]:
            event["cwd"] = thread["cwd"]
        reason = excluded_thread(thread)
        if reason:
            return reason
        if thread.get("status", {}).get("type") == "notLoaded":
            return "not_loaded"
        if thread.get("canAcceptDirectInput") is not True:
            return "direct_input_unavailable"
        turns = self.rpc.call("thread/turns/list", {
            "threadId": tid, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded",
        })["data"]
        if not turns or turns[0]["id"] != event["turn_id"]:
            return "newer_turn_or_no_turn"
        turn = turns[0]
        if turn.get("status") == "inProgress" or thread.get("status", {}).get("type") == "active":
            return "wait"
        if turn.get("status") != "failed" or CAPACITY not in (turn.get("error") or {}).get("message", ""):
            return "latest_turn_not_capacity_failed"
        queued = self.rpc.call("thread/queue/list", {"threadId": tid, "limit": 1})["data"]
        if queued:
            return "user_input_queued"
        # Recheck runtime state immediately before dispatch; do not intentionally steer.
        status = self.rpc.call("thread/read", {"threadId": tid, "includeTurns": False})["thread"].get("status", {}).get("type")
        # A failed turn can leave a loaded thread in systemError. Its latest
        # failure and direct-input capability were checked above.
        return "ready" if status in {"idle", "systemError"} else "wait"

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.ingest(now)
        due = [e for e in self.state.data["pending"].values() if e["due"] <= now]
        if not due:
            return
        owners = self.owners(self.args.socket)
        for event in due:
            tid = event["thread_id"]
            # State survives restarts; current authorization and limits win.
            if self.args.thread and tid not in self.args.thread:
                self.discard(tid, "outside_current_scope")
                continue
            if event["attempts"] >= self.args.max_retries:
                self.discard(tid, "retry_limit")
                continue
            if now - event["ts"] > self.args.max_age:
                self.discard(tid, "expired")
                continue
            match = PROCESS_PID.match(event["process_uuid"])
            if not owners:
                LOG.warning("socket_owner_unavailable; waiting")
                return
            use_herdr = bool(match and int(match.group(1)) not in owners and self.herdr and tid in self.args.herdr_thread)
            if (not match or int(match.group(1)) not in owners) and not use_herdr:
                self.discard(tid, "different_app_server_process")
                continue
            try:
                decision = self.herdr.inspect(event, self.rpc) if use_herdr else self.inspect(event)
            except Exception as e:
                self.rpc.close()
                LOG.warning("inspect_failed thread=%s cwd=%s error=%s", tid, cwd_field(event), e)
                event["due"] = now + max(self.args.poll, 10)
                self.state.save()
                continue
            if decision == "wait":
                event["due"] = now + self.args.poll
                self.state.save()
                continue
            if decision != "ready":
                self.discard(tid, decision)
                continue
            if self.args.dry_run:
                LOG.info("dry_run would_send thread=%s cwd=%s failed_turn=%s message=%r", tid, cwd_field(event), event["turn_id"], self.args.message)
                self.discard(tid, "dry_run_checked")
                continue
            # Persist consumption BEFORE sending. A timeout may mean it was accepted;
            # never replay this failure automatically after a crash or uncertain reply.
            self.state.data["pending"].pop(tid, None)
            retry = {"attempts": event["attempts"] + 1, "expected_turn": None,
                     "failure_turn": event["turn_id"], "sent_at": now, "outcome": "dispatching",
                     "cwd": event.get("cwd"),
                     "transport": "herdr" if use_herdr else "app-server"}
            self.state.data["retries"][tid] = retry
            self.state.save()
            try:
                if use_herdr:
                    result = self.herdr.send(event, self.args.message, self.rpc)
                else:
                    result = self.rpc.call("turn/start", {
                        "threadId": tid,
                        "input": [{"type": "text", "text": self.args.message}],
                        "clientUserMessageId": str(uuid.uuid4()),
                    })
                retry.update(expected_turn=result["turn"]["id"], outcome="accepted")
                LOG.info("sent thread=%s cwd=%s turn=%s attempt=%s", tid, cwd_field(event), retry["expected_turn"], retry["attempts"])
            except Exception as e:
                self.rpc.close()
                retry["outcome"] = "uncertain_or_rejected"
                LOG.error("dispatch_failed thread=%s cwd=%s error=%s; this failure will not be replayed", tid, cwd_field(event), e)
            self.state.save()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    preload = argparse.ArgumentParser(add_help=False)
    preload.add_argument("--env-file", type=Path)
    known, _ = preload.parse_known_args(argv)
    env_file = (known.env_file or ROOT / ".env").expanduser()
    try:
        settings = read_env(env_file, required=known.env_file is not None)
        home = local_path(get_setting(settings, "WHIPPER_CODEX_HOME", os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
        def value(name, default=None):
            return get_setting(settings, "WHIPPER_" + name, default)
        def number(name, default, convert=float):
            try:
                return convert(value(name, str(default)))
            except ValueError as e:
                raise ValueError(f"WHIPPER_{name} must be a number") from e
        boolean_defaults = {key: boolean_setting(settings, "WHIPPER_" + key) for key in
                            ["DRY_RUN", "ONCE", "RECONCILE_ON_START"]}
        numeric_defaults = {key: number(key, default, convert) for key, default, convert in [
            ("DELAY", 60, float), ("MAX_DELAY", 300, float), ("MAX_RETRIES", 5, int),
            ("MAX_AGE", 1800, float), ("POLL", 5, float), ("LOOKBACK", 0, float),
        ]}
    except (OSError, ValueError) as e:
        parser.error(str(e))
    parser.add_argument("--env-file", type=Path, default=env_file, help="Private configuration file; defaults to .env next to the script")
    parser.add_argument("--database", type=Path, default=Path(value("DATABASE", str(home / "logs_2.sqlite"))))
    parser.add_argument("--socket", type=Path, default=Path(value("SOCKET", str(home / "app-server-control/app-server-control.sock"))))
    parser.add_argument("--state", type=Path, default=Path(value("STATE")) if value("STATE") else None, help="Persistent state; dry-run always uses a separate sibling")
    parser.add_argument("--thread", action="append", help="Only watch this UUID (repeatable); overrides WHIPPER_THREADS")
    parser.add_argument("--herdr-thread", action="append", help="Explicitly permit Herdr input for this UUID (repeatable); overrides WHIPPER_HERDR_THREADS")
    parser.add_argument("--herdr-bin", default=value("HERDR_BIN", str(Path.home() / ".local/bin/herdr")))
    parser.add_argument("--log-file", type=Path, default=Path(value("LOG_FILE")) if value("LOG_FILE") else None, help="Rotating log; active file + backup <= 1 MB")
    parser.add_argument("--message", default=value("MESSAGE", "keep doing"))
    parser.add_argument("--delay", type=float, default=numeric_defaults["DELAY"], help="Initial retry delay in seconds")
    parser.add_argument("--max-delay", type=float, default=numeric_defaults["MAX_DELAY"])
    parser.add_argument("--max-retries", type=int, default=numeric_defaults["MAX_RETRIES"], help="Max retries per capacity failure chain")
    parser.add_argument("--max-age", type=float, default=numeric_defaults["MAX_AGE"], help="Discard failures older than this many seconds")
    parser.add_argument("--poll", type=float, default=numeric_defaults["POLL"])
    parser.add_argument("--lookback", type=float, default=numeric_defaults["LOOKBACK"], help="Seconds of history to inspect with a NEW state file")
    parser.add_argument("--reconcile-on-start", action=argparse.BooleanOptionalAction, default=boolean_defaults["RECONCILE_ON_START"], help="Also rescan lookback history with existing state")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=boolean_defaults["DRY_RUN"], help="Inspect without sending messages")
    parser.add_argument("--once", action=argparse.BooleanOptionalAction, default=boolean_defaults["ONCE"], help="Process due events once, then exit")
    parser.add_argument("--doctor", action="store_true", help="Read-only database/socket/protocol check")
    args = parser.parse_args(argv)
    args.thread = list_setting(settings, "WHIPPER_THREADS") if args.thread is None else args.thread
    args.herdr_thread = list_setting(settings, "WHIPPER_HERDR_THREADS") if args.herdr_thread is None else args.herdr_thread
    if not all(math.isfinite(v) for v in [args.poll, args.delay, args.max_delay, args.max_age, args.lookback]) or args.poll <= 0 or args.delay <= 0 or args.max_delay < args.delay or args.max_retries <= 0 or args.max_age <= 0 or args.lookback < 0 or not args.message.strip():
        parser.error("Invalid delays/retry count/age/lookback/message")
    if args.reconcile_on_start and args.lookback <= 0:
        parser.error("Reconciliation requires a positive lookback")
    args.database = local_path(args.database)
    args.socket = local_path(args.socket)
    if args.state is None:
        args.state = Path.home() / ".local/state/codex-whipper" / ("dry-run.json" if args.dry_run else "state.json")
    else:
        args.state = local_path(args.state)
        if args.dry_run:
            args.state = args.state.with_name(args.state.stem + ".dry-run" + args.state.suffix)
    if args.log_file:
        args.log_file = local_path(args.log_file)
    if "/" in args.herdr_bin or args.herdr_bin.startswith("~"):
        args.herdr_bin = str(local_path(args.herdr_bin))
    return args


def main(argv=None):
    args = parse_args(argv)
    handlers = [BoundedLogHandler(args.log_file)] if args.log_file else [logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    store, rpc, state = None, None, None
    try:
        store = LogStore(args.database)
        rpc = Rpc(args.socket)
        if args.doctor:
            owners = socket_owner_pids(args.socket)
            rpc.connect()
            loaded = rpc.call("thread/loaded/list", {})
            print(json.dumps({"database": str(args.database), "latest_log_id": store.latest_id(),
                              "socket": str(args.socket), "owner_pids": sorted(owners),
                              "loaded_threads": len(loaded["data"])}, indent=2))
            return 0 if owners else 1
        state = State(args.state, args.database, args.socket, store.initial_cursor(time.time(), args.lookback))
        if args.reconcile_on_start:
            state.data["cursor"] = min(state.data["cursor"], store.initial_cursor(time.time(), args.lookback))
            state.save()
        watcher = Watcher(args, store, state, rpc,
                          herdr=HerdrFallback(args.herdr_thread, args.herdr_bin) if args.herdr_thread else None)
        LOG.info("watching dry_run=%s cursor=%s state=%s", args.dry_run, state.data["cursor"], args.state)
        running = True
        def stop(signum, frame):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while running:
            try:
                watcher.tick()
            except (sqlite3.Error, OSError) as e:
                LOG.error("scan_failed error=%s", e)
                if args.once:
                    return 1
            if args.once:
                break
            # Bounded sleeps so SIGTERM exits promptly.
            deadline = time.monotonic() + args.poll
            while running and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        return 0
    except (Exception, KeyboardInterrupt) as e:
        LOG.error("stopped error=%s", e)
        return 1
    finally:
        if rpc:
            rpc.close()
        if store:
            store.close()
        if state:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
