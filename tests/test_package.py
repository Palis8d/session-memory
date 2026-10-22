"""Tests for settings, exclusions, compressed logs, the background job, skills, and the `smem` command end to end.
Run: python3 -I tests/test_package.py (from the repository folder). Nothing outside a throwaway folder is touched,
and no system command (launchctl, systemctl) is ever run."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
SANDBOX = tempfile.mkdtemp(prefix="smem-test-")
ENV = {"HOME": "home", "SESSION_MEMORY_HOME": "data", "SESSION_MEMORY_CONFIG": "config/config.toml",
       "CODEX_HOME": "home/.codex", "CLAUDE_CONFIG_DIR": "home/.claude", "XDG_CONFIG_HOME": "home/.config",
       "XDG_DATA_HOME": "home/.local/share"}
for var, sub in ENV.items():
    os.environ[var] = os.path.join(SANDBOX, sub)
os.environ["SESSION_MEMORY_DRY_SYSTEM"] = "1"
for d in ("home/.codex/sessions", "home/.claude/projects"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)
from session_memory import claude, cli, codex, config, health, ingest, schedule, skills, store  # noqa: E402

FX = health.FIXTURES
QUIET = lambda *a, **k: None  # noqa: E731


class _SyncPool:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, items, chunksize=1):
        return map(fn, items)


def _reset_settings():
    if os.path.exists(config.config_path()):
        os.remove(config.config_path())
    config.apply(config.load(reload=True))


def _claude_log(cwd, sid, words):
    folder = os.path.join(config.claude_home(), "projects", "".join(c if c.isalnum() else "-" for c in cwd))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{sid}.jsonl")
    with open(path, "w") as f:
        for rec in ({"type": "user", "cwd": cwd, "timestamp": "2026-10-01T10:00:00Z",
                     "message": {"role": "user", "content": words}},
                    {"type": "assistant", "cwd": cwd, "timestamp": "2026-10-01T10:00:05Z",
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "done: " + words}]}}):
            f.write(json.dumps(rec) + "\n")
    return path


# ---------------------------------------------------------------- settings

def test_settings_roundtrip_and_types():
    _reset_settings()
    assert config.load()["refresh"]["mode"] == "manual"
    config.set_value("refresh.interval_minutes", "30")
    config.set_value("privacy.exclude_projects", '["secret-client"]')
    cfg = config.load(reload=True)
    assert (cfg["refresh"]["interval_minutes"], cfg["privacy"]["exclude_projects"]) == (30, ["secret-client"])
    text = open(config.config_path()).read()
    assert "[refresh]" in text and text.count("# ") >= 4, text  # commented, readable by hand
    for key, raw in (("refresh.interval_minutes", '"often"'), ("refresh.interval_minutes", "2"),
                     ("refresh.mode", "sometimes"), ("nope.key", "1"), ("privacy.exclude_paths", "true"),
                     ("refresh.interval_minutes", "true")):
        try:
            config.set_value(key, raw)
            raise AssertionError(f"accepted {key}={raw}")
        except SystemExit:
            pass
    assert config.load(reload=True)["refresh"]["interval_minutes"] == 30  # a refused change leaves the file alone
    _reset_settings()


def test_assistant_folders_follow_their_own_variables():
    _reset_settings()
    assert config.codex_home() == os.environ["CODEX_HOME"]
    assert config.claude_home() == os.environ["CLAUDE_CONFIG_DIR"]
    assert config.skills_dir("claude") == os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "skills")
    assert config.skills_dir("codex") == os.path.join(os.environ["HOME"], ".agents", "skills")
    assert (codex.CODEX_HOME, claude.CLAUDE_PROJECTS) == (
        os.environ["CODEX_HOME"], os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects"))
    try:
        store.connect(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "x.sqlite"))
        raise AssertionError("a store inside Claude Code's folder was allowed")
    except ValueError:
        pass


# ---------------------------------------------------------------- exclusions and sources

def test_exclusions_and_turned_off_sources_leave_the_memory():
    _reset_settings()
    try:
        _exclusions_case()
    finally:
        shutil.rmtree(os.path.join(config.claude_home(), "projects"))
        os.makedirs(os.path.join(config.claude_home(), "projects"))
        _reset_settings()


def _exclusions_case():
    with tempfile.TemporaryDirectory() as tmp:
        _claude_log("/work/private-client", "11111111-0000-0000-0000-000000000001", "quarterly numbers")
        _claude_log("/work/open-project", "11111111-0000-0000-0000-000000000002", "fix the login page")
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        with patch.object(ingest, "ProcessPoolExecutor", _SyncPool):
            ingest.update(con, workers=1, log=QUIET)
            assert con.execute("select count(*) from sessions where agent='claude'").fetchone()[0] == 2
            config.set_value("privacy.exclude_paths", '["/work/private-client"]')
            s = ingest.update(con, workers=1, log=QUIET)
            rows = [r[0] for r in con.execute("select cwd from sessions where agent='claude'")]
            assert rows == ["/work/open-project"], rows
            assert con.execute("select count(*) from turns_fts where request like '%quarterly%'").fetchone()[0] == 0
            assert s.get("excluded_removed") == 1, s
            (lvl, _, detail), = [r for r in health.check_discovery(con) if r[1] == "claude discovery"]
            assert lvl == health.GREEN and "1 excluded" in detail, (lvl, detail)
            config.set_value("sources.claude", "false")
            s = ingest.update(con, workers=1, log=QUIET)
            assert con.execute("select count(*) from sessions where agent='claude'").fetchone()[0] == 0
            assert s["sources"]["claude"] == "off", s
            (lvl, _, detail), = [r for r in health.check_discovery(con) if r[1] == "claude discovery"]
            assert lvl == health.GREEN and "isn't in use" in detail, (lvl, detail)
        con.close()


def test_missing_assistant_is_left_alone():
    """Someone with only Claude Code: Codex isn't found, nothing is removed, and health stays calm."""
    _reset_settings()
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        store.replace_session(con, {"agent": "codex", "session_id": "kept", "kind": "top", "log_path": "p",
                                    "turns": [], "files": {}})
        con.execute("insert into goals values ('kept', 'active', 'ship it', 1, '2026-10-01T00:00:00Z')")
        with patch.object(codex, "CODEX_HOME", os.path.join(tmp, "no-codex-here")), \
                patch.object(ingest, "ProcessPoolExecutor", _SyncPool):
            s = ingest.update(con, workers=1, log=QUIET)
            assert s["sources"]["codex"] == "not found", s
            assert con.execute("select count(*) from sessions where agent='codex'").fetchone()[0] == 1
            assert con.execute("select count(*) from goals").fetchone()[0] == 1 and not s["failures"], s
            levels = {name: lvl for lvl, name, _ in health.run(con)[1]}
            assert levels["codex discovery"] == health.GREEN and "codex cross-check" not in levels, levels
        con.close()


