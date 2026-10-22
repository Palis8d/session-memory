"""Adapter and store tests. Run: python3 -I tests/test_adapters.py (from the repository folder)."""
import glob
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
# Every folder the package could touch points into a throwaway sandbox, set before the first import.
SANDBOX = tempfile.mkdtemp(prefix="smem-test-")
for var, sub in (("SESSION_MEMORY_HOME", "data"), ("SESSION_MEMORY_CONFIG", "config/config.toml"),
                 ("CODEX_HOME", "codex"), ("CLAUDE_CONFIG_DIR", "claude"), ("XDG_CONFIG_HOME", "xdg-config")):
    os.environ[var] = os.path.join(SANDBOX, sub)
for d in ("codex", "claude/projects"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)
from session_memory import claude, cli, codex, health, ingest, store  # noqa: E402

FX = health.FIXTURES


def test_current_format():
    r = codex.parse_thread([os.path.join(FX, "codex_current.jsonl")])
    (t,) = r["turns"]
    assert t["request"] == "Add a health check to the demo app", t  # ambient block dropped
    assert t["answer"] == "Added /health and a test."
    assert t["commands"] == 1, "a code-mode call and its completed item must count once"
    assert r["files"] == {"/tmp/demo/health.py": ("add", 0)}
    assert not r["unknown_types"]


def test_legacy_format():
    r = codex.parse_thread([os.path.join(FX, "codex_legacy.jsonl")])
    (t,) = r["turns"]
    assert t["request"] == "Summarize the repo", "legacy event wins over the mirrored response message"
    assert t["answer"] == "It is a demo app."
    assert t["commands"] == 1


def test_segments_skip_overlap_and_join_split_turn():
    paths = sorted(glob.glob(os.path.join(FX, "segments", "*.jsonl")), key=os.path.basename)
    tid = "bbbbbbbb-0000-0000-0000-000000000001"
    assert [codex.ROLLOUT_RE.search(os.path.basename(p)).group(1) for p in paths] == [tid, tid]
    r = codex.parse_thread(paths)
    assert [t["request"] for t in r["turns"]] == ["First request", "Second request"], r["turns"]
    assert [t["answer"] for t in r["turns"]] == ["First answer", "Second answer"]
    assert r["command_count"] == 3, "overlapping records must not be counted twice"
    assert r["segments"] == 2


def test_drift_is_reported():
    r = codex.parse_thread([os.path.join(FX, "codex_drifted.jsonl")])
    assert not r["turns"]
    assert r["unknown_types"].get("event:item_done") == 3


def test_scrub():  # review F1
    cases = ["key sk-ant-abcdefghijklmnopqrstuvwxyz0123 and token=supersecretvalue1",
             '{"password": "synthetic-password-123"}', "SERVICE_API_KEY=synthetic-value-123456",
             "postgresql://user:synthetic-password-123@localhost/db", 'set password: "two words here" ok']
    for c in cases:
        out = store.clean(c)
        assert "synthetic" not in out and "supersecret" not in out and "two words" not in out, out
    for keep in ("see /Users/x/api_key_rotation.md", "<automation_id>weekly-task-summary-and-cleanup-run</automation_id>",
                 "are_you_sure_about_this_value_here_x"):
        assert store.clean(keep) == keep, store.clean(keep)  # no redaction inside ordinary words


def test_canaries():
    assert all(ok for ok, _, _ in health.selftest())


def test_claude_prompt_filter():
    assert claude._user_prompt({"content": "<system-reminder>x</system-reminder>real ask"}) == "real ask"
    assert claude._user_prompt({"content": [{"type": "tool_result", "content": "out"}]}) == ""


def test_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        r = codex.parse_thread([os.path.join(FX, "codex_current.jsonl")])
        r.pop("meta"), r.pop("segments")
        rec = {"agent": "codex", "session_id": "x", "kind": "top", "log_path": "p", "updated_at": "2026-10-01T00:00:00Z"}
        rec.update(r)
        store.replace_session(con, rec)
        store.replace_session(con, dict(rec, turns=list(r["turns"]), files=dict(r["files"])))  # idempotent
        hits = con.execute("select count(*) from turns_fts where turns_fts match 'health'").fetchone()[0]
        assert hits == 1, hits
        assert con.execute("select request_turns, answer_turns from sessions").fetchone()[:] == (1, 1)



