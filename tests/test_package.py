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
    assert "update" in text and "--scheduled" in text and sys.executable in text
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
        assert " search:*" in front and " update:*" in front  # reading and updating run without asking
        for cmd in ("uninstall", "settings", "setup", "refresh", "skills"):
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
            assert bad.returncode and "readonly" in (bad.stderr + bad.stdout).lower().replace(" ", ""), bad
            assert "last update" in _smem("stats").stdout
            assert _smem("health", "--fast", check=False).returncode in (0, 1)
        finally:
            os.chmod(store.DATA_DIR, stat.S_IRWXU)
        time.sleep(1.1)  # the memory records its update time in whole seconds
        _claude_log("/work/garden", "22222222-0000-0000-0000-000000000002", "prune the roses")
        note = _smem("recent").stdout
        assert "Since then 0 Codex and 1 Claude Code sessions changed" in note, note
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
