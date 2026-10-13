import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import whipper

TID = "11111111-1111-7111-8111-111111111111"
TURN = "22222222-2222-7222-8222-222222222222"
NEXT = "33333333-3333-7333-8333-333333333333"
NOW = 1000


class BoundedLogTests(unittest.TestCase):
    def test_rotation_bounds_total_bytes_for_unicode_and_huge_errors(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "logs/whipper.log"
            handler = whipper.BoundedLogHandler(path)
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            try:
                # The total budget is measured in bytes, not Chinese characters.
                for _ in range(150):
                    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "目錄" * 2500, (), None))
                handler.emit(logging.LogRecord("test", logging.ERROR, "", 0, "錯誤" * 1_000_000, (), None))
                sizes = [p.stat().st_size for p in path.parent.iterdir()]
                self.assertEqual(len(sizes), 2)
                self.assertLessEqual(sum(sizes), 1_000_000)
                self.assertTrue(all(size <= 500_000 for size in sizes))
                self.assertIn("[truncated]", path.read_text())
                for p in path.parent.iterdir():
                    p.read_text(encoding="utf-8")
            finally:
                handler.close()


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.thread = {"source": "cli", "cwd": "/tmp/example project", "status": {"type": "idle"},
                       "canAcceptDirectInput": True, "parentThreadId": None}
        self.turn = {"id": TURN, "status": "failed", "error": {"message": whipper.CAPACITY}}
        self.queued = []
        self.fail_send = False

    def close(self):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": self.thread}
        if method == "thread/turns/list":
            return {"data": [self.turn]}
        if method == "thread/queue/list":
            return {"data": self.queued}
        if method == "turn/start":
            if self.fail_send:
                raise TimeoutError("accepted? unknown")
            return {"turn": {"id": NEXT if len(self.sends) == 1 else "44444444-4444-7444-8444-444444444444"}}
        raise AssertionError(method)

    @property
    def sends(self):
        return [params for method, params in self.calls if method == "turn/start"]


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "logs.sqlite"
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, target TEXT, thread_id TEXT, process_uuid TEXT, feedback_log_body TEXT)")
            c.execute("CREATE INDEX idx_ts ON logs(ts)")
        env_file = self.root / ".env"
        env_file.write_text("# Isolated test configuration\n")
        self.args = whipper.parse_args([
            "--env-file", str(env_file),
            "--database", str(self.db), "--socket", str(self.root / "server.sock"),
            "--state", str(self.root / "state.json"), "--delay", "60",
        ])
        self.store = whipper.LogStore(self.db)
        self.state = whipper.State(self.args.state, self.db, self.args.socket, 0)
        self.rpc = FakeRpc()
        self.watcher = whipper.Watcher(self.args, self.store, self.state, self.rpc, owners=lambda _: {os.getpid()})

    def tearDown(self):
        self.state.close()
        self.store.close()
        self.tmp.cleanup()

    def add_error(self, turn=TURN, ts=NOW, pid=None, body=None, target="codex_core::session::turn"):
        if pid is None:
            pid = os.getpid()
        if body is None:
            body = f'turn{{thread.id={TID} turn.id={turn}}}: Turn error: {whipper.CAPACITY}.'
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO logs(ts,target,thread_id,process_uuid,feedback_log_body) VALUES(?,?,?,?,?)",
                      (ts, target, TID, f"pid:{pid}:process-id", body))

    def test_exact_error_filter_ignores_prompts_and_other_targets(self):
        self.add_error(body=f'User prompt: {whipper.CAPACITY}')
        self.add_error(target="codex_core::session::handlers")
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.assertEqual(len(self.rpc.sends), 1)
        self.assertEqual(self.rpc.sends[0]["input"], [{"type": "text", "text": "keep doing"}])

    def test_delay_and_restart_do_not_duplicate(self):
        self.add_error()
        self.watcher.tick(NOW)
        self.assertEqual(self.rpc.sends, [])
        self.state.close()
        self.state = whipper.State(self.args.state, self.db, self.args.socket, 999)
        self.watcher.state = self.state
        self.watcher.tick(NOW + 60)
        self.watcher.tick(NOW + 61)
        self.add_error(ts=NOW + 61)  # duplicate logging of consumed failure
        self.watcher.tick(NOW + 121)
        self.assertEqual(len(self.rpc.sends), 1)

    def test_newer_turn_skips(self):
        self.add_error()
        self.rpc.turn["id"] = NEXT
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])
        self.assertFalse(self.state.data["pending"])

    def test_busy_waits_without_steering(self):
        self.add_error()
        self.rpc.thread["status"]["type"] = "active"
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])
        self.assertIn(TID, self.state.data["pending"])

    def test_subagent_and_other_process_skip(self):
        self.add_error(pid=os.getpid() + 100000)
        self.watcher.tick(NOW + 60)
        self.assertTrue(all(method == "thread/read" for method, _ in self.rpc.calls))
        self.add_error(turn=NEXT, ts=NOW + 61)
        self.rpc.thread["parentThreadId"] = TID
        self.watcher.tick(NOW + 121)
        self.assertEqual(self.rpc.sends, [])

    def test_queued_user_input_skips(self):
        self.add_error()
        self.rpc.queued = [{"id": "user-message"}]
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])

    def test_failure_must_still_be_capacity_failure(self):
        self.add_error()
        self.rpc.turn["error"]["message"] = "Usage limit exceeded"
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])

    def test_capacity_failed_system_error_thread_can_retry(self):
        self.add_error()
        self.rpc.thread["status"]["type"] = "systemError"
        self.watcher.tick(NOW + 60)
        self.assertEqual(len(self.rpc.sends), 1)

    def test_dry_run_does_not_send_or_count_retry(self):
        self.add_error()
        self.args.dry_run = True
        with self.assertLogs(whipper.LOG, level="INFO") as logs:
            self.watcher.tick(NOW + 60)
        self.assertIn("would_send", " ".join(logs.output))
        self.assertEqual(self.rpc.sends, [])
        self.assertFalse(self.state.data["retries"])

    def test_backoff_and_retry_limit(self):
        self.args.max_retries = 2
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.add_error(turn=NEXT, ts=NOW + 61)
        self.rpc.turn["id"] = NEXT
        self.watcher.tick(NOW + 121)
        self.assertEqual(len(self.rpc.sends), 1)
        self.watcher.tick(NOW + 181)
        self.assertEqual(len(self.rpc.sends), 2)
        last = "44444444-4444-7444-8444-444444444444"
        self.add_error(turn=last, ts=NOW + 182)
        self.rpc.turn["id"] = last
        with self.assertLogs(whipper.LOG, level="WARNING") as logs:
            self.watcher.tick(NOW + 500)
        self.assertIn("retry_limit", " ".join(logs.output))
        self.assertEqual(len(self.rpc.sends), 2)
        self.assertFalse(self.state.data["pending"])
        self.assertEqual(self.watcher.delay(10), 300)

    def test_uncertain_dispatch_blocks_replay_and_further_automatic_input(self):
        self.rpc.fail_send = True
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.add_error(turn=NEXT, ts=NOW + 61)
        self.rpc.turn["id"] = NEXT
        self.watcher.tick(NOW + 121)
        self.assertEqual(len(self.rpc.sends), 1)
        self.assertEqual(self.state.data["retries"][TID]["outcome"], "uncertain_or_rejected")
        self.state.close()
        self.state = whipper.State(self.args.state, self.db, self.args.socket, 0)
        self.watcher.state = self.state
        self.watcher.tick(NOW + 200)
        self.assertEqual(len(self.rpc.sends), 1)

    def test_old_errors_expire_and_allowlist_filters(self):
        self.add_error(ts=NOW - 2000)
        self.watcher.tick(NOW)
        self.assertFalse(self.state.data["pending"])
        self.add_error()
        self.args.thread = ["another-thread"]
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])

    def test_new_state_skips_history_unless_lookback_requested(self):
        self.add_error()
        self.assertEqual(self.store.initial_cursor(NOW + 1, 0), 1)
        self.assertEqual(self.store.initial_cursor(NOW + 1, 10), 0)

    def test_catch_up_reads_multiple_batches_in_one_tick(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO logs(id,ts,target,feedback_log_body) VALUES(50001,?,?,?)",
                      (NOW, "other", "not a failure"))
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.assertEqual(len(self.rpc.sends), 1)
        self.assertEqual(self.state.data["cursor"], 50002)

    def test_reconcile_preserves_consumed_failure(self):
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.state.data["cursor"] = self.store.initial_cursor(NOW + 61, 1800)
        self.watcher.tick(NOW + 61)
        self.assertEqual(len(self.rpc.sends), 1)

    def test_state_lock_and_identity(self):
        with self.assertRaisesRegex(RuntimeError, "Another watcher"):
            whipper.State(self.args.state, self.db, self.args.socket, 0)
        self.state.close()
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            whipper.State(self.args.state, self.db, self.root / "other.sock", 0)

    def test_unloaded_or_no_direct_input_skips(self):
        self.add_error()
        self.rpc.thread["status"]["type"] = "notLoaded"
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])

    def test_allowlisted_standalone_dispatch_uses_herdr(self):
        self.add_error(pid=os.getpid() + 100000)
        self.args.herdr_thread = [TID]
        fallback = Mock()
        fallback.inspect.return_value = "ready"
        fallback.send.return_value = {"turn": {"id": NEXT}}
        self.watcher.herdr = fallback
        self.watcher.tick(NOW + 60)
        fallback.send.assert_called_once()
        self.assertEqual(self.rpc.sends, [])
        self.assertEqual(self.state.data["retries"][TID]["transport"], "herdr")
        self.assertEqual(self.state.data["retries"][TID]["expected_turn"], NEXT)

    def test_herdr_blocked_session_is_not_prompted(self):
        self.add_error(pid=os.getpid() + 100000)
        self.args.herdr_thread = [TID]
        fallback = Mock()
        fallback.inspect.return_value = "herdr_blocked_or_unknown"
        self.watcher.herdr = fallback
        self.watcher.tick(NOW + 60)
        fallback.send.assert_not_called()

    def test_herdr_identity_and_input_guards(self):
        fallback = whipper.HerdrFallback([TID], "herdr")
        pid = os.getpid()
        agent = {"agent": "codex", "agent_status": "idle", "agent_session": {"value": TID}}
        def result(env, *args, text=False):
            if args[:2] == ("pane", "process-info"):
                return {"process_info": {"foreground_process_group_id": pid}}
            if args[:2] == ("agent", "get"):
                return {"agent": agent}
            return f"■ {whipper.CAPACITY}.\n› pending user text\n"
        fallback.run = result
        self.assertEqual(fallback.identity({}, "observed-pane", pid, TID), "ready")
        self.assertEqual(fallback.screen({}, "observed-pane"), "herdr_input_not_empty")
        agent["agent_session"]["value"] = NEXT
        self.assertEqual(fallback.identity({}, "observed-pane", pid, TID), "herdr_session_changed")
        agent["agent_session"]["value"] = TID
        agent["agent_status"] = "blocked"
        self.assertEqual(fallback.identity({}, "observed-pane", pid, TID), "herdr_blocked_or_unknown")

    def test_herdr_requires_explicit_thread_authorization(self):
        fallback = whipper.HerdrFallback([], "herdr")
        with self.assertRaisesRegex(RuntimeError, "authorized"):
            fallback.target({"thread_id": TID, "process_uuid": f"pid:{os.getpid()}:test"})

    def test_herdr_submission_requires_new_turn_readback(self):
        fallback = whipper.HerdrFallback([TID], "herdr")
        fallback.targets[TID] = {}, "observed-pane", os.getpid()
        event = {"thread_id": TID, "turn_id": TURN}
        with patch.object(fallback, "inspect", return_value="ready"), patch.object(fallback, "run", return_value={}) as run, patch.object(fallback, "latest", return_value={"id": NEXT}):
            reply = fallback.send(event, "keep doing", self.rpc)
        self.assertEqual(reply["turn"]["id"], NEXT)
        run.assert_called_once_with({}, "agent", "prompt", "observed-pane", "keep doing")

    def test_unix_websocket_end_to_end(self):
        """Actual socket handshake, JSON-RPC, ownership check, detection and dispatch."""
        from websockets.asyncio.server import unix_serve
        ready, stop = threading.Event(), threading.Event()
        fake = self.rpc
        failures = []

        async def handle(ws):
            async for raw in ws:
                msg = json.loads(raw)
                if msg["method"] == "initialized":
                    continue
                if msg["method"] == "initialize":
                    result = {"userAgent": "test"}
                else:
                    result = fake.call(msg["method"], msg["params"])
                await ws.send(json.dumps({"id": msg["id"], "result": result}))

        async def serve():
            async with unix_serve(handle, str(self.args.socket)):
                ready.set()
                while not stop.is_set():
                    await asyncio.sleep(0.01)

        def run():
            try:
                asyncio.run(serve())
            except Exception as e:
                failures.append(e)
                ready.set()

        thread = threading.Thread(target=run)
        thread.start()
        rpc = whipper.Rpc(self.args.socket, timeout=2)
        try:
            self.assertTrue(ready.wait(3))
            self.assertFalse(failures)
            self.add_error()
            self.watcher.rpc = rpc
            self.watcher.owners = whipper.socket_owner_pids
            self.watcher.tick(NOW + 60)
            self.assertEqual(len(fake.sends), 1)
            self.assertEqual(self.state.data["retries"][TID]["expected_turn"], NEXT)
        finally:
            rpc.close()
            stop.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