# ---------------------------------------------------------------- compressed logs

def test_compressed_logs_read_with_every_fallback():
    raw = open(os.path.join(FX, "codex_current.jsonl"), "rb").read()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rollout-2026-10-01T10-00-00-aaaaaaaa-0000-0000-0000-000000000001.jsonl.zst")
        if shutil.which("zstd"):
            open(path, "wb").write(subprocess.run(["zstd", "-cq"], input=raw, capture_output=True, check=True).stdout)
        else:
            try:
                from compression import zstd
                open(path, "wb").write(zstd.compress(raw))
            except ImportError:
                import zstandard
                open(path, "wb").write(zstandard.ZstdCompressor().compress(raw))
        want = "Add a health check to the demo app"
        tried = []
        blocks = [{}, {"compression": None, "compression.zstd": None}, {"compression": None,
                                                                         "compression.zstd": None, "zstandard": None}]
        for block in blocks:
            available = True
            with patch.dict(sys.modules, block):
                try:
                    r = codex.parse_thread([path])
                except RuntimeError as e:  # no reader at all on this machine for this route
                    assert "zstd" in str(e)
                    available = False
            if available:
                assert r["turns"][0]["request"] == want, r["turns"]
                tried.append(len(block))
        assert tried, "no zstd reader worked at all"


# ---------------------------------------------------------------- background job

def test_background_job_files_and_commands():
    _reset_settings()
    kind, files, on = schedule.enable(20, dry_run=True)
    assert kind in ("launchd", "systemd", "unsupported")
    if kind == "unsupported":
        return
    text = "".join(t for _, t in files)
    assert "update" in text and "--scheduled" in text and skills.program()[0] in text
    assert os.environ["SESSION_MEMORY_HOME"] in text and os.environ["SESSION_MEMORY_CONFIG"] in text
    assert ("1200" in text) if kind == "launchd" else ("20min" in text)
    assert not any(os.path.exists(p) for p, _ in files)  # a dry run writes nothing
    assert schedule.disable() == (kind, [], [])  # nothing installed: nothing done, no command run
    schedule.RAN.clear()
    schedule.enable(20)
    assert schedule.status()["installed"] and schedule.RAN, schedule.RAN
    schedule.RAN.clear()
    _, removed, _ = schedule.disable()
    assert removed and not schedule.status()["installed"] and schedule.RAN


