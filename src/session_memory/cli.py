"""smem: one searchable memory of your Claude Code and Codex sessions, kept on this computer.

Reading (never writes anything)
  smem search "words"           full-text search over your requests and the assistants' answers
  smem session ID               one session: details, links, recent turns, files changed
  smem recent [--days N]        sessions active recently
  smem project NAME             a project's sessions, newest first
  smem files PATH               sessions that changed a matching file
  smem goals                    Codex goals
  smem links ID                 sessions connected to this one
  smem stats                    totals, and when the memory was last updated
  smem sql "SELECT ..."         any read-only query (`smem schema` lists the tables)

Keeping it current
  smem update                   read new and changed logs (only those)
  smem health                   checks that the memory is complete and current
  smem refresh [auto|manual|off]

Setup
  smem setup                    settings, skills, refresh, first build and checks, in one step
  smem settings [KEY VALUE]     show or change a setting
  smem skills [install|remove]  the Claude Code and Codex skills
  smem uninstall                remove everything smem added
"""
import argparse
import calendar
import contextlib
import json
import os
import sqlite3
import sys
import time

from . import __version__, config, health, ingest, schedule, skills, store

AGENT_CHOICES = ["codex", "claude"]


def local(ts):
    """UTC ISO -> local 'YYYY-MM-DD HH:MM' for display."""
    if not ts:
        return "?"
    try:
        epoch = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except ValueError:
        return ts[:16]


def since(days):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))


def one_line(s, n):
    s = " ".join((s or "").split())
    return s if n is None or len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- opening the memory

def reader():
    """The memory, read-only. Works where writing is blocked, such as an assistant's sandbox."""
    try:
        return store.connect_readonly()
    except FileNotFoundError:
        sys.exit("There's no memory yet. Run `smem setup` (or `smem update`) to build it.")
    except store.OutdatedStore:
        sys.exit("The memory was built by an older version of smem. Run `smem update` to rebuild it.")
    except sqlite3.Error as e:
        sys.exit(f"Couldn't open the memory at {store.DB_PATH}: {e}")


def writer():
    """(connection, needs_full_rebuild)."""
    try:
        return store.connect(), False
    except store.OutdatedStore:
        print("The memory was built by an older version; rebuilding it. The old one stays until the new one is complete.")
        return store.connect(allow_outdated=True), True
    except (OSError, sqlite3.Error) as e:
        sys.exit(f"Couldn't write to {store.DATA_DIR}: {e}. "
                 "Inside an assistant's sandbox, updating needs permission to write there.")