def _write(tmp, name, records):
    p = os.path.join(tmp, name)
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


def _ev(o, payload, typ="event_msg"):
    return {"timestamp": "2026-10-01T10:00:00Z", "ordinal": o, "type": typ, "payload": payload}


def _item(o, item):
    return _ev(o, {"type": "item_completed", "item": item})


def test_segment_boundary_after_commentary_and_split_call():  # review F3
    meta = {"type": "session_meta", "ordinal": 0, "payload": {"id": "dddddddd-0000-0000-0000-000000000001"}}
    seg1 = [meta, _ev(1, {"type": "task_started"}),
            _item(2, {"type": "UserMessage", "content": [{"type": "text", "text": "synthetic request"}]}),
            _item(3, {"type": "AgentMessage", "phase": "commentary", "content": [{"type": "text", "text": "Progress so far"}]}),
            _ev(4, {"type": "custom_tool_call", "name": "exec", "input": "await tools.exec_command({cmd:'ls'})"}, "response_item")]
    seg2 = [meta, _ev(4, {"type": "custom_tool_call", "name": "exec", "input": "x"}, "response_item"),
            _item(5, {"type": "CommandExecution", "command": ["ls"]}),
            _item(6, {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "text", "text": "Final result"}]}),
            _ev(7, {"type": "task_complete", "last_agent_message": "Final result"})]
    with tempfile.TemporaryDirectory() as tmp:
        a = _write(tmp, "rollout-2026-10-01T10-00-00-dddddddd-0000-0000-0000-000000000001.jsonl", seg1)
        b = _write(tmp, "rollout-2026-10-01T11-00-00-dddddddd-0000-0000-0000-000000000001_eeeeeeee-0000-0000-0000-000000000001.jsonl", seg2)
        r = codex.parse_thread([a, b])
    assert [(t["request"], t["answer"]) for t in r["turns"]] == [("synthetic request", "Final result")], r["turns"]
    assert r["command_count"] == 1 and r["commands_estimated"] == 0, r


def test_mixed_legacy_then_items():  # review F14
    recs = [_ev(1, {"type": "task_started"}), _ev(2, {"type": "user_message", "message": "First ask"}),
            _ev(3, {"type": "agent_message", "message": "First answer"}), _ev(4, {"type": "task_complete"}),
            _ev(5, {"type": "task_started"}),
            _item(6, {"type": "UserMessage", "content": [{"type": "text", "text": "Second ask"}]}),
            _item(7, {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "text", "text": "Second answer"}]})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "x.jsonl", recs)])
    assert [(t["request"], t["answer"]) for t in r["turns"]] == [("First ask", "First answer"), ("Second ask", "Second answer")]


def test_file_turn_index_after_filtering():  # review F7
    recs = [_ev(1, {"type": "task_started"}), _ev(2, {"type": "task_started"}),  # empty setup turn is dropped
            _item(3, {"type": "UserMessage", "content": [{"type": "text", "text": "edit it"}]}),
            _item(4, {"type": "FileChange", "changes": {"/tmp/a.py": {"type": "update"}}})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "x.jsonl", recs)])
    assert len(r["turns"]) == 1 and r["files"] == {"/tmp/a.py": ("update", 0)}, r


def _turn(q, a, commands=0):
    import hashlib
    return {"request": q, "answer": a, "commands": commands, "files": [],
            "raw_hash": hashlib.sha1(f"{q}\x00{a}".encode()).hexdigest()}


def _put(con, sid, turns, parent=None, agent="codex", forked=True):
    store.replace_session(con, {
        "agent": agent, "session_id": sid, "kind": "helper" if parent else "top", "log_path": "p",
        "parent_id": parent, "parent_rel": "helper" if parent else None, "forked_from": parent if forked else None,
        "turns": turns, "files": {}, "command_count": sum(t["commands"] for t in turns)})


