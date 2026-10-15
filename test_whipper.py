import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import whipper

TID = "11111111-1111-7111-8111-111111111111"
TURN = "22222222-2222-7222-8222-222222222222"
NEXT = "33333333-3333-7333-8333-333333333333"
PARENT = "55555555-5555-7555-8555-555555555555"
ROOT_TURN = "66666666-6666-7666-8666-666666666666"
NOW = 1000


class BoundedLogTests(unittest.TestCase):
    def test_existing_oversized_logs_are_bounded_on_open(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "whipper.log"
            backup = path.with_name(path.name + ".1")
            for file in [path, backup]:
                file.write_text("舊紀錄\n" * 100_000, encoding="utf-8")
            handler = whipper.BoundedLogHandler(path)
            try:
                for file in [path, backup]:
                    self.assertLessEqual(file.stat().st_size, 500_000)
                    file.read_text(encoding="utf-8")
            finally:
                handler.close()

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
        self.environment = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                  if not k.startswith("WHIPPER_")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
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
            "--database", str(self.db), "--socket", str(self.root / "server with spaces.sock"),
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

    def test_opted_in_subagent_retries_with_existing_guards(self):
        self.args.subagents = True
        self.rpc.thread.update(parentThreadId=PARENT, source={"subAgent": {"thread_spawn": {"parent_thread_id": PARENT, "depth": 1}}})
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.assertEqual(len(self.rpc.sends), 1)

    def test_opted_in_internal_or_ephemeral_still_excluded(self):
        for thread in [{"source": {"internal": "compaction"}}, {"ephemeral": True},
                       {"parentThreadId": PARENT, "source": {"subAgent": "compact"}},
                       {"parentThreadId": PARENT, "source": {"subAgent": "memory_consolidation"}}]:
            self.assertIsNotNone(whipper.excluded_thread(thread, allow_subagents=True))

    def setup_review(self):
        self.args.subagents = True
        self.args.codex_home = self.root
        path = self.root / "sessions/reviewer.jsonl"
        path.parent.mkdir()
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": TID, "parent_thread_id": PARENT,
        }}) + "\n" + json.dumps({"type": "turn_context", "payload": {
            "turn_id": TURN, "root_turn_id": ROOT_TURN,
        }}) + "\n")
        self.rpc.thread.update(parentThreadId=PARENT, source={"subAgent": "review"},
                               path=str(path), status={"type": "notLoaded"}, canAcceptDirectInput=None)
        self.rpc.turn["items"] = [{"type": "userMessage", "content": [{"type": "text", "text": "Review request 42"}]}]
        self.parent_thread = {"source": "cli", "status": {"type": "idle"}, "canAcceptDirectInput": True}
        self.parent_turn = {"id": ROOT_TURN, "status": "failed", "items": [{"type": "enteredReviewMode", "review": "Review request 42"}]}
        self.review_sends = []
        self.parent_queue = []
        call = self.rpc.call
        def route(method, params):
            if params.get("threadId") == PARENT:
                if method == "thread/read":
                    return {"thread": self.parent_thread}
                if method == "thread/turns/list":
                    return {"data": [self.parent_turn]}
                if method == "thread/queue/list":
                    return {"data": self.parent_queue}
                if method == "review/start":
                    self.review_sends.append(params)
                    if self.rpc.fail_send:
                        raise TimeoutError("uncertain review submission")
                    return {"turn": {"id": NEXT}, "reviewThreadId": PARENT}
            return call(method, params)
        self.rpc.call = route
        self.add_error()

    def test_review_restart_uses_original_input_and_parent_not_child(self):
        self.setup_review()
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])
        self.assertEqual(self.review_sends, [{"threadId": PARENT, "delivery": "inline", "target": {
            "type": "custom", "instructions": "Review request 42"}}])
        self.assertEqual(self.state.data["retries"][PARENT]["transport"], "review")
        self.state.data["cursor"] = 0
        self.watcher.tick(NOW + 61)
        self.assertEqual(len(self.review_sends), 1)

    def test_review_newer_parent_turn_skips(self):
        self.setup_review()
        self.parent_turn["id"] = NEXT
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.assertFalse(self.state.data["pending"])

    def test_review_active_parent_waits_then_sends(self):
        self.setup_review()
        self.parent_thread["status"]["type"] = "active"
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.assertIn(TID, self.state.data["pending"])
        self.parent_thread["status"]["type"] = "idle"
        self.watcher.tick(NOW + 65)
        self.assertEqual(len(self.review_sends), 1)

    def test_review_queued_parent_input_skips(self):
        self.setup_review()
        self.parent_queue.append({"id": "user-input"})
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])

    def test_review_missing_origin_or_input_does_not_guess(self):
        self.setup_review()
        Path(self.rpc.thread["path"]).write_text("")
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])

    def test_review_uncertain_dispatch_is_not_replayed(self):
        self.setup_review()
        self.rpc.fail_send = True
        self.watcher.tick(NOW + 60)
        self.state.data["cursor"] = 0
        self.watcher.tick(NOW + 120)
        self.assertEqual(len(self.review_sends), 1)
        self.assertEqual(self.state.data["retries"][PARENT]["outcome"], "uncertain_or_rejected")

    def test_review_retry_chain_obeys_backoff_and_limit(self):
        self.setup_review()
        self.state.data["retries"][PARENT] = {"attempts": 2, "expected_turn": ROOT_TURN, "outcome": "accepted"}
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.watcher.tick(NOW + 240)
        self.assertEqual(len(self.review_sends), 1)
        self.assertEqual(self.state.data["retries"][PARENT]["attempts"], 3)

    def test_review_limit_survives_reviewer_thread_changes(self):
        self.setup_review()
        self.state.data["retries"][PARENT] = {"attempts": 5, "expected_turn": ROOT_TURN, "outcome": "accepted"}
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.assertFalse(self.state.data["pending"])

    def test_review_recheck_catches_newer_parent_turn(self):
        self.setup_review()
        original = self.watcher.inspect_review
        def inspect(event, thread):
            result = original(event, thread)
            self.parent_turn["id"] = NEXT
            return result
        with patch.object(self.watcher, "inspect_review", side_effect=inspect):
            self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.assertFalse(self.state.data["pending"])

    def test_review_recheck_disconnect_keeps_pending_without_sending(self):
        self.setup_review()
        original = self.watcher.inspect
        calls = 0
        def inspect(event):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("inspection disconnected")
            return original(event)
        with patch.object(self.watcher, "inspect", side_effect=inspect):
            self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.assertIn(TID, self.state.data["pending"])

    def test_review_missing_input_or_mismatched_rollout_identity_skips(self):
        self.setup_review()
        self.rpc.turn["items"] = []
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])
        self.rpc.turn["items"] = [{"type": "userMessage", "content": [{"type": "text", "text": "review"}]}]
        path = Path(self.rpc.thread["path"])
        path.write_text(path.read_text().replace(PARENT, NEXT))
        self.state.data["cursor"] = 0
        self.watcher.tick(NOW + 120)
        self.assertEqual(self.review_sends, [])

    def test_review_parent_scope_is_checked_separately(self):
        self.setup_review()
        self.args.thread = [TID]
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.review_sends, [])

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

    def test_pending_event_respects_narrowed_scope_after_restart(self):
        self.add_error()
        self.watcher.tick(NOW)
        self.assertIn(TID, self.state.data["pending"])
        self.state.close()
        self.state = whipper.State(self.args.state, self.db, self.args.socket, 999)
        self.watcher.state = self.state
        self.args.thread = [NEXT]
        self.watcher.tick(NOW + 60)
        self.assertEqual(self.rpc.sends, [])
        self.assertFalse(self.state.data["pending"])

    def test_pending_retry_respects_lowered_limit(self):
        self.add_error()
        self.watcher.tick(NOW + 60)
        self.add_error(turn=NEXT, ts=NOW + 61)
        self.rpc.turn["id"] = NEXT
        self.watcher.tick(NOW + 61)
        self.assertIn(TID, self.state.data["pending"])
        self.args.max_retries = 1
        self.watcher.tick(NOW + 181)
        self.assertEqual(len(self.rpc.sends), 1)
        self.assertFalse(self.state.data["pending"])

    def test_herdr_rechecks_thread_scope_and_queued_input(self):
        fallback = whipper.HerdrFallback([TID], "herdr")
        event = {"thread_id": TID, "turn_id": TURN}
        with patch.object(fallback, "target", return_value=({}, "pane", os.getpid())), \
                patch.object(fallback, "identity", return_value="ready"), \
                patch.object(fallback, "screen", return_value="ready"):
            self.rpc.thread["parentThreadId"] = NEXT
            self.assertEqual(fallback.inspect(event, self.rpc), "subagent_or_internal")
            self.rpc.thread["parentThreadId"] = None
            self.rpc.thread["ephemeral"] = True
            self.assertEqual(fallback.inspect(event, self.rpc), "ephemeral")
            self.rpc.thread["ephemeral"] = False
            self.rpc.queued = [{"id": "user-message"}]
            self.assertEqual(fallback.inspect(event, self.rpc), "user_input_queued")

    def test_herdr_send_uses_target_from_final_inspection(self):
        fallback = whipper.HerdrFallback([TID], "herdr")
        fallback.targets[TID] = {}, "old-pane", os.getpid()
        def inspect(event, rpc):
            fallback.targets[TID] = {}, "rechecked-pane", os.getpid()
            return "ready"
        with patch.object(fallback, "inspect", side_effect=inspect), \
                patch.object(fallback, "run", return_value={}) as run, \
                patch.object(fallback, "latest", return_value={"id": NEXT}):
            fallback.send({"thread_id": TID, "turn_id": TURN}, "keep doing", self.rpc)
        run.assert_called_once_with({}, "agent", "prompt", "rechecked-pane", "keep doing")

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