@contextlib.contextmanager
def update_lock(wait):
    """One update at a time. Yields False when another is running and we shouldn't wait."""
    try:
        import fcntl
    except ImportError:  # Windows: no lock; SQLite still keeps each write whole
        yield True
        return
    os.makedirs(store.DATA_DIR, exist_ok=True)
    with open(os.path.join(store.DATA_DIR, "update.lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                yield False
                return
            print("Another update is running; waiting for it to finish.", file=sys.stderr)
            fcntl.flock(f, fcntl.LOCK_EX)
        yield True


def resolve(con, q, agent=None):
    args = [q, q]
    sql = "select * from sessions where (session_id=? or session_id like ? || '%')"
    if agent:
        sql += " and agent=?"
        args.append(agent)
    rows = con.execute(sql + " order by updated_at desc limit 10", args).fetchall()
    if not rows:
        args = [f"%{q}%"] + ([agent] if agent else [])
        rows = con.execute("select * from sessions where name like ?" + (" and agent=?" if agent else "")
                           + " and kind='top' order by updated_at desc limit 10", args).fetchall()
    if not rows:
        sys.exit(f"No session matches {q!r}.")
    if len(rows) > 1 and not any(r["session_id"] == q or r["name"] == q for r in rows):
        print(f"{q!r} matches {len(rows)} sessions; showing the newest. Others:")
        for r in rows[1:]:
            print(f"  {r['agent']:6} {r['session_id']}  {local(r['updated_at'])}  {one_line(r['name'], 60)}")
    exact = [r for r in rows if r["session_id"] == q or r["name"] == q]
    return (exact or rows)[0]


def row_line(r):
    tag = r["agent"] + ("·h" if r["kind"] == "helper" else "")
    name = r["name"] or one_line(r["first_request"], 60) or "(unnamed)"
    return (f"{local(r['updated_at'])}  {tag:8} {one_line(name, 58):58}  "
            f"t{r['turn_count']:<4} c{('≈' if r['commands_estimated'] else '') + str(r['command_count']):<6} "
            f"f{r['file_change_count']:<4} {r['session_id'][:13]}")


# ---------------------------------------------------------------- reading

def cmd_search(a):
    con = reader()
    sql = """select f.agent, f.session_id, f.turn_index, s.name, s.updated_at, t.started_at,
                    snippet(turns_fts, 0, '[', ']', ' … ', 14) req, snippet(turns_fts, 1, '[', ']', ' … ', 18) ans
             from turns_fts f join sessions s on s.agent=f.agent and s.session_id=f.session_id
             join turns t on t.agent=f.agent and t.session_id=f.session_id and t.turn_index=f.turn_index
             where turns_fts match ?"""
    args = [a.query]
    if a.agent:
        sql += " and f.agent=?"
        args.append(a.agent)
    if not a.helpers:
        sql += " and s.kind='top'"
    if a.project:
        sql += " and (s.project like ? or s.cwd like ?)"
        args += [f"%{a.project}%"] * 2
    if a.days:
        sql += " and coalesce(t.started_at, s.updated_at) >= ?"
        args.append(since(a.days))
    sql += " order by bm25(turns_fts) limit ?"
    args.append(a.limit)
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Search couldn't read that query ({e}). Put words with punctuation inside double quotes, "
                 'e.g. smem search \'"config.toml" error\'.')
    if not rows:
        print("No matches. Search matches whole words (and their endings); try fewer or different words.")
    for r in rows:
        print(f"{local(r['started_at'] or r['updated_at'])}  {r['agent']:6} {one_line(r['name'], 50)}  "
              f"[{r['session_id'][:13]} turn {r['turn_index'] + 1}]")
        if r["req"].strip():
            print(f"    YOU:   {one_line(r['req'], 220)}")
        if r["ans"].strip():
            print(f"    AGENT: {one_line(r['ans'], 220)}")


def cmd_session(a):
    con = reader()
    s = resolve(con, a.id, a.agent)
    print(f"{s['name'] or '(unnamed)'}")
    print(f"  {s['agent']} {s['kind']}  id {s['session_id']}  origin {s['origin']}"
          f"{'  ARCHIVED' if s['archived'] else ''}")
    print(f"  project {s['project']}  cwd {s['cwd']}")
    print(f"  model {s['model']} {s['effort'] or ''}  branch {s['git_branch'] or '-'}  tokens {s['tokens_used'] or '-'}")
    est = f" (≈{s['commands_estimated']} estimated from code-mode source)" if s["commands_estimated"] else ""
    print(f"  {local(s['created_at'])} → {local(s['updated_at'])}  turns {s['turn_count']}  commands {s['command_count']}{est}"
          f"  files {s['file_change_count']}  MCP {s['mcp_count']}  helpers {s['helper_count']}")
    if s["inherited_turns"]:
        print(f"  {s['inherited_turns']} leading turns copied from the parent thread are not shown as this session's work")
    if s["helper_role"]:
        print(f"  helper role {s['helper_role']}")
    if s["parent_id"]:
        p = con.execute("select name from sessions where session_id=?", (s["parent_id"],)).fetchone()
        print(f"  {s['parent_rel'] or 'parent'} of {s['parent_id']}  {one_line(p['name'] if p else '', 60)}")
    g = con.execute("select status, objective from goals where thread_id=?", (s["session_id"],)).fetchone()
    if g:
        print(f"  goal [{g['status']}] {one_line(g['objective'], 160)}")
    for l in con.execute("""select * from links where (from_id=? or to_id=?) and kind not in ('helper')""",
                         (s["session_id"], s["session_id"])):
        other = l["to_id"] if l["from_id"] == s["session_id"] else l["from_id"]
        print(f"  link: {l['kind']} {'→' if l['from_id'] == s['session_id'] else '←'} {other}")
    nh = con.execute("select count(*) from sessions where parent_id=? and parent_rel='helper'",
                     (s["session_id"],)).fetchone()[0]
    if nh:
        print(f"  {nh} helper sessions (smem links {s['session_id'][:13]})")
    if s["inherited_context"] and not s["first_request"]:
        print(f"  inherited context (from parent, not this helper's own ask): {one_line(s['inherited_context'], 200)}")
    turns = con.execute("select * from turns where agent=? and session_id=? order by turn_index",
                        (s["agent"], s["session_id"])).fetchall()
    shown = turns[-a.turns:] if a.turns else turns
    clip = None if a.full else 500
    for t in shown:
        print(f"\n--- turn {t['turn_index'] + 1}/{len(turns)}  {local(t['started_at'])}  "
              f"commands {t['commands']}  files {t['files']}")
        if t["request"]:
            print("  YOU:   " + (t["request"] if a.full else one_line(t["request"], clip)))
        if t["answer"]:
            print("  AGENT: " + (t["answer"] if a.full else one_line(t["answer"], clip * 2)))
        if t["aborted"]:
            print("  (turn was cancelled)")
        if t["rewound"]:
            print("  (on a branch Codex later rewound past: this happened, but isn't on the thread's visible history)")
        if t["truncated"] and a.full:
            print("  (text longer than 200,000 characters was cut; the full text is in the original log)")
    files = con.execute("select path, change from files_touched where agent=? and session_id=? order by path",
                        (s["agent"], s["session_id"])).fetchall()
    if files:
        print(f"\nFiles touched ({len(files)}):")
        for f in files[:40]:
            print(f"  {f['change'] or '':7} {f['path']}")
        if len(files) > 40:
            print(f"  … and {len(files) - 40} more (smem sql lists them all)")


def cmd_recent(a):
    con = reader()
    sql = "select * from sessions where updated_at>=?" + ("" if a.helpers else " and kind='top'")
    args = [since(a.days)]
    if a.agent:
        sql += " and agent=?"
        args.append(a.agent)
    for r in con.execute(sql + " order by updated_at desc limit ?", args + [a.limit]):
        print(row_line(r))


def cmd_project(a):
    con = reader()
    rows = con.execute("""select * from sessions where (project like ? or cwd like ?) and kind='top'
                          order by updated_at desc limit ?""", (f"%{a.name}%", f"%{a.name}%", a.limit)).fetchall()
    if not rows:
        sys.exit(f"No sessions for a project matching {a.name!r}.")
    projects = sorted({r["project"] for r in rows if r["project"]})
    print(f"{len(rows)} sessions in {', '.join(projects[:6])}")
    for r in rows:
        print(row_line(r))
        if a.answers and r["last_answer"]:
            print(f"      last answer: {one_line(r['last_answer'], 200)}")


def cmd_files(a):
    con = reader()
    rows = con.execute("""select f.path, f.change, f.turn_index, s.* from files_touched f
                          join sessions s on s.agent=f.agent and s.session_id=f.session_id
                          where f.path like ? order by s.updated_at desc limit ?""",
                       (f"%{a.path}%", a.limit)).fetchall()
    if not rows:
        sys.exit(f"No session changed a file matching {a.path!r}.")
    for r in rows:
        turn = f" turn {r['turn_index'] + 1}" if r["turn_index"] is not None else ""
        print(f"{row_line(r)}\n      {r['change'] or 'changed':7} {r['path']}{turn}")


def cmd_goals(a):
    con = reader()
    sql = "select g.*, s.name from goals g left join sessions s on s.session_id=g.thread_id"
    args = []
    if a.status:
        sql += " where g.status=?"
        args.append(a.status)
    for r in con.execute(sql + " order by g.updated_at desc", args):
        print(f"[{r['status']:8}] {local(r['updated_at'])}  {one_line(r['name'], 50):50}  {r['thread_id'][:13]}")
        print(f"           {one_line(r['objective'], 180)}")


def cmd_links(a):
    con = reader()
    s = resolve(con, a.id)
    sid = s["session_id"]
    print(f"{s['name'] or sid}")
    for l in con.execute("select * from links where from_id=? or to_id=? order by kind", (sid, sid)):
        other = l["to_id"] if l["from_id"] == sid else l["from_id"]
        o = con.execute("select agent, name, updated_at from sessions where session_id=?", (other,)).fetchone()
        arrow = "→" if l["from_id"] == sid else "←"
        print(f"  {l['kind']:10} {arrow} {(o['agent'] if o else '?'):6} {other}  "
              f"{one_line(o['name'] if o else '', 50)}  ({l['confidence']})")


def cmd_stats(a):
    con = reader()
    for r in con.execute("""select agent, kind, count(*) n, sum(turn_count) turns, sum(command_count) cmds,
                            sum(file_change_count) files, min(created_at) first, max(updated_at) last
                            from sessions group by agent, kind order by agent, kind"""):
        print(f"{r['agent']:6} {r['kind']:6} {r['n']:6} sessions  {r['turns'] or 0:7} turns  {r['cmds'] or 0:8} commands"
              f"  {r['files'] or 0:6} files  {local(r['first'])} → {local(r['last'])}")
    for k in ("links", "goals"):
        print(f"{k}: {con.execute(f'select count(*) from {k}').fetchone()[0]}")
    last = con.execute("select value from meta where key='last_update'").fetchone()
    mode = config.load()["refresh"]
    every = f" every {mode['interval_minutes']} minutes" if mode["mode"] == "auto" else ""
    print(f"last update: {local(last[0]) if last else 'never'}   refresh: {mode['mode']}{every}   memory: {store.DB_PATH}")


def cmd_sql(a):
    con = reader()
    try:
        cur = con.execute(a.query)
    except sqlite3.Error as e:
        sys.exit(f"SQL error: {e}")
    cols = [d[0] for d in cur.description or []]
    n = 0
    for row in cur:
        if a.json:
            print(json.dumps(dict(zip(cols, tuple(row))), ensure_ascii=False))
        else:
            if n == 0:
                print("\t".join(cols))
            print("\t".join("" if v is None else str(v).replace("\t", " ").replace("\n", " ") for v in row))
        n += 1
        if a.limit and n >= a.limit:
            print(f"(stopped at {a.limit} rows; --limit 0 shows all)", file=sys.stderr)
            break


def cmd_schema(a):
    print(store.SCHEMA.strip())


# ---------------------------------------------------------------- keeping it current

def cmd_update(a):
    mode = config.load()["refresh"]["mode"]
    if a.scheduled and mode != "auto":
        return  # settings were changed by hand; the background job does nothing until it's removed
    if mode == "off" and not a.force:
        sys.exit("Refresh is off in your settings, so the memory isn't updated. "
                 "`smem refresh manual` turns updates back on; `smem update --force` runs one anyway.")
    if a.scheduled:
        _rotate_log()
    with update_lock(wait=not a.scheduled) as got:
        if not got:
            return
        con, outdated = writer()
        log = (lambda *_a, **_k: None) if (a.quiet or a.scheduled or a.json) else print
        try:
            if a.full or outdated:
                s = rebuild(con, workers=a.workers, accept_removals=a.accept_removals, log=log)
            else:
                s = ingest.update(con, workers=a.workers, accept_removals=a.accept_removals, log=log)
        finally:
            con.close()
    try:
        skills.refresh_installed()
    except OSError:
        pass  # e.g. the skill folder isn't writable here; the next update tries again
    if a.json:
        print(json.dumps(s, indent=1))
    elif a.scheduled:
        print(f"{time.strftime('%Y-%m-%d %H:%M')}  {summary(s)}")
    else:
        print(summary(s))
        for f in s["failures"][:5]:
            print(f"  problem: {f.get('path')}: {f.get('error')}")


def summary(s):
    parts = []
    for agent, label in (("codex", "Codex"), ("claude", "Claude Code")):
        state = s.get("sources", {}).get(agent, "on")
        if state != "on":
            parts.append(f"{label}: {state}")
            continue
        n = s.get(f"{agent}_parsed", 0)
        parts.append(f"{label}: {n} session{'' if n == 1 else 's'} read, {s.get(f'{agent}_unchanged', 0)} unchanged")
    removed = sum(v for k, v in s.items() if k.endswith("_removed") and isinstance(v, int))
    if removed:
        parts.append(f"{removed} removed (excluded, turned off, or their logs are gone)")
    if s["failures"]:
        n = len(s["failures"])
        parts.append(f"{n} problem{'' if n == 1 else 's'} (smem health explains)")
    return f"Updated in {s.get('seconds', '?')}s. " + ". ".join(parts) + "."


def _rotate_log():
    path = os.path.join(store.DATA_DIR, "refresh.log")
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        os.replace(path, path + ".1")  # keeps one older log; replaces the previous older one


def rebuild(con, workers=None, accept_removals=False, log=print):
    """Rebuild the whole memory from the sources without ever putting the current one at risk.

    The rebuild is written to a separate file. It replaces the current content only if every source
    log was read and no more than 20% of the stored sessions went missing (unless removals are
    accepted). The replacement uses SQLite's backup, which copies the new database into the live one
    as a single transaction: open readers stay consistent, and a crash leaves either the old content
    or the new, never a mix."""
    tmp = store.DB_PATH + ".rebuild"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)  # an earlier failed rebuild's scratch copy, never the live memory
    new = store.connect(tmp)
    try:
        acked = con.execute("select value from meta where key='acked_tools'").fetchone()
        if acked:
            new.execute("insert or replace into meta values ('acked_tools', ?)", (acked[0],))
        try:
            s = ingest.update(new, full=True, workers=workers, accept_removals=accept_removals, previous=con, log=log)
        except Exception as e:
            sys.exit(f"Rebuild stopped; the current memory is unchanged. {type(e).__name__}: {e}")
        unread = [f for f in s["failures"] if f.get("kind") == "parse"]
        if unread:
            # A log that couldn't be read would be missing from the new memory; removal permission
            # doesn't cover read failures.
            sys.exit(f"Rebuild stopped; the current memory is unchanged. {len(unread)} logs could not be read, "
                     f"e.g. {unread[0]['path']}: {unread[0]['error']}")
        new.commit()
        con.commit()  # the live memory must have no open transaction for the backup
        new.backup(con)  # one transaction on the live memory
    finally:
        new.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    return s