def test_legacy_helper_meta_and_inherited_turns():  # review F2
    kind, parent, role = codex.helper_info({"subagent": {"thread_spawn": {"parent_thread_id": "P", "agent_role": "worker"}}})
    assert (kind, parent, role) == ("helper", "P", "worker")
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        shared = [_turn(f"ask {i}", f"ans {i}", 1) for i in range(3)]
        _put(con, "P", list(shared))
        _put(con, "C", list(shared) + [_turn("own task", "own answer", 2)], parent="P")
        keys = {"P": [t["raw_hash"] for t in shared], "C": [t["raw_hash"] for t in shared] + ["own"]}
        assert ingest.trim_inherited(con, {"C"}, keys, lambda t: None) == 1
        row = con.execute("select turn_count, inherited_turns, command_count, first_request, last_answer "
                          "from sessions where session_id='C'").fetchone()
        assert tuple(row) == (1, 3, 2, "own task", "own answer"), tuple(row)
        assert con.execute("select turn_index from turns where session_id='C'").fetchall()[0][0] == 0


def test_trimming_keeps_real_child_work():  # rereview R1
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        # same wording, different answer: the child's own work
        _put(con, "P", [_turn("Review changes", "parent result", 1)])
        _put(con, "C", [_turn("Review changes", "different child result", 7), _turn("Next step", "done", 2)], parent="P")
        # secrets that scrub to the same text are still different raw text
        _put(con, "P2", [_turn("API_KEY=parent-secret-123", "a", 1)])
        _put(con, "C2", [_turn("API_KEY=child-secret-456", "a", 1), _turn("x", "y")], parent="P2")
        # a helper that isn't a fork copy is never trimmed, and Claude sessions are never trimmed
        _put(con, "P3", [_turn("same", "same", 1)])
        _put(con, "C3", [_turn("same", "same", 1), _turn("x", "y")], parent="P3", forked=False)
        _put(con, "P4", [_turn("same", "same", 1)], agent="claude")
        _put(con, "C4", [_turn("same", "same", 1), _turn("x", "y")], parent="P4", agent="claude")
        keys = {sid: [r[0] for r in con.execute("select request||char(0)||answer from turns where session_id=? "
                                                "order by turn_index", (sid,))] for sid in ("C3", "P3", "C4", "P4")}
        keys.update({"P": [_turn("Review changes", "parent result")["raw_hash"]],
                     "C": [_turn("Review changes", "different child result")["raw_hash"], "x"],
                     "P2": [_turn("API_KEY=parent-secret-123", "a")["raw_hash"]],
                     "C2": [_turn("API_KEY=child-secret-456", "a")["raw_hash"], "x"]})
        assert ingest.trim_inherited(con, {"C", "C2", "C3", "C4"}, keys, lambda t: None) == 0
        assert [r[0] for r in con.execute("select turn_count from sessions where session_id in ('C','C2','C3','C4') "
                                          "order by session_id")] == [2, 2, 2, 2]


def test_full_rebuild_failure_keeps_store():  # rereview R2
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        old = store.connect(os.path.join(tmp, "old.sqlite"))
        for i in range(6):
            store.replace_session(old, {"agent": "claude", "session_id": f"s{i}", "kind": "top", "log_path": "p",
                                        "turns": [_turn("hello", "hi")], "files": {}})
        old.commit()
        new = store.connect(os.path.join(tmp, "new.sqlite"))
        with patch.object(codex, "load_threads", return_value={}), patch.object(codex, "scan_rollouts", return_value={}), \
                patch.object(codex, "load_goals", return_value=[]), patch.object(claude, "list_logs", return_value=[]), \
                patch.object(ingest, "ProcessPoolExecutor", _SyncPool):
            try:
                ingest.update(new, full=True, workers=1, previous=old, log=lambda *_: None)
                raise AssertionError("an empty rebuild was accepted")
            except RuntimeError as e:
                assert "keeping the current store" in str(e)
        assert old.execute("select count(*) from sessions").fetchone()[0] == 6


class _SyncPool:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, items, chunksize=1):
        return map(fn, items)


def test_scrub_escaped_json_and_refresh():  # rereview R3/R4
    assert "def-secret" not in store.clean(json.dumps({"password": 'abc"def-secret-123'}))
    assert store.clean("password=12345") == "password=[REDACTED]"
    assert "secret-9" not in store.clean('{\\"api_key\\": \\"secret-9\\"}')
    for keep in ("author: Alice Example", "authentication: required", "token_count=123456", "tokens_used=99"):
        assert store.clean(keep) == keep, store.clean(keep)
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        rec = {"agent": "codex", "session_id": "x", "name": "n", "project": None, "cwd": None, "origin": "app",
               "kind": "top", "parent_id": None, "parent_rel": None, "helper_role": None, "archived": 0,
               "updated_at": None, "tokens_used": None, "model": None, "effort": None,
               "git_branch": "token=synthetic-token-123", "log_path": "p"}
        store.replace_session(con, dict(rec, turns=[], files={}))
        ingest._refresh_metadata(con, rec)
        assert con.execute("select git_branch from sessions").fetchone()[0] == "token=[REDACTED]"