def test_refresh_command_and_scheduled_runs_respect_the_mode():
    _reset_settings()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["refresh", "auto", "--every", "10"])
    if schedule.plan(10)[0] == "unsupported":
        return
    assert config.load(reload=True)["refresh"] == {"mode": "auto", "interval_minutes": 10}
    assert schedule.status()["installed"], out.getvalue()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["refresh", "off"])
    assert not schedule.status()["installed"]
    try:
        cli.main(["update"])
        raise AssertionError("update ran while refresh is off")
    except SystemExit as e:
        assert "off" in str(e)
    cli.main(["update", "--scheduled"])  # a leftover background job does nothing when the mode isn't auto
    assert not os.path.exists(store.DB_PATH)
    _reset_settings()


# ---------------------------------------------------------------- skills

def test_skills_install_remove_and_never_touch_others():
    _reset_settings()
    other = os.path.join(config.skills_dir("codex"), "smem")
    os.makedirs(other)
    open(os.path.join(other, "SKILL.md"), "w").write("someone else's skill")
    try:
        msgs = skills.sync()
        assert any("left" in m and "alone" in m for m in msgs), msgs
        assert open(os.path.join(other, "SKILL.md")).read() == "someone else's skill"
        mine = skills.folder("claude")
        body = open(os.path.join(mine, "SKILL.md")).read()
        assert body.startswith("---\nname: smem\n") and "allowed-tools:" in body and "{{" not in body
        front = body.split("---")[1]
        assert " search:*" in front and " update)" in front  # reading and a plain update run without asking
        for cmd in ("update", "uninstall", "settings", "setup", "refresh", "skills"):
            assert f" {cmd}:*" not in front, cmd  # changing the user's setup still asks
        assert "Bash(" not in skills.render("codex")
        assert skills.remove("codex")[0] is False and os.path.exists(other)
        config.set_value("skills.claude", "false")
        skills.sync()
        assert not os.path.exists(mine)
    finally:
        shutil.rmtree(other)
        _reset_settings()


# ---------------------------------------------------------------- the command, end to end

def _smem(*args, check=True):
    env = {k: os.environ[k] for k in list(ENV) + ["SESSION_MEMORY_DRY_SYSTEM"]}
    env.update(PATH=os.environ.get("PATH", "/usr/bin:/bin"), PYTHONPATH=SRC, TZ="UTC")
    r = subprocess.run([sys.executable, "-m", "session_memory", *args], env=env, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f"smem {' '.join(args)} exited {r.returncode}:\n{r.stdout}\n{r.stderr}")
    return r