def cmd_health(a):
    if a.selftest:
        outcomes = health.selftest()  # built-in sample logs in throwaway stores; your memory isn't touched
        for ok, msg, res in outcomes:
            print(("PASS " if ok else "FAIL ") + msg)
            if not ok or a.verbose:
                for lvl, name, detail in res:
                    print(f"     {lvl:6} {name}: {detail}")
        print(f"{sum(o[0] for o in outcomes)} of {len(outcomes)} self-test cases passed.")
        sys.exit(0 if all(o[0] for o in outcomes) else 1)
    if a.ack:
        con, _ = writer()
        acked = set(json.loads((con.execute("select value from meta where key='acked_tools'").fetchone() or ["[]"])[0]))
        acked.update(a.ack)
        con.execute("insert or replace into meta values ('acked_tools', ?)", (json.dumps(sorted(acked)),))
        con.commit()
        con.close()
        print(f"Marked as reviewed: {', '.join(sorted(a.ack))}")
    con = reader()
    overall, results = health.run(con, crosscheck=not a.fast)
    if a.json:
        print(json.dumps({"overall": overall, "checks": [{"level": l, "check": n, "detail": d} for l, n, d in results]},
                         indent=1))
    else:
        print(f"Session Memory health: {overall}")
        for lvl, name, detail in results:
            print(f"  {lvl:6} {name}: {detail}")
    sys.exit(health.RANK[overall])