def test_db_name_must_stay_in_data():  # rereview R5
    import subprocess
    for bad in ("/outside-data/probe.sqlite", "../escape.sqlite"):
        r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {SRC!r}); import session_memory.store"],
                           env={"SESSION_MEMORY_DB_NAME": bad, "PATH": os.environ.get("PATH", "")},
                           capture_output=True, text=True)
        assert r.returncode != 0 and "plain file name" in (r.stderr + r.stdout), (bad, r.stderr)


def test_signature_includes_full_path():  # rereview R8
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a", "same.jsonl"), os.path.join(tmp, "b", "same.jsonl")
        for p in (a, b):
            os.makedirs(os.path.dirname(p))
            open(p, "w").write("x")
            os.utime(p, ns=(1_000_000_000, 1_000_000_000))
        assert codex.log_sig(a) != codex.log_sig(b) and claude.log_sig(a) != claude.log_sig(b)


def test_same_turn_mirror_and_follow_up():  # rereview R10
    recs = [_ev(1, {"type": "task_started"}), _ev(2, {"type": "user_message", "message": "First ask"}),
            _item(3, {"type": "UserMessage", "content": [{"type": "text", "text": "First ask"}]}),
            _item(4, {"type": "UserMessage", "content": [{"type": "text", "text": "Real follow-up"}]}),
            _item(5, {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "text", "text": "Final"}]})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "x.jsonl", recs)])
    assert r["turns"][0]["request"] == "First ask\n\nReal follow-up", r["turns"]


def test_session_full_shows_everything():  # rereview R9
    import contextlib
    import io
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        store.replace_session(con, {"agent": "codex", "session_id": "big", "kind": "top", "log_path": "p",
                                    "turns": [_turn("x" * 120000 + "TAIL_SENTINEL", "done")], "files": {}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli, "reader", lambda: con):
            cli.cmd_session(type("A", (), {"id": "big", "agent": None, "turns": 0, "full": True})())
        assert "TAIL_SENTINEL" in out.getvalue()


def test_claude_compaction_summary_is_not_a_prompt():  # review F6
    recs = [{"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued…"}},
            {"type": "user", "message": {"role": "user", "content": "real ask"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/tmp/ok.py"}},
                {"type": "tool_use", "id": "t2", "name": "Write", "input": {"file_path": "/tmp/failed.py"}},
                {"type": "text", "text": "done"}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "denied", "is_error": True}]}}]
    with tempfile.TemporaryDirectory() as tmp:
        r = claude.parse_log(_write(tmp, "s.jsonl", recs), "s", None)
    assert [t["request"] for t in r["turns"]] == ["real ask"], r["turns"]
    assert r["inherited_context"].startswith("This session is being continued")
    assert list(r["files"]) == ["/tmp/ok.py"], r["files"]  # review S3: failed edits aren't changes


def test_vanished_sources_are_removed():  # review F8
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        for sid in ("a", "b", "c", "d", "e", "f"):
            store.replace_session(con, {"agent": "claude", "session_id": sid, "kind": "top", "log_path": "p",
                                        "turns": [{"request": "hello", "answer": "hi"}], "files": {}})
        stats = {"failures": []}
        ingest._reconcile(con, "claude", {"a", "b", "c", "d", "e"}, stats, lambda *_: None)
        assert stats.get("claude_removed") == 1
        assert con.execute("select count(*) from turns_fts where session_id='f'").fetchone()[0] == 0
        ingest._reconcile(con, "claude", set(), stats, lambda *_: None)  # mass disappearance is refused
        assert con.execute("select count(*) from sessions").fetchone()[0] == 5


def test_store_refuses_source_paths():  # review F13
    try:
        store.connect(os.path.expanduser("~/.codex/should-never-exist.sqlite"))
    except ValueError:
        return
    raise AssertionError("connect() accepted a path inside ~/.codex")