def test_end_to_end_setup_read_only_use_and_uninstall():
    _reset_settings()
    seg = os.path.join(config.codex_home(), "sessions", "2026", "10", "01")
    os.makedirs(seg, exist_ok=True)
    shutil.copy(os.path.join(FX, "codex_current.jsonl"),
                os.path.join(seg, "rollout-2026-10-01T10-00-00-aaaaaaaa-0000-0000-0000-000000000001.jsonl"))
    _claude_log("/work/garden", "22222222-0000-0000-0000-000000000001", "water the tomatoes on Tuesday")
    try:
        plan = _smem("setup", "--refresh", "manual", "--skills", "all", "--dry-run").stdout
        assert "dry run" in plan and not os.path.exists(config.config_path()) and not os.path.exists(store.DB_PATH)
        out = _smem("setup", "--refresh", "manual", "--skills", "all").stdout
        assert "Self-test: 7 of 7" in out and "Ready." in out, out
        assert "Codex: 1 session read" in out and "Claude Code: 1 session read" in out, out
        # Reading works with the memory folder made unwritable, as inside an assistant's sandbox.
        os.chmod(store.DATA_DIR, stat.S_IRUSR | stat.S_IXUSR)
        try:
            hits = _smem("search", "tomatoes").stdout
            assert "[tomatoes]" in hits and "claude" in hits, hits
            assert "[health] check" in _smem("search", "health", "--agent", "codex").stdout
            assert "/tmp/demo/health.py" in _smem("files", "health.py").stdout
            rows = [json.loads(l) for l in _smem("sql", "select agent, count(*) n from sessions group by agent",
                                                 "--json").stdout.splitlines()]
            assert {r["agent"]: r["n"] for r in rows} == {"claude": 1, "codex": 1}, rows
            bad = _smem("sql", "delete from sessions", check=False)
            assert bad.returncode and "not authorized" in bad.stderr, bad
            assert "last update" in _smem("stats").stdout
            assert _smem("health", "--fast", check=False).returncode in (0, 1)
        finally:
            os.chmod(store.DATA_DIR, stat.S_IRWXU)
        time.sleep(1.1)  # the memory records its update time in whole seconds
        _claude_log("/work/garden", "22222222-0000-0000-0000-000000000002", "prune the roses")
        note = _smem("recent").stdout
        assert "Since then 0 Codex and 1 Claude Code sessions changed" in note, note
        _smem("refresh", "auto")  # the background job (recorded, not run) updated recently enough: no note
        assert "Since then" not in _smem("recent").stdout
        _smem("refresh", "off")  # off: the note states the fact without suggesting an update that would refuse
        assert "refresh is off in settings" in _smem("recent").stdout
        _smem("refresh", "manual")
        assert "Updated in" in _smem("update").stdout
        assert "Since then" not in _smem("recent").stdout
        for agent in ("claude", "codex"):
            assert os.path.exists(os.path.join(config.skills_dir(agent), "smem", "SKILL.md"))
        listing = _smem("uninstall", "--dry-run").stdout
        assert "memory" in listing and os.path.exists(store.DB_PATH)
        assert _smem("uninstall", check=False).returncode != 0  # no terminal and no --yes: nothing removed
        assert os.path.exists(store.DB_PATH)
        _smem("uninstall", "--yes")
        assert not os.path.exists(store.DATA_DIR) and not os.path.exists(config.config_path())
        for agent in ("claude", "codex"):
            assert not os.path.exists(os.path.join(config.skills_dir(agent), "smem"))
        assert os.path.exists(os.path.join(seg, "rollout-2026-10-01T10-00-00-aaaaaaaa-0000-0000-0000-000000000001.jsonl"))
    finally:
        shutil.rmtree(os.path.join(config.codex_home(), "sessions"))
        os.makedirs(os.path.join(config.codex_home(), "sessions"))
        shutil.rmtree(os.path.join(config.claude_home(), "projects"))
        os.makedirs(os.path.join(config.claude_home(), "projects"))
        _reset_settings()


def test_uninstall_keeps_files_it_did_not_create():
    os.makedirs(store.DATA_DIR, exist_ok=True)
    keep = os.path.join(store.DATA_DIR, "my-notes.txt")
    open(keep, "w").write("mine")
    sqlite3.connect(store.DB_PATH).close()
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["uninstall", "--yes"])
    assert os.path.exists(keep) and not os.path.exists(store.DB_PATH)
    os.remove(keep)


# ---------------------------------------------------------------- found in review (each fails without its fix)

def _update(con):
    with patch.object(ingest, "ProcessPoolExecutor", _SyncPool):
        return ingest.update(con, workers=1, log=QUIET)


def _codex_log(cwd, tid, ask, folder="sessions/2026/10/15"):
    raw = open(os.path.join(FX, "codex_current.jsonl")).read()
    raw = raw.replace("/tmp/demo", cwd).replace("Add a health check to the demo app", ask)
    raw = raw.replace("aaaaaaaa-0000-0000-0000-000000000001", tid)
    d = os.path.join(config.codex_home(), folder)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"rollout-2026-10-15T10-00-00-{tid}.jsonl")
    open(path, "w").write(raw)
    return path


def _clean_sources():
    for sub in ("sessions", "projects"):
        base = config.codex_home() if sub == "sessions" else config.claude_home()
        shutil.rmtree(os.path.join(base, sub), ignore_errors=True)
        os.makedirs(os.path.join(base, sub))


def test_broken_links_and_logs_that_move_mid_update():
    _reset_settings()
    try:
        real = _codex_log("/work/a", "c0dec0de-0000-4000-8000-000000000001", "first ask")
        links = os.path.join(config.codex_home(), "sessions", "copies")
        os.makedirs(links)
        os.symlink(real, os.path.join(links, os.path.basename(real)))  # a second route to the same log
        os.symlink("/nonexistent/rollout-2026-10-15T10-00-00-c0dec0de-0000-4000-8000-000000000009.jsonl",
                   os.path.join(links, "rollout-2026-10-15T10-00-00-c0dec0de-0000-4000-8000-000000000009.jsonl"))
        found = codex.scan_rollouts()
        assert list(found) == ["c0dec0de-0000-4000-8000-000000000001"] and len(found[list(found)[0]]) == 1, found
        with tempfile.TemporaryDirectory() as tmp:
            con = store.connect(os.path.join(tmp, "t.sqlite"))
            gone = {"c0dec0de-0000-4000-8000-000000000002": ["/nonexistent/rollout-x.jsonl"]}
            with patch.object(codex, "scan_rollouts", lambda: dict(found, **gone)):
                s = _update(con)  # a log that vanished after the scan is skipped, not fatal
            assert s.get("moved_during_update") == 1 and s["codex_parsed"] == 1, s
            con.close()
    finally:
        _clean_sources()