def cmd_refresh(a):
    cfg = config.load()
    if a.mode is None and a.every is None:
        st = schedule.status()
        mode = cfg["refresh"]["mode"]
        print(f"refresh: {mode}" + (f", every {cfg['refresh']['interval_minutes']} minutes" if mode == "auto" else ""))
        print(f"background job: {'installed' if st['installed'] else 'not installed'} ({st['system']})")
        for p in st["files"]:
            print(f"  {p}")
        if mode == "auto" and not st["installed"]:
            print("The background job is missing; `smem refresh auto` puts it back.")
        if a.show:
            for path, text in schedule.plan(cfg["refresh"]["interval_minutes"])[1]:
                print(f"\n# {path}\n{text}")
        return
    if a.mode is not None:
        cfg["refresh"]["mode"] = a.mode
    if a.every is not None:
        cfg["refresh"]["interval_minutes"] = a.every
    config.validate(cfg)
    for line in apply_refresh(cfg, dry_run=a.dry_run):
        print(line)
    if not a.dry_run:
        config.save(cfg)


def apply_refresh(cfg, dry_run=False):
    """Install or remove the background job so it matches cfg. Returns what was done, in words."""
    mode, minutes = cfg["refresh"]["mode"], cfg["refresh"]["interval_minutes"]
    out = []
    if mode == "auto":
        kind, files, _ = schedule.enable(minutes, dry_run=dry_run)
        verb = "would write" if dry_run else "wrote"
        out += [f"Refresh: auto, every {minutes} minutes ({kind})."] + [f"  {verb} {p}" for p, _ in files]
    else:
        kind, removed, _ = schedule.disable(dry_run=dry_run)
        verb = "would remove" if dry_run else "removed"
        out.append(f"Refresh: {mode}." + (" Updates happen only when `smem update` runs." if mode == "manual"
                                          else " Nothing updates; search still works on what's stored."))
        out += [f"  {verb} {p}" for p in removed]
    return out