def test_local_time_handles_daylight_saving():  # review F12
    mem = cli
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Denver"
    time.tzset()
    try:
        assert mem.local("2026-10-13T05:28:06Z") == "2026-10-12 23:28"
        assert mem.local("2026-01-13T05:28:06Z") == "2026-01-12 22:28"
    finally:
        if old is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_outdated_store_is_never_wiped():  # self-review: schema change must not destroy the store
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sqlite")
        con = store.connect(path)
        store.replace_session(con, {"agent": "codex", "session_id": "keep", "kind": "top", "log_path": "p",
                                    "turns": [_turn("hello", "hi")], "files": {}})
        con.execute("insert or replace into meta values ('acked_tools', '[\"_web_run\"]')")
        con.execute("update meta set value='0' where key='schema_version'")
        con.commit()
        con.close()
        try:
            store.connect(path)
            raise AssertionError("an outdated store was opened for normal use")
        except store.OutdatedStore:
            pass
        old = store.connect(path, allow_outdated=True)
        assert old.execute("select count(*) from sessions").fetchone()[0] == 1
        assert old.execute("select value from meta where key='acked_tools'").fetchone()[0] == '["_web_run"]'


def test_empty_answers_are_readable_and_markdown_secrets():  # self-review
    recs = [_ev(1, {"type": "task_started"}),
            _item(2, {"type": "UserMessage", "content": [{"type": "text", "text": "<heartbeat>tick</heartbeat>"}]}),
            _item(3, {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "Text", "text": ""}]})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "x.jsonl", recs * 3)])
    assert (r["agent_records"], r["agent_read"]) == (3, 3), r
    assert store.clean("**Password:** hunter2") == "**Password:** [REDACTED]", store.clean("**Password:** hunter2")
    cmd_only = [_ev(1, {"type": "task_started"}), _item(2, {"type": "CommandExecution", "command": ["ls"]})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "y.jsonl", cmd_only)])
    assert r["turns"][0]["raw_hash"] is None


# ---------------------------------------------------------------- final review (N1-N8, P3/S4)

def _mem():
    return cli


def _seed_claude(con, ids, goal=True):
    for sid in ids:
        store.replace_session(con, {"agent": "claude", "session_id": sid, "kind": "top", "log_path": f"/x/{sid}.jsonl",
                                    "turns": [_turn(f"hello {sid}", "hi")], "files": {}})
    if goal:
        con.execute("insert into goals values ('g1', 'blocked', 'ship it', 1, '2026-10-01T00:00:00Z')")
    con.commit()


def _sources(tmp, claude_ids, fail=None, goals=None):
    """Patches: no Codex sources; Claude logs `claude_ids` (one may fail to read); goals as given."""
    from unittest.mock import patch
    logs = []
    for sid in claude_ids:
        p = os.path.join(tmp, f"{sid}.jsonl")
        open(p, "w").write(json.dumps({"type": "user", "message": {"role": "user", "content": f"hello {sid}"}}) + "\n")
        logs.append((p, sid, None))
    real = ingest._parse_claude

    def parse(args):
        return (args, None, "synthetic read failure") if args[1] == fail else real(args)
    return [patch.object(codex, "load_threads", return_value={}), patch.object(codex, "scan_rollouts", return_value={}),
            patch.object(codex, "load_goals", return_value=goals), patch.object(claude, "list_logs", return_value=logs),
            patch.object(ingest, "_parse_claude", parse), patch.object(ingest, "ProcessPoolExecutor", _SyncPool),
            patch.object(ingest, "rebuild_links", lambda con: None)]


def _run(patches, fn):
    import contextlib
    with contextlib.ExitStack() as st:
        for pt in patches:
            st.enter_context(pt)
        return fn()