def test_shell_output_and_notices_are_not_stored_as_requests():
    _reset_settings()
    try:
        path = _claude_log("/work/b", "33333333-0000-0000-0000-000000000001", "deploy it")
        with open(path, "a") as f:
            for text in ("<bash-input>cat .env</bash-input><bash-stdout>DB_PASS=CANARY_771</bash-stdout><bash-stderr></bash-stderr>",
                         "<task-notification><task-id>x</task-id>CANARY_882 finished</task-notification>"):
                f.write(json.dumps({"type": "user", "cwd": "/work/b", "message": {"role": "user", "content": text}}) + "\n")
        r = claude.parse_log(path, "s", None)
        text = json.dumps(r["turns"])
        assert "CANARY_771" not in text and "CANARY_882" not in text and "cat .env" in text, r["turns"]
    finally:
        _clean_sources()


def test_exclusions_are_exact_complete_and_erased():
    _reset_settings()
    try:
        _claude_log("/work/acme", "44444444-0000-0000-0000-000000000001", "acme secret plans ZEBRAWORD")
        _claude_log("/work/acme/api", "44444444-0000-0000-0000-000000000002", "acme api ZEBRAWORD")
        _claude_log("/work/acme-tools", "44444444-0000-0000-0000-000000000003", "a sibling folder")
        _codex_log("/work/acme", "c0dec0de-0000-4000-8000-000000000003", "codex in acme ZEBRAWORD")
        _codex_log("/work/acme-tools", "c0dec0de-0000-4000-8000-000000000004", "codex in the sibling")
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.sqlite")
            con = store.connect(db)
            _update(con)
            assert con.execute("select count(*) from sessions").fetchone()[0] == 5
            exclude = "/WORK/Acme" if sys.platform == "darwin" else "/work/acme"  # macOS folders ignore case
            config.set_value("privacy.exclude_paths", json.dumps([exclude + "/"]))
            s = _update(con)  # the Codex thread is unchanged on disk: it must still go
            left = sorted(r[0] for r in con.execute("select cwd from sessions"))
            assert left == ["/work/acme-tools", "/work/acme-tools"], (left, s)
            con.close()
            raw = open(db, "rb").read() + (open(db + "-wal", "rb").read() if os.path.exists(db + "-wal") else b"")
            assert b"ZEBRAWORD" not in raw and b"zebraword" not in raw  # gone from the file, not only the index
        for bad in ('"~/work/acme"', '["work/acme"]', "[1, 2]"):
            try:
                config.set_value("privacy.exclude_paths", bad)
                raise AssertionError(f"accepted exclude_paths = {bad}")
            except SystemExit:
                pass
    finally:
        _clean_sources()
        _reset_settings()


def test_settings_file_mistakes_are_explained_not_crashes():
    for text, words in (("[privacy]\nexclude_path = []\n", "unknown setting privacy.exclude_path"),
                        ("[privacy]\nexclude_paths = \"~/x\"\n", "must be a list of text"),
                        ("[refresh\nmode = 1\n", "can't be read")):
        os.makedirs(os.path.dirname(config.config_path()), exist_ok=True)
        open(config.config_path(), "w").write(text)
        try:
            config.load(reload=True)
            raise AssertionError(f"accepted {text!r}")
        except SystemExit as e:
            assert words in str(e), (words, str(e))
    _reset_settings()


def test_sql_can_only_read():
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        store.replace_session(con, {"agent": "claude", "session_id": "s", "kind": "top", "log_path": "p",
                                    "turns": [{"request": "hello world", "answer": "hi"}], "files": {}})
        con.commit()
        con.close()
        ro = store.query_only(store.connect_readonly(os.path.join(tmp, "t.sqlite")))
        assert ro.execute("select snippet(turns_fts, 0, '[', ']', '…', 5) from turns_fts "
                          "where turns_fts match 'hello'").fetchone()[0] == "[hello] world"
        assert ro.execute("select count(*) from pragma_table_info('sessions')").fetchone()[0] > 5
        for q in (f"vacuum into '{tmp}/copy.sqlite'", f"attach '{tmp}/new.sqlite' as x", "delete from sessions",
                  "pragma query_only=off", "create temp table t(x)"):
            try:
                ro.execute(q)
                raise AssertionError(f"allowed: {q}")
            except sqlite3.Error:
                pass
        assert not {"copy.sqlite", "new.sqlite"} & set(os.listdir(tmp)), os.listdir(tmp)