def cmd_settings(a):
    if a.key is None:
        cfg = config.load()
        print(f"settings file: {config.config_path()}{'' if os.path.exists(config.config_path()) else ' (not created yet; defaults apply)'}")
        print(f"memory:        {store.DB_PATH}")
        for section, values in cfg.items():
            for k, v in values.items():
                print(f"  {section}.{k} = {config._toml_value(v)}")
        return
    if a.value is None:
        sys.exit("Give a value too, e.g. smem settings refresh.interval_minutes 30")
    if a.dry_run:
        print(f"Would set {a.key} = {a.value} in {config.config_path()}")
        return
    path = config.set_value(a.key, a.value)
    print(f"Set {a.key}. Saved in {path}")
    section = a.key.partition(".")[0]
    cfg = config.load()
    if section == "refresh":
        for line in apply_refresh(cfg):
            print(line)
    elif section == "skills":
        for line in skills.sync():
            print(line)
    elif section in ("sources", "privacy"):
        print("This takes effect at the next update (`smem update`).")


def cmd_skills(a):
    if a.action == "install":
        for agent in _agents(a.only):
            print(skills.install(agent, dry_run=a.dry_run)[1])
        return
    if a.action == "remove":
        for agent in _agents(a.only):
            print(skills.remove(agent, dry_run=a.dry_run)[1])
        return
    for agent, st in skills.status().items():
        state = ("installed" if st["installed"] else "a different skill with this name is there" if st["foreign"]
                 else "not installed")
        found = "" if st["assistant_found"] else " (assistant not found on this computer)"
        print(f"{skills.LABELS[agent]:12} {state}{found}: {st['folder']}")