def test_rebuild_refuses_after_a_read_failure_and_keeps_goals():  # N1
    mem = _mem()
    with tempfile.TemporaryDirectory() as tmp:
        live = os.path.join(tmp, "memory.sqlite")
        con = store.connect(live)
        ids = [f"s{i}" for i in range(6)]
        _seed_claude(con, ids)
        old_path = mem.store.DB_PATH
        mem.store.DB_PATH = live
        try:
            try:
                _run(_sources(tmp, ids, fail="s5", goals=None), lambda: mem.rebuild(con, workers=1))
                raise AssertionError("a rebuild with an unreadable log was promoted")
            except SystemExit as e:
                assert "could not be read" in str(e)
            assert con.execute("select count(*) from sessions").fetchone()[0] == 6
            # Without read failures, unreadable goals are carried over instead of lost.
            _run(_sources(tmp, ids, goals=None), lambda: mem.rebuild(con, workers=1))
            assert con.execute("select objective from goals").fetchone()[0] == "ship it"
            assert con.execute("select count(*) from sessions").fetchone()[0] == 6
            assert not os.path.exists(live + ".rebuild")
            # Six sessions replaced by six different ones: refused, compared by identity.
            try:
                _run(_sources(tmp, [f"n{i}" for i in range(6)], goals=[]), lambda: mem.rebuild(con, workers=1))
                raise AssertionError("an identity swap was promoted")
            except SystemExit as e:
                assert "missing 6" in str(e)
            assert {r[0] for r in con.execute("select session_id from sessions")} == set(ids)
        finally:
            mem.store.DB_PATH = old_path


def test_rebuild_swap_keeps_readers_consistent():  # N2
    from unittest.mock import patch
    mem = _mem()
    with tempfile.TemporaryDirectory() as tmp:
        live = os.path.join(tmp, "memory.sqlite")
        con = store.connect(live)
        _seed_claude(con, ["s0", "s1"])
        con.close()
        con = store.connect(live)  # exactly as `smem update` opens it, with no further commits
        reader = sqlite3.connect(live)
        reader.execute("begin")
        reader.execute("select count(*) from sessions").fetchone()  # a pinned read snapshot
        con.execute("insert into meta values ('late', 'committed')")
        con.commit()  # committed data sitting in the WAL
        removed = []
        real_remove = os.remove
        old_path = mem.store.DB_PATH
        mem.store.DB_PATH = live
        try:
            with patch.object(mem.os, "remove", lambda p: (removed.append(p), real_remove(p))):
                _run(_sources(tmp, ["s0", "s1"], goals=[]), lambda: mem.rebuild(con, workers=1))
        finally:
            mem.store.DB_PATH = old_path
        assert not [p for p in removed if p.startswith(live) and not p.startswith(live + ".rebuild")], removed
        reader.rollback()
        fresh = sqlite3.connect(live)
        assert fresh.execute("select count(*) from sessions").fetchone()[0] == 2  # opens fine, new content
        reader.close()
        fresh.close()


def test_no_text_hashes_are_stored():  # N3
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        cols = {r[1] for r in con.execute("pragma table_info(turns)")}
        assert "raw_hash" not in cols, cols
        store.replace_session(con, {"agent": "codex", "session_id": "x", "kind": "top", "log_path": "p",
                                    "turns": [_turn("password=0420", "done")], "files": {}})
        row = con.execute("select * from turns").fetchone()
        assert all("0420" not in str(v) for v in tuple(row)), tuple(row)


def test_claude_unread_text_and_crosscheck_on_reads():  # N4
    recs = [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "valid"}]}}]
    recs += [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "body": "lost"}]}}] * 9
    recs += [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "output_text", "text": "moved"}]}}] * 3
    with tempfile.TemporaryDirectory() as tmp:
        r = claude.parse_log(_write(tmp, "s.jsonl", recs), "s", None)
        assert (r["user_records"], r["user_read"]) == (10, 1), r
        assert (r["agent_records"], r["agent_read"]) == (3, 0), r
        # Cross-check compares what was *read* with Codex's own index.
        home = os.path.join(tmp, "codex")
        os.makedirs(home)
        h = sqlite3.connect(os.path.join(home, "thread_history_1.sqlite"))
        h.execute("create table thread_items (thread_id text, item_type text)")
        h.executemany("insert into thread_items values ('t1', ?)", [("userMessage",)] * 10 + [("agentMessage",)] * 10)
        h.commit()
        h.close()
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        store.replace_session(con, {"agent": "codex", "session_id": "t1", "kind": "top", "log_path": "p",
                                    "turns": [_turn("a", "b")], "files": {}, "user_records": 10, "user_read": 1,
                                    "agent_records": 10, "agent_read": 10, "updated_at": "2026-10-21T00:00:00Z"})
        old = codex.CODEX_HOME
        codex.CODEX_HOME = home
        try:
            (lvl, _, detail), = health.check_crosscheck(con, top_n=1, helper_n=0)
        finally:
            codex.CODEX_HOME = old
        assert lvl == health.RED and "user messages read 1 vs 10" in detail, (lvl, detail)