def test_private_files_and_no_files_in_codex_folder():
    old = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            data = os.path.join(tmp, "data")
            con = store.connect(os.path.join(data, "memory.sqlite"))
            con.close()
            assert stat.S_IMODE(os.stat(data).st_mode) == 0o700
            assert stat.S_IMODE(os.stat(os.path.join(data, "memory.sqlite")).st_mode) == 0o600
            home = os.path.join(tmp, "codex")
            os.makedirs(home)
            w = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
            w.execute("pragma journal_mode=wal")
            w.execute("create table threads (id text)")
            w.commit()
            w.close()
            with patch.object(codex, "CODEX_HOME", home):
                codex.ro("state_5.sqlite").execute("select * from threads").fetchall()
            assert os.listdir(home) == ["state_5.sqlite"], os.listdir(home)
    finally:
        os.umask(old)
    config.save(config.load(reload=True))
    assert stat.S_IMODE(os.stat(config.config_path()).st_mode) == 0o600
    _reset_settings()


def test_background_job_pins_every_folder_and_escapes_paths():
    kind, files, _ = schedule.enable(15, dry_run=True)
    if kind == "unsupported":
        return
    text = "".join(t for _, t in files)
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "SESSION_MEMORY_HOME", "SESSION_MEMORY_CONFIG"):
        assert var in text, var
    assert ("<key>Umask</key><integer>63</integer>" in text) if kind == "launchd" else ("UMask=0077" in text)
    assert "-P" in text or "smem" in text  # never a bare `python -m`, which imports from the current folder
    assert schedule._quote('/a b/50%$x"y') == '"/a b/50%%$$x\\"y"'
    try:
        schedule._quote("/a\nb")
        raise AssertionError("a path with a line break was accepted")
    except SystemExit:
        pass


def test_skill_keeps_its_command_and_the_users_edits():
    _reset_settings()
    try:
        with patch.object(skills, "command", lambda: "smem"):
            skills.install("claude")
        path = skills.folder("claude")
        assert "command=smem" in open(os.path.join(path, skills.MARKER)).read()
        marker = os.path.join(path, skills.MARKER)
        old = open(marker).read()
        open(marker, "w").write(old.replace(skills.__version__, "0.0.1", 1))
        # An upgrade seen from a background job, whose PATH has no `smem`: the skill keeps `smem`.
        with patch.object(skills, "command", lambda: "/elsewhere/python -P -m session_memory"):
            skills.refresh_installed()
        body = open(os.path.join(path, "SKILL.md")).read()
        assert "Bash(smem search:*)" in body and "/elsewhere" not in body
        assert skills._marker(path)["version"] == skills.__version__
        # A SKILL.md the user edited is left alone by upgrades.
        open(os.path.join(path, "SKILL.md"), "a").write("\nmy own note\n")
        old = open(marker).read()
        open(marker, "w").write(old.replace(skills.__version__, "0.0.1", 1))
        skills.refresh_installed()
        assert "my own note" in open(os.path.join(path, "SKILL.md")).read()
        # Removing keeps files the user added to the folder.
        open(os.path.join(path, "NOTES.md"), "w").write("mine")
        skills.remove("claude")
        assert os.listdir(path) == ["NOTES.md"]
        shutil.rmtree(path)
    finally:
        _reset_settings()