def _agents(only):
    return [only] if only else list(skills.AGENTS)


# ---------------------------------------------------------------- setup and removal

def cmd_setup(a):
    """Everything in one step, with flags so an assistant can run it for the user."""
    cfg = config.load(reload=True)
    found = {agent: skills.assistant_found(agent) for agent in skills.AGENTS}
    if a.sources is not None:
        chosen = _list(a.sources)
        for agent in skills.AGENTS:
            cfg["sources"][agent] = agent in chosen
    if a.refresh:
        cfg["refresh"]["mode"] = a.refresh
    if a.every:
        cfg["refresh"]["interval_minutes"] = a.every
    if a.skills is not None:
        chosen = _list(a.skills)
        for agent in skills.AGENTS:
            cfg["skills"][agent] = agent in chosen
    for p in a.exclude or []:
        if p not in cfg["privacy"]["exclude_paths"]:
            cfg["privacy"]["exclude_paths"].append(p)
    config.validate(cfg)

    print(f"smem {__version__} setup{' (dry run: nothing is changed)' if a.dry_run else ''}")
    print(f"  Claude Code: {'found' if found['claude'] else 'not found'}; Codex: {'found' if found['codex'] else 'not found'}")
    if not any(found.values()):
        print("  Neither assistant's history was found. smem reads ~/.claude/projects and ~/.codex "
              "(change with `smem settings sources.claude_home PATH` / `sources.codex_home PATH`).")
    print(f"  settings: {config.config_path()}")
    print(f"  memory:   {store.DB_PATH}")
    if a.dry_run:
        print("  would save these settings:")
        for section, values in cfg.items():
            for k, v in values.items():
                print(f"    {section}.{k} = {config._toml_value(v)}")
    else:
        config.save(cfg)
        config.apply(cfg)
    for line in skills.sync(dry_run=a.dry_run):
        print("  " + line)
    if a.dry_run or a.no_build:
        _setup_refresh(cfg, a.dry_run)
        return
    print("Building the memory (the first time can take a few minutes)…")
    with update_lock(wait=True):
        con, outdated = writer()
        try:
            s = (rebuild(con, log=lambda *_a, **_k: None) if outdated
                 else ingest.update(con, log=lambda *_a, **_k: None))
        finally:
            con.close()
    print("  " + summary(s))
    outcomes = health.selftest()
    passed = sum(o[0] for o in outcomes)
    print(f"Self-test: {passed} of {len(outcomes)} cases passed.")
    con = reader()
    overall, results = health.run(con, crosscheck=True)
    print(f"Health: {overall}")
    for lvl, name, detail in results:
        if lvl != health.GREEN:
            print(f"  {lvl:6} {name}: {detail}")
    _setup_refresh(cfg, False)
    print('Ready. Try: smem recent   or   smem search "a few words"')