def test_refused_update_keeps_discovery_baseline():  # N5
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        _seed_claude(con, [f"s{i}" for i in range(6)], goal=False)
        con.execute("insert into meta values ('inventory', ?)", (json.dumps({"claude_logs": 6}),))
        con.commit()
        _run(_sources(tmp, [], goals=[]), lambda: ingest.update(con, workers=1, log=lambda *_: None))
        assert con.execute("select count(*) from sessions").fetchone()[0] == 6
        assert json.loads(con.execute("select value from meta where key='inventory'").fetchone()[0])["claude_logs"] == 6


def test_discovery_finds_logs_the_scan_missed():  # N5
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "codex")
        os.makedirs(os.path.join(home, "sessions"))
        odd = os.path.join(home, "elsewhere.jsonl")  # resolvable, but not where/how the scanner looks
        open(odd, "w").write("{}\n")
        st = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        st.execute("create table threads (id text, rollout_path text)")
        st.execute("insert into threads values ('aaaaaaaa-0000-0000-0000-000000000009', ?)", (odd,))
        st.commit()
        st.close()
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        old = codex.CODEX_HOME, claude.CLAUDE_PROJECTS
        codex.CODEX_HOME, claude.CLAUDE_PROJECTS = home, os.path.join(tmp, "claude")
        try:
            res = {name: (lvl, d) for lvl, name, d in health.check_discovery(con)}
        finally:
            codex.CODEX_HOME, claude.CLAUDE_PROJECTS = old
        lvl, detail = res["codex discovery"]
        assert lvl == health.RED and "weren't found by the scan" in detail, (lvl, detail)


def test_merge_keeps_source_order_and_latest_progress():  # N6
    recs = [_ev(1, {"type": "task_started"}), _ev(2, {"type": "user_message", "message": "A"}),
            _item(3, {"type": "UserMessage", "content": [{"type": "text", "text": "A"}]}),
            _item(4, {"type": "UserMessage", "content": [{"type": "text", "text": "B"}]}),
            _ev(5, {"type": "user_message", "message": "C"}),
            _item(6, {"type": "AgentMessage", "content": [{"type": "text", "text": "earlier progress"}]}),
            _ev(7, {"type": "agent_message", "message": "latest progress"})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "x.jsonl", recs)])
    t = r["turns"][0]
    assert (t["request"], t["answer"]) == ("A\n\nB\n\nC", "latest progress"), t
    twice = [_ev(1, {"type": "task_started"}), _ev(2, {"type": "user_message", "message": "go"}),
             _item(3, {"type": "UserMessage", "content": [{"type": "text", "text": "go"}]}),
             _ev(4, {"type": "user_message", "message": "go"}),
             _item(5, {"type": "UserMessage", "content": [{"type": "text", "text": "go"}]})]
    with tempfile.TemporaryDirectory() as tmp:
        r = codex.parse_thread([_write(tmp, "y.jsonl", twice)])
    assert r["turns"][0]["request"] == "go\n\ngo", r["turns"]  # sent twice on purpose, mirrored once each


def test_escaped_json_keeps_following_fields():  # N7
    out = store.clean(r'{\"password\": \"synthetic-123\", \"author\": \"Alice\", \"note\": \"keep me\"}')
    assert "synthetic" not in out and "Alice" in out and "keep me" in out, out


def test_table_parent_clears_but_log_parent_stays():  # N8
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        for sid, src in (("t", "table"), ("l", "log")):
            store.replace_session(con, {"agent": "codex", "session_id": sid, "kind": "helper", "log_path": "p",
                                        "parent_id": "old-parent", "parent_rel": "helper", "parent_source": src,
                                        "turns": [], "files": {}})
        base = {"name": "n", "project": None, "cwd": None, "origin": "app", "kind": "top", "parent_id": None,
                "parent_rel": None, "helper_role": None, "archived": 0, "updated_at": None, "tokens_used": None,
                "model": None, "effort": None, "git_branch": None, "log_path": None}
        for sid in ("t", "l"):
            ingest._refresh_metadata(con, dict(base, session_id=sid))
        got = dict(con.execute("select session_id, parent_id from sessions").fetchall())
        assert got == {"t": None, "l": "old-parent"}, got