def test_reading_commands_handle_punctuation_names_dates_and_nothing():
    _reset_settings()
    try:
        log = _claude_log("/work/c", "55555555-0000-0000-0000-000000000001", "edit config.toml for the Safari fix")
        with open(log, "a") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": "Checkout button bug"}) + "\n")
        _codex_log("/work/c", "c0dec0de-0000-4000-8000-000000000005", "rename the inventory table")
        out = _smem("setup", "--refresh", "manual", "--skills", "none").stdout
        assert "Health: GREEN" in out, out  # a healthy new install with only two sessions isn't a warning
        assert "[config.toml]" in _smem("search", "config.toml").stdout  # punctuation is searched literally
        some = _smem("search", "Safari", "fix", "zeppelin").stdout  # unquoted words; no turn has all three
        assert "some of them" in some and "[Safari]" in some, some
        named = _smem("search", "checkout bug").stdout  # words only in the session's name
        assert "Sessions named like this" in named and "Checkout button bug" in named, named
        assert "inventory table" in _smem("session", "inventory table").stdout  # unnamed: found by its request
        assert "No sessions in that time" in _smem("recent", "--since", "2030-01-01").stdout
        assert "c0dec0de" in _smem("recent", "--since", "2026-10-01", "--until", "2026-10-15").stdout
        assert "No Codex goals" in _smem("goals").stdout
        assert "(no rows)" in _smem("sql", "select * from goals").stderr
        assert "Session Memory" in _smem("--version").stdout
        assert "Reading" in _smem().stdout  # no command: the help, not an error
        _smem("uninstall", "--yes")
    finally:
        _clean_sources()
        _reset_settings()


# ---------------------------------------------------------------- keeping sessions after their logs are deleted

def _history_for_retention():
    """16 Claude Code sessions (one with a helper) and 5 Codex threads; returns the logs that get deleted."""
    main = _claude_log("/work/r", "66666666-0000-0000-0000-000000000001", "the KEEPWORD decision")
    helper_dir = os.path.join(os.path.dirname(main), "66666666-0000-0000-0000-000000000001", "subagents")
    os.makedirs(helper_dir)
    helper = os.path.join(helper_dir, "agent-a1.jsonl")
    shutil.copy(_claude_log("/work/tmp", "66666666-0000-0000-0000-0000000000ff", "HELPERWORD research"), helper)
    os.remove(os.path.join(os.path.dirname(main).replace("work-r", "work-tmp"), "66666666-0000-0000-0000-0000000000ff.jsonl"))
    for i in range(2, 16):
        _claude_log("/work/r", f"66666666-0000-0000-0000-0000000000{i:02d}", f"ordinary session {i}")
    codex_logs = [_codex_log("/work/r", f"c0dec0de-0000-4000-8000-00000000010{i}", f"codex ask {i} CODEXWORD{i}")
                  for i in range(5)]
    return main, helper, codex_logs[0]


def _kept(con):
    return {r[0] for r in con.execute("select session_id from sessions where log_deleted_at is not null")}


def test_deleted_logs_are_kept_as_settings_say():
    _reset_settings()
    try:
        main, helper, clog = _history_for_retention()
        main_id, helper_id = "66666666-0000-0000-0000-000000000001", "66666666-0000-0000-0000-000000000001/agent-a1"
        cid = "c0dec0de-0000-4000-8000-000000000100"
        saved = {p: open(p).read() for p in (main, helper, clog)}
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "memory.sqlite")
            con = store.connect(db)
            _update(con)
            assert con.execute("select count(*) from sessions").fetchone()[0] == 21
            for p in (main, helper, clog):
                os.remove(p)
            s = _update(con)  # default keep = all: nothing is forgotten, all three are marked
            assert _kept(con) == {main_id, helper_id, cid} and not s["failures"], (s, _kept(con))
            assert con.execute("select count(*) from turns_fts where turns_fts match 'keepword'").fetchone()[0] == 1
            levels = {name: (lvl, d) for lvl, name, d in health.check_discovery(con)}
            assert levels["claude discovery"][0] == health.GREEN and "2 kept" in levels["claude discovery"][1], levels
            # A full rebuild reads only the logs on disk, and still hands the kept sessions on.
            with patch.object(store, "DB_PATH", db), patch.object(ingest, "ProcessPoolExecutor", _SyncPool):
                cli.rebuild(con, workers=1, log=QUIET)
            assert _kept(con) == {main_id, helper_id, cid}, _kept(con)
            assert con.execute("select count(*) from turns_fts where turns_fts match 'codexword0'").fetchone()[0] == 1
            config.set_value("retention.keep", '"main"')  # keep main sessions, let helpers go
            _update(con)
            assert _kept(con) == {main_id, cid}, _kept(con)
            config.set_value("retention.keep", '"none"')  # already known to be gone: removed without the guard
            s = _update(con)
            assert _kept(con) == set() and not s["failures"], s
            assert con.execute("select count(*) from sessions").fetchone()[0] == 18
            config.set_value("retention.keep", '"all"')
            open(main, "w").write(saved[main])
            _update(con)
            os.remove(main)
            _update(con)
            assert _kept(con) == {main_id}
            open(main, "w").write(saved[main])  # the log comes back: the mark goes
            _update(con)
            assert _kept(con) == set(), _kept(con)
            con.close()
    finally:
        _clean_sources()
        _reset_settings()