def _setup_refresh(cfg, dry_run):
    try:
        lines = apply_refresh(cfg, dry_run=dry_run)
    except SystemExit as e:  # e.g. no launchd/systemd here: fall back to manual rather than stop setup
        cfg["refresh"]["mode"] = "manual"
        config.save(cfg)
        lines = [str(e), "Refresh set to manual."]
    for line in lines:
        print("  " + line)


def _list(raw):
    items = {x.strip().lower() for x in raw.split(",") if x.strip()}
    if items <= {"none", ""}:
        return set()
    if items == {"all"}:
        return set(skills.AGENTS)
    bad = items - set(skills.AGENTS)
    if bad:
        sys.exit(f"Unknown choice {', '.join(sorted(bad))}: use claude, codex, all or none.")
    return items


def cmd_uninstall(a):
    plan = []
    for agent in skills.AGENTS:
        path = skills.folder(agent)
        if skills.ours(path):
            plan.append(("skill", path))
    st = schedule.status()
    if st["files"]:
        plan.append(("background job", ", ".join(st["files"])))
    if os.path.exists(config.config_path()):
        plan.append(("settings", config.config_path()))
    if not a.keep_data and os.path.isdir(store.DATA_DIR):
        plan.append(("memory", store.DATA_DIR))
    if not plan:
        print("Nothing to remove.")
    else:
        print("smem uninstall removes:")
        for what, path in plan:
            print(f"  {what:15} {path}")
        print("Your Claude Code and Codex history is not touched.")
        if a.dry_run:
            return
        if not a.yes:
            if not sys.stdin.isatty():
                sys.exit("Run again with --yes to remove these.")
            if input("Remove them? [y/N] ").strip().lower() not in ("y", "yes"):
                sys.exit("Nothing removed.")
        for agent in skills.AGENTS:
            skills.remove(agent)
        schedule.disable()
        if os.path.exists(config.config_path()):
            os.remove(config.config_path())
            with contextlib.suppress(OSError):
                os.rmdir(os.path.dirname(config.config_path()))  # only if empty
        if not a.keep_data and os.path.isdir(store.DATA_DIR):
            _remove_data_dir()
        print("Removed.")
    print("To remove the program itself: `uv tool uninstall session-memory` or `pipx uninstall session-memory`.")


def _remove_data_dir():
    """Delete only the files smem creates, then the folder if it's empty, so nothing else is ever lost."""
    names = {store.DB_PATH, store.DB_PATH + ".rebuild"}
    known = {n + s for n in names for s in ("", "-wal", "-shm")}
    known |= {os.path.join(store.DATA_DIR, n) for n in ("update.lock", "refresh.log", "refresh.log.1")}
    for path in known:
        if os.path.exists(path):
            os.remove(path)
    try:
        os.rmdir(store.DATA_DIR)
    except OSError:
        print(f"  left {store.DATA_DIR} in place: it holds files smem didn't create")


# ---------------------------------------------------------------- arguments