def test_duplicate_segment_and_ordinal_reset():  # P3 / S4
    meta = {"type": "session_meta", "ordinal": 0, "payload": {"id": "ffffffff-0000-0000-0000-000000000001"}}
    seg = [meta] + [_ev(1, {"type": "task_started"}),
                    _item(2, {"type": "UserMessage", "content": [{"type": "text", "text": "ask"}]}),
                    _item(3, {"type": "CommandExecution", "command": ["ls"]}),
                    _ev(4, {"type": "task_complete", "last_agent_message": "done"})]
    reset = [meta] + [_ev(1, {"type": "task_started", "turn_id": "t2"}),  # real records carry their own ids
                      _item(2, {"type": "UserMessage", "content": [{"type": "text", "text": "a different ask"}]}),
                      _ev(3, {"type": "task_complete", "last_agent_message": "other"})]
    with tempfile.TemporaryDirectory() as tmp:
        a = _write(tmp, "rollout-2026-10-01T10-00-00-ffffffff-0000-0000-0000-000000000001.jsonl", seg)
        b = _write(tmp, "rollout-2026-10-01T11-00-00-ffffffff-0000-0000-0000-000000000001_99999999-0000-0000-0000-000000000001.jsonl", seg)
        c = _write(tmp, "rollout-2026-10-01T12-00-00-ffffffff-0000-0000-0000-000000000001_88888888-0000-0000-0000-000000000001.jsonl", reset)
        dup = codex.parse_thread([a, b])
        assert (len(dup["turns"]), dup["command_count"], dup["unknown_types"]) == (1, 1, {}), dup  # exact copy: once
        rst = codex.parse_thread([a, c])
        assert [t["request"] for t in rst["turns"]] == ["ask", "a different ask"], rst["turns"]  # nothing lost
        assert [t["rewound"] for t in rst["turns"]] == [1, 0]  # the first branch was rewound past


# ---------------------------------------------------------------- confirmation (C1, C2)

def test_escaped_quote_inside_escaped_json_is_fully_redacted():  # C1
    s = json.dumps(json.dumps({"password": 'prefix"TAIL_SENTINEL', "author": "Alice"}))[1:-1]
    out = store.clean(s)
    assert "TAIL_SENTINEL" not in out and "Alice" in out, out
    for value in ('a\\b"c', "ends-with\\"):
        out = store.clean(json.dumps(json.dumps({"password": value, "x": "keep"}))[1:-1])
        assert "keep" in out and "ends-with" not in out and 'b\\\\\\"c' not in out, out
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        store.replace_session(con, {"agent": "codex", "session_id": "x", "kind": "top", "log_path": "p",
                                    "turns": [_turn(s, "done")], "files": {}})
        for q in ("select first_request from sessions", "select request from turns", "select request from turns_fts"):
            assert "TAIL_SENTINEL" not in con.execute(q).fetchone()[0], q


def test_duplicate_segment_does_not_mark_rewound():  # C2
    meta = {"type": "session_meta", "ordinal": 0, "payload": {"id": "eeeeeeee-0000-0000-0000-000000000001"}}
    seg = [meta, _ev(1, {"type": "task_started", "turn_id": "t1"}),
           _item(2, {"type": "UserMessage", "content": [{"type": "text", "text": "ask"}]}),
           _ev(3, {"type": "task_complete", "last_agent_message": "done"})]
    with tempfile.TemporaryDirectory() as tmp:
        a = _write(tmp, "rollout-2026-10-01T10-00-00-eeeeeeee-0000-0000-0000-000000000001.jsonl", seg)
        b = _write(tmp, "rollout-2026-10-01T11-00-00-eeeeeeee-0000-0000-0000-000000000001_77777777-0000-0000-0000-000000000001.jsonl", seg)
        one, two = codex.parse_thread([a]), codex.parse_thread([a, b])
    assert [t["rewound"] for t in one["turns"]] == [t["rewound"] for t in two["turns"]] == [0], two["turns"]


if __name__ == "__main__":
    import atexit
    import shutil
    atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            except (Exception, SystemExit) as e:
                failed += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