def test_unplugged_drive_is_not_taken_for_deletion():
    _reset_settings()
    try:
        _history_for_retention()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "memory.sqlite")
            con = store.connect(db)
            _update(con)
            inventory = con.execute("select value from meta where key='inventory'").fetchone()[0]
            shutil.rmtree(os.path.join(config.claude_home(), "projects"))
            os.makedirs(os.path.join(config.claude_home(), "projects"))  # every Claude log gone at once
            s = _update(con)
            assert any(f["kind"] == "guard" for f in s["failures"]) and _kept(con) == set(), s
            assert con.execute("select value from meta where key='inventory'").fetchone()[0] == inventory
            levels = {name: lvl for lvl, name, _ in health.check_discovery(con)}
            assert levels["claude discovery"] == health.RED, levels
            with patch.object(store, "DB_PATH", db), patch.object(ingest, "ProcessPoolExecutor", _SyncPool):
                try:
                    cli.rebuild(con, workers=1, log=QUIET)
                    raise AssertionError("a rebuild with every Claude log missing was accepted")
                except SystemExit as e:
                    assert "missing 16" in str(e), e
            con.close()
    finally:
        _clean_sources()
        _reset_settings()


def test_forget_erases_for_good_and_can_be_undone():
    _reset_settings()
    try:
        main, helper, _ = _history_for_retention()
        _smem("setup", "--refresh", "manual", "--skills", "none")
        assert "matches" in _smem("forget", "66666666", "--yes", check=False).stdout  # ambiguous: refused
        assert _smem("forget", "6666", "--yes", check=False).returncode != 0  # too short: refused
        preview = _smem("forget", "66666666-0000-0000-0000-000000000001", "--dry-run").stdout
        assert "2 sessions" in preview, preview  # the session and the helper it started
        assert _smem("forget", "66666666-0000-0000-0000-000000000001", check=False).returncode != 0  # needs --yes
        _smem("forget", "66666666-0000-0000-0000-000000000001", "--yes")
        raw = open(store.DB_PATH, "rb").read()
        assert b"KEEPWORD" not in raw and b"HELPERWORD" not in raw
        _smem("update")  # the logs still exist; forgotten sessions stay out
        assert "No matches" in _smem("search", "KEEPWORD").stdout
        assert os.path.exists(main) and os.path.exists(helper)  # forgetting never touches the logs
        listed = _smem("forget", "--list").stdout
        assert "66666666-0000-0000-0000-000000000001" in listed and "agent-a1" in listed, listed
        assert "GREEN" in _smem("health", "--fast", check=False).stdout.splitlines()[0]
        _smem("forget", "--undo", "66666666-0000-0000-0000-000000000001")
        _smem("update")
        assert "[KEEPWORD]" in _smem("search", "KEEPWORD").stdout
        _smem("uninstall", "--yes")
    finally:
        _clean_sources()
        _reset_settings()


def test_workflow_helpers_are_found_and_new_logs_are_not_missed():
    _reset_settings()
    try:
        main = _claude_log("/work/w", "77777777-0000-0000-0000-000000000001", "start a workflow")
        run = os.path.join(os.path.dirname(main), "77777777-0000-0000-0000-000000000001", "subagents", "workflows", "wf_1")
        os.makedirs(run)
        shutil.copy(main, os.path.join(run, "agent-b1.jsonl"))
        open(os.path.join(run, "journal.jsonl"), "w").write('{"type": "started"}\n')  # bookkeeping, not a session
        ids = {sid for _, sid, _ in claude.list_logs()}
        assert ids == {"77777777-0000-0000-0000-000000000001", "77777777-0000-0000-0000-000000000001/agent-b1"}, ids
        with tempfile.TemporaryDirectory() as tmp:
            con = store.connect(os.path.join(tmp, "t.sqlite"))
            _update(con)
            time.sleep(1.1)
            _claude_log("/work/w", "77777777-0000-0000-0000-000000000002", "a session that started just now")
            levels = {name: (lvl, d) for lvl, name, d in health.check_discovery(con)}
            assert levels["claude discovery"][0] == health.GREEN, levels  # new since the update isn't "missed"
            assert health.check_freshness(con)[0][0] == health.YELLOW  # it's reported as changed instead
            con.close()
    finally:
        _clean_sources()
        _reset_settings()


if __name__ == "__main__":
    failed = 0
    try:
        for name, fn in list(globals().items()):
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
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(1 if failed else 0)