def parser():
    ap = argparse.ArgumentParser(prog="smem", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"smem {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def cmd(name, fn, help):
        p = sub.add_parser(name, help=help, description=help)
        p.set_defaults(fn=fn)
        return p

    p = cmd("search", cmd_search, "full-text search over your requests and the assistants' answers")
    p.add_argument("query", help='words (all must match), "an exact phrase", OR, NOT, prefix*')
    p.add_argument("--agent", choices=AGENT_CHOICES)
    p.add_argument("--project", help="only sessions whose project or folder contains this")
    p.add_argument("--days", type=float, help="only turns from the last N days")
    p.add_argument("--helpers", action="store_true", help="include helper (sub-agent) sessions")
    p.add_argument("--limit", type=int, default=15)

    p = cmd("session", cmd_session, "one session: details, links, recent turns, files changed")
    p.add_argument("id", help="a session id, its first characters, or part of its name")
    p.add_argument("--agent", choices=AGENT_CHOICES)
    p.add_argument("--turns", type=int, default=5, help="how many recent turns to show (0 = all)")
    p.add_argument("--full", action="store_true", help="show whole requests and answers")

    p = cmd("recent", cmd_recent, "sessions active recently, newest first")
    p.add_argument("--days", type=float, default=3)
    p.add_argument("--agent", choices=AGENT_CHOICES)
    p.add_argument("--helpers", action="store_true", help="include helper (sub-agent) sessions")
    p.add_argument("--limit", type=int, default=40)

    p = cmd("project", cmd_project, "a project's sessions, newest first")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--answers", action="store_true", help="add each session's last answer")

    p = cmd("files", cmd_files, "sessions that changed a file whose path contains PATH")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=30)

    p = cmd("goals", cmd_goals, "Codex goals")
    p.add_argument("--status", help="e.g. active, complete, blocked")

    p = cmd("links", cmd_links, "sessions connected to this one (helpers, forks, mentions)")
    p.add_argument("id")

    cmd("stats", cmd_stats, "totals, and when the memory was last updated")

    p = cmd("sql", cmd_sql, "run a read-only SQL query on the memory")
    p.add_argument("query")
    p.add_argument("--json", action="store_true", help="one JSON object per row")
    p.add_argument("--limit", type=int, default=1000, help="stop after N rows (0 = no limit)")

    cmd("schema", cmd_schema, "the memory's tables, with a note on each column")

    p = cmd("update", cmd_update, "read new and changed logs into the memory")
    p.add_argument("--full", action="store_true", help="re-read every log (the current memory stays until it's done)")
    p.add_argument("--force", action="store_true", help="update even when refresh is off")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="print the full statistics")
    p.add_argument("--workers", type=int, help=argparse.SUPPRESS)
    p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--accept-removals", action="store_true",
                   help="allow more than 20%% of sessions to disappear (only when their logs were really removed)")

    p = cmd("health", cmd_health, "checks that the memory is complete and current")
    p.add_argument("--fast", action="store_true", help="skip the comparison with Codex's own index")
    p.add_argument("--selftest", action="store_true", help="prove the checks on built-in sample logs")
    p.add_argument("--verbose", action="store_true", help="with --selftest, show every check's detail")
    p.add_argument("--json", action="store_true")
    p.add_argument("--ack", nargs="+", metavar="TOOL", help="mark new tool names as reviewed")

    p = cmd("refresh", cmd_refresh, "show or set how the memory stays current: auto, manual or off")
    p.add_argument("mode", nargs="?", choices=["auto", "manual", "off"])
    p.add_argument("--every", type=int, metavar="MINUTES", help="how often auto refreshes (at least 5)")
    p.add_argument("--show", action="store_true", help="print the background job's file")
    p.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")

    p = cmd("settings", cmd_settings, "show settings, or change one: smem settings KEY VALUE")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--dry-run", action="store_true")

    p = cmd("skills", cmd_skills, "show, install or remove the Claude Code and Codex skills")
    p.add_argument("action", nargs="?", choices=["install", "remove"])
    p.add_argument("--only", choices=AGENT_CHOICES)
    p.add_argument("--dry-run", action="store_true")

    p = cmd("setup", cmd_setup, "settings, skills, refresh, first build and checks, in one step")
    p.add_argument("--refresh", choices=["auto", "manual", "off"], help="how the memory stays current (default: keep current)")
    p.add_argument("--every", type=int, metavar="MINUTES", help="how often auto refreshes (default 15)")
    p.add_argument("--skills", metavar="LIST", help="claude,codex | all | none (default: keep current)")
    p.add_argument("--sources", metavar="LIST", help="whose history to read: claude,codex | all (default: keep current)")
    p.add_argument("--exclude", action="append", metavar="PATH", help="never store sessions run inside PATH (repeatable)")
    p.add_argument("--no-build", action="store_true", help="save the choices but don't build the memory yet")
    p.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")

    p = cmd("uninstall", cmd_uninstall, "remove the skills, background job, settings and memory")
    p.add_argument("--keep-data", action="store_true", help="keep the memory itself")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--dry-run", action="store_true", help="list what would be removed")
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    config.apply()
    a.fn(a)


if __name__ == "__main__":
    main()
