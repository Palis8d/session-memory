"""`smem update`: bring the store up to date with Codex and Claude, re-parsing only logs that changed."""
import calendar
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import re
import sqlite3
import sys

from . import claude, codex, config, store

# Refuse to delete vanished sessions when an inventory shrinks this much at once: more likely a
# transient read failure or an unmounted drive than a real deletion.
MAX_VANISH_SHARE = 0.2


def _parse_codex(item):
    tid, paths = item
    try:
        return tid, codex.parse_thread(paths), None
    except Exception as e:  # one unreadable log must not stop the update
        return tid, None, f"{type(e).__name__}: {e}"


def _parse_claude(args):
    path, sid, parent = args
    try:
        return args, claude.parse_log(path, sid, parent), None
    except Exception as e:
        return args, None, f"{type(e).__name__}: {e}"


def _sig(paths):
    """None when a log vanished since the scan (Codex moves logs when it archives a session): skipped this
    run, read at its new place next time."""
    try:
        return _sig_of(paths)
    except FileNotFoundError:
        return None


def _sig_of(paths):
    # The adapter version is part of the signature, so a parsing change re-reads every log.
    return f"v{codex.ADAPTER_VERSION}|" + "|".join(codex.log_sig(p) for p in paths)


def _stored_sigs(con, agent):
    return {r["session_id"]: r["log_sig"] for r in
            con.execute("select session_id, log_sig from sessions where agent=?", (agent,))}


def _refresh_metadata(con, rec):
    """Source-owned metadata changes (renames, archiving, moves) without the log content changing."""
    # Codex's thread table owns these fields, so they are assigned as-is (a removed value clears).
    # A parent link clears too when it came from the table; one read from the log is kept.
    parent = rec["parent_id"]
    con.execute("""update sessions set name=?, project=?, cwd=?, origin=?, kind=?,
                   parent_id = case when ? is not null then ? when parent_source='table' then null else parent_id end,
                   parent_rel = case when ? is not null then ? when parent_source='table' then null else parent_rel end,
                   parent_source = case when ? is not null then 'table' when parent_source='table' then null
                                   else parent_source end,
                   helper_role=?, archived=?, updated_at=?, tokens_used=?,
                   model=?, effort=?, git_branch=?, log_path=coalesce(?, log_path)
                   where agent='codex' and session_id=?""",
                (store.clean(rec["name"]), rec["project"], rec["cwd"], rec["origin"], rec["kind"],
                 parent, parent, parent, rec["parent_rel"], parent,
                 store.clean(rec["helper_role"]), rec["archived"],
                 rec["updated_at"], rec["tokens_used"], rec["model"], rec["effort"], store.clean(rec["git_branch"]),
                 rec.get("log_path"), rec["session_id"]))


def _how(agent, updated_at, cleanup):
    """Why a log is probably gone. Claude Code deletes logs whose last activity is older than its cleanup
    period, so an older log looks automatic; a younger one, or any Codex log (Codex never deletes on its own),
    looks deleted by hand. A best guess: health shows the evidence, and the user decides what it means."""
    if agent == "claude" and updated_at:
        try:
            age = (time.time() - calendar.timegm(time.strptime(updated_at[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
            if age >= cleanup - 1:
                return "auto"
        except ValueError:
            pass
    return "deliberate"


def _keeps(keep, kind, how):
    """Whether retention.keep holds on to a session after its log is deleted."""
    return (keep == "all" or (keep == "main" and kind != "helper")
            or (keep == "auto" and how == "auto"))


def _note_gone(con, agent, gone, stats):
    """Record newly deleted logs (for the retention check) and mark the kept ones. `gone`: {id: (how, kept)}."""
    stamp = store.now_iso()
    run = stats.setdefault("run", f"{time.time():.6f}")  # one id per update, to tell one-update batches apart
    stats.setdefault("deletions", []).extend(
        {"agent": agent, "session_id": s, "how": how, "kept": kept, "noticed": stamp, "run": run}
        for s, (how, kept) in gone.items())
    kept = [(stamp, how, agent, s) for s, (how, k) in gone.items() if k]
    con.executemany("update sessions set log_deleted_at=coalesce(log_deleted_at, ?), "
                    "log_deleted_how=coalesce(log_deleted_how, ?) where agent=? and session_id=?", kept)
    if kept:
        stats["kept_after_log_deleted"] = stats.get("kept_after_log_deleted", 0) + len(kept)


def _vanished_alarm(stats, agent, n, total):
    stats["failures"].append({"kind": "guard", "path": agent,
                              "error": f"{n} of {total} sessions vanished at once, including recent ones; kept them "
                                       "(drive unmounted or unreadable?). If the removal is real, run "
                                       "`smem update --accept-removals`"})


def _reconcile(con, agent, present, stats, log, accept_removals=False, keep="all", with_log=None, cleanup=30):
    """Sessions whose source is gone (from disk, and for Codex from its thread table too) are kept and
    marked, or removed, as retention.keep says. A session whose log is back loses its mark."""
    with_log = present if with_log is None else with_log
    rows = {r[0]: tuple(r)[1:] for r in con.execute(
        "select session_id, kind, log_deleted_at, log_deleted_how, updated_at from sessions where agent=?", (agent,))}
    back = [s for s, (_, gone_at, _, _) in rows.items() if gone_at and s in with_log]
    con.executemany("update sessions set log_deleted_at=null, log_deleted_how=null where agent=? and session_id=?",
                    [(agent, s) for s in back])
    gone = set(rows) - present
    if not gone:
        return
    new = {s: _how(agent, rows[s][3], cleanup) for s in gone if not rows[s][1]}
    # Many logs vanishing at once looks like an unmounted drive, not deletion: nothing is marked or removed,
    # and the guard keeps the discovery baseline so health raises the alarm. Logs old enough for the
    # assistant's own cleanup don't count (a cleanup after weeks away is normal), nor do sessions already
    # known to be gone (kept until the user changed retention.keep).
    recent = [s for s, how in new.items() if how != "auto"]
    if len(recent) > MAX_VANISH_SHARE * max(1, len(rows)) and not accept_removals:
        return _vanished_alarm(stats, agent, len(new), len(rows))
    how = {s: new.get(s) or rows[s][2] or "deliberate" for s in gone}
    kept = {s for s in gone if _keeps(keep, rows[s][0], how[s])}
    _note_gone(con, agent, {s: (h, s in kept) for s, h in new.items()}, stats)
    drop = gone - kept
    for sid in drop:
        store.delete_session(con, agent, sid)
        con.execute("delete from mentions where agent=? and session_id=?", (agent, sid))
    if drop:
        stats[f"{agent}_removed"] = len(drop)
        log(f"{agent}: removed {len(drop)} sessions whose log no longer exists")


def _carry_kept(previous, con, agent, keep, ignore, stats, accept_removals=False, cleanup=30):
    """A full rebuild reads only the logs on disk; hand on the sessions kept after their logs were deleted.
    Refuses, like _check_shrink, when many recent logs vanished since the last update (drive unmounted?)."""
    now = {r[0] for r in con.execute("select session_id from sessions where agent=?", (agent,))}
    try:
        old = {r[0]: (r[1], r[2], r[3]) for r in previous.execute(
            "select session_id, kind, updated_at, log_deleted_how from sessions where agent=?", (agent,))}
    except sqlite3.OperationalError:  # a store from before log_deleted_how existed
        old = {r[0]: (r[1], r[2], None) for r in previous.execute(
            "select session_id, kind, updated_at from sessions where agent=?", (agent,))}
    missing = [s for s in old if s not in now and s not in ignore]
    known = _known_gone(previous, agent)
    new = {s: _how(agent, old[s][1], cleanup) for s in missing if s not in known}
    recent = [s for s, how in new.items() if how != "auto"]
    if len(recent) > MAX_VANISH_SHARE * max(1, len(old)) and not accept_removals:
        raise RuntimeError(f"rebuild is missing {len(new)} of the store's {len(old)} {agent} sessions, including "
                           "recent ones; keeping the current store (drive unmounted?). If the removal is real, add "
                           "--accept-removals")
    how = {s: new.get(s) or old[s][2] or "deliberate" for s in missing}
    ids = [s for s in missing if _keeps(keep, old[s][0], how[s])]
    if ids:
        store.copy_sessions(previous, con, agent, ids)
        con.executemany("update sessions set log_deleted_at=coalesce(log_deleted_at, ?), log_deleted_how=? "
                        "where agent=? and session_id=?", [(store.now_iso(), how[s], agent, s) for s in ids])
    _note_gone(con, agent, {s: (h, s in ids) for s, h in new.items()}, stats)


def _known_gone(previous, agent):
    try:
        return {r[0] for r in previous.execute(
            "select session_id from sessions where agent=? and log_deleted_at is not null", (agent,))}
    except sqlite3.OperationalError:  # a store from before log_deleted_at existed
        return set()


def _check_shrink(previous, new, agent, stats, accept_removals, ignore=()):
    """A full rebuild must not quietly lose sessions the current store has: compared by identity, so
    six sessions replaced by six different ones counts as six lost."""
    old = {r[0] for r in previous.execute("select session_id from sessions where agent=?", (agent,))}
    now = {r[0] for r in new.execute("select session_id from sessions where agent=?", (agent,))}
    lost = old - now - set(ignore) - _known_gone(previous, agent)  # excluded or already-gone sessions aren't "lost"
    if lost and len(lost) > MAX_VANISH_SHARE * len(old) and not accept_removals:
        raise RuntimeError(f"rebuild is missing {len(lost)} of the store's {len(old)} {agent} sessions; keeping the "
                           "current store (drive unmounted?). If the removal is real, add --accept-removals")
    if lost:
        stats[f"{agent}_removed"] = len(lost)


# macOS and Windows folders ignore letter case by default, so exclusions must too.
_FOLD = (lambda s: s.casefold()) if sys.platform in ("darwin", "win32") else (lambda s: s)


def _excluder(cfg):
    """A test for sessions the user has excluded by folder or project name. A folder matches itself and
    everything inside it (not siblings that merely share a prefix), by its written and its resolved path."""
    paths = []
    for p in cfg["privacy"]["exclude_paths"]:
        p = os.path.abspath(os.path.expanduser(p))
        paths += {_FOLD(p.rstrip("/") or "/"), _FOLD(os.path.realpath(p).rstrip("/") or "/")}
    projects = {p.casefold() for p in cfg["privacy"]["exclude_projects"]}

    def inside(folder):
        return any(folder == p or folder.startswith(p.rstrip("/") + "/") for p in paths)

    def excluded(cwd, project=None):
        cwd = (cwd or "").replace("file://", "").rstrip("/")
        if cwd and paths and (inside(_FOLD(cwd)) or inside(_FOLD(os.path.realpath(cwd)))):
            return True
        return bool(project) and project.casefold() in projects
    return excluded, paths


def _claude_dir_excluded(path, excluded_paths):
    """Claude names each project folder after its working folder (non-alphanumerics become '-'), so an
    excluded folder's own logs can be skipped before they're opened. Only an exact match counts: the name
    doesn't show where folder names end, so sessions in subfolders are caught later by their recorded cwd."""
    folder = os.path.relpath(path, claude.CLAUDE_PROJECTS).split(os.sep)[0]
    return any(_FOLD(folder) == re.sub(r"[^A-Za-z0-9]", "-", p) for p in excluded_paths)


def _drop(con, agent, ids, stats, key):
    for sid in ids:
        store.delete_session(con, agent, sid)
        con.execute("delete from mentions where agent=? and session_id=?", (agent, sid))
    if ids:
        stats[key] = stats.get(key, 0) + len(ids)


def update(con, full=False, workers=None, log=print, accept_removals=False, previous=None):
    """Update `con` in place. For a full rebuild, `con` is a fresh database and `previous` the current
    store, used only to judge whether sources vanished (see cli.rebuild, which swaps them on success)."""
    t0 = time.time()
    started = store.now_iso()
    stats = {"codex_parsed": 0, "codex_meta_only": 0, "codex_unchanged": 0, "codex_log_only": 0,
             "claude_parsed": 0, "claude_unchanged": 0, "inherited_trimmed": 0, "failures": []}
    workers = workers or max(2, (os.cpu_count() or 4) - 2)
    parsed_ids = set()
    copy_keys = {}  # thread id -> per-turn raw-text hashes, in memory only (see codex.parse_thread)
    ref = previous or con  # what the store held before this update
    had_content = {r[0] for r in ref.execute(
        "select session_id from sessions where agent='codex' and log_path is not null")}
    cfg = config.load()
    keep = cfg["retention"]["keep"]
    cleanup = claude.cleanup_days()[0]
    excluded, excluded_paths = _excluder(cfg)
    forgotten = {a: set(ids) for a, ids in json.loads(
        (ref.execute("select value from meta where key='forgotten'").fetchone() or ["{}"])[0]).items()}
    stored = {a: {r[0] for r in con.execute("select session_id from sessions where agent=?", (a,))}
              for a in ("codex", "claude")}
    # A source the user turned off is removed from the memory; one that is missing is left alone.
    for agent in ("codex", "claude"):
        if not cfg["sources"][agent]:
            _drop(con, agent, stored[agent], stats, f"{agent}_source_off_removed")
    use_codex = cfg["sources"]["codex"] and os.path.isdir(codex.CODEX_HOME)
    use_claude = cfg["sources"]["claude"] and os.path.isdir(claude.CLAUDE_PROJECTS)
    stats["sources"] = {"codex": "on" if use_codex else ("off" if not cfg["sources"]["codex"] else "not found"),
                        "claude": "on" if use_claude else ("off" if not cfg["sources"]["claude"] else "not found")}

    # ---- Codex ----
    threads, on_disk = {}, {}
    if use_codex:
        try:
            threads = codex.load_threads()
        except sqlite3.Error as e:  # no thread table (yet), or a format change: logs still work
            if os.path.exists(os.path.join(codex.CODEX_HOME, "state_5.sqlite")):  # absent isn't a fault; health notes it
                stats["failures"].append({"kind": "source", "path": "Codex thread table", "error": str(e)})
        on_disk = codex.scan_rollouts()  # thread id -> ordered log segments
    skipped = {tid for tid, rec in threads.items()
               if tid in forgotten.get("codex", ()) or excluded(rec.get("cwd"), rec.get("project"))}
    skipped |= forgotten.get("codex", set()) & set(on_disk)
    with_log = set(on_disk)
    sigs = {} if full else _stored_sigs(con, "codex")
    todo, recs, lost_logs = [], {}, []
    for tid, rec in threads.items():
        if tid in skipped:
            continue
        paths = on_disk.get(tid) or [p for p in [codex.resolve_log(rec["log_path"])] if p]
        if paths:
            with_log.add(tid)
        if not paths:
            if tid in had_content:
                lost_logs.append(tid)
                continue  # decided after the loop: a mass disappearance keeps the stored content
            rec.update(log_path=None, log_sig=None, turns=[], files={}, unknown_types={}, parse_errors=0,
                       command_count=0, file_change_count=0, mcp_count=0, helper_count=0, inherited_context=None)
            if tid in sigs and sigs[tid] is None and not full:
                _refresh_metadata(con, rec)
            else:
                store.replace_session(con, rec)
                stats["codex_meta_only"] += 1
            continue
        sig = _sig(paths)  # taken before reading, so bytes appended during the parse get re-read next time
        if sig is None:
            stats["moved_during_update"] = stats.get("moved_during_update", 0) + 1
            continue
        rec["log_path"] = paths[-1]
        if sigs.get(tid) == sig:
            _refresh_metadata(con, rec)
            stats["codex_unchanged"] += 1
            continue
        rec["log_sig"] = sig
        recs[tid] = rec
        todo.append((tid, paths))
    # Threads that exist only as log files (not in Codex's thread table).
    log_only = [t for t in on_disk if t not in threads and t not in skipped]
    for tid in log_only:
        paths = on_disk[tid]
        sig = _sig(paths)
        if sig is None:
            stats["moved_during_update"] = stats.get("moved_during_update", 0) + 1
            continue
        if sigs.get(tid) == sig:
            stats["codex_unchanged"] += 1
            continue
        recs[tid] = {"agent": "codex", "session_id": tid, "kind": "top", "origin": "log-only",
                     "log_path": paths[-1], "log_sig": sig}
        todo.append((tid, paths))
    log(f"codex: {len(threads)} threads, {len(todo)} to parse ({len(log_only)} known only from logs)")
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for tid, parsed, err in pool.map(_parse_codex, todo, chunksize=8):
            done += 1
            rec = recs[tid]
            if err:
                stats["failures"].append({"kind": "parse", "path": rec["log_path"], "error": err})
                continue
            m = parsed.pop("meta")
            copy_keys[tid] = [t.get("raw_hash") for t in parsed["turns"]]
            parsed.pop("segments", None)
            if rec.get("origin") == "log-only":
                if excluded(m.get("cwd"), codex.project_from_cwd(m.get("cwd"))):
                    skipped.add(tid)  # read only to learn its folder; never stored
                    continue
                ts = m.get("timestamp")
                rec.update(cwd=m.get("cwd"), project=codex.project_from_cwd(m.get("cwd")),
                           created_at=ts[:19] + "Z" if ts else None, updated_at=ts[:19] + "Z" if ts else None,
                           kind=m.get("kind") or "top", helper_role=m.get("helper_role"))
                if m.get("kind") == "helper":
                    rec["origin"] = "log-only helper"
                stats["codex_log_only"] += 1
            if rec.get("parent_id"):
                rec["parent_source"] = rec.get("parent_source") or "table"
            elif m.get("parent_thread_id") or m.get("forked_from_id"):
                rec["parent_id"] = m.get("parent_thread_id") or m.get("forked_from_id")
                rec["parent_rel"] = "helper" if (m.get("parent_thread_id") or rec.get("kind") == "helper") else "fork"
                rec["parent_source"] = "log"
            rec.update(parsed)
            store.replace_session(con, rec)
            parsed_ids.add(tid)
            stats["codex_parsed"] += 1
            if done % 500 == 0:
                con.commit()
                log(f"  codex {done}/{len(todo)}")
    known = _known_gone(ref, "codex")
    newly_lost = [t for t in lost_logs if t not in known]
    if len(newly_lost) > MAX_VANISH_SHARE * max(1, len(had_content)) and not accept_removals:
        pass  # many at once: the guard below keeps everything as it was
    elif lost_logs:  # in Codex's thread table, but its log is gone: Codex never deletes on its own
        kinds = {r[0]: r[1] for r in ref.execute("select session_id, kind from sessions where agent='codex'")}
        kept = [t for t in lost_logs if _keeps(keep, threads[t].get("kind") or kinds.get(t), "deliberate")]
        if not full:  # a full rebuild hands them on from the previous store (see _carry_kept)
            _note_gone(con, "codex", {t: ("deliberate", t in kept) for t in newly_lost}, stats)
        lost_logs = [t for t in lost_logs if t not in kept]
        newly_lost = [t for t in lost_logs if t not in known]
    if lost_logs:
        if len(newly_lost) > MAX_VANISH_SHARE * max(1, len(had_content)) and not accept_removals:
            stats["failures"].append({"kind": "guard", "path": "codex logs",
                                      "error": f"logs for {len(lost_logs)} of {len(had_content)} "
                                      "threads became unreadable at once; kept their content (drive unmounted?)"})
            if full and previous is not None:
                raise RuntimeError(stats["failures"][-1]["error"])
        else:
            for tid in lost_logs:
                rec = threads[tid]
                rec.update(log_path=None, log_sig=None, turns=[], files={}, unknown_types={}, parse_errors=0,
                           command_count=0, file_change_count=0, mcp_count=0, helper_count=0, inherited_context=None)
                store.replace_session(con, rec)
            stats["codex_logs_gone"] = len(lost_logs)
    _drop(con, "codex", skipped & (stored["codex"] | parsed_ids), stats, "excluded_removed")
    if full and previous is not None:
        _carry_kept(previous, con, "codex", keep, skipped, stats, accept_removals, cleanup)
        _check_shrink(previous, con, "codex", stats, accept_removals, ignore=skipped)
    elif use_codex:
        _reconcile(con, "codex", (set(threads) | set(on_disk)) - skipped, stats, log, accept_removals, keep,
                   with_log=with_log, cleanup=cleanup)
    con.commit()

    had_goals = ref.execute("select 1 from goals limit 1").fetchone() is not None
    if not cfg["sources"]["codex"]:
        goals = []  # turned off: removed with the rest of Codex
    elif not use_codex:
        goals = None if had_goals else []  # folder missing (drive unmounted?): keep what's stored
    elif not os.path.exists(os.path.join(codex.CODEX_HOME, "goals_1.sqlite")) and not had_goals:
        goals = []  # this Codex has never kept goals here: nothing to read, nothing to report
    else:
        goals = codex.load_goals()
    if goals is None:
        if previous is not None:  # a rebuild starts empty: carry the previous goals over
            for g in previous.execute("select thread_id, status, objective, tokens_used, updated_at from goals"):
                con.execute("insert or replace into goals values (?,?,?,?,?)", tuple(g))
        if use_codex:
            stats["failures"].append({"kind": "goals", "path": "goals_1.sqlite", "error": "unreadable; kept the previous goals"})
    else:
        con.execute("delete from goals")
        for g in goals:
            g["objective"] = store.clean(g["objective"])
            con.execute("insert into goals values (:thread_id,:status,:objective,:tokens_used,:updated_at)", g)

    # ---- Claude ----
    logs = claude.list_logs() if use_claude else []
    cskipped = {a[1] for a in logs if a[1] in forgotten.get("claude", ()) or _claude_dir_excluded(a[0], excluded_paths)}
    logs = [a for a in logs if a[1] not in cskipped]
    csigs = {} if full else _stored_sigs(con, "claude")
    ctodo = []
    for a in logs:
        try:
            sig = claude.log_sig(a[0])  # before reading; see the Codex note above
        except FileNotFoundError:  # removed since the scan: skipped this run
            stats["moved_during_update"] = stats.get("moved_during_update", 0) + 1
            continue
        if csigs.get(a[1]) != sig:
            ctodo.append((a, sig))
    presig = {a[1]: s for a, s in ctodo}
    stats["claude_unchanged"] = len(logs) - len(ctodo)
    log(f"claude: {len(logs)} logs, {len(ctodo)} to parse")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for args, rec, err in pool.map(_parse_claude, [a for a, _ in ctodo], chunksize=4):
            if err:
                stats["failures"].append({"kind": "parse", "path": args[0], "error": err})
                continue
            mentions = rec.pop("_mentions")
            if excluded(rec.get("cwd"), rec.get("project")):
                cskipped.add(rec["session_id"])
                continue
            rec["log_sig"] = presig[args[1]]
            store.replace_session(con, rec)
            con.execute("delete from mentions where agent='claude' and session_id=?", (rec["session_id"],))
            con.executemany("insert or ignore into mentions values ('claude', ?, ?)",
                            [(rec["session_id"], m) for m in mentions])
            parsed_ids.add(rec["session_id"])
            stats["claude_parsed"] += 1
    _drop(con, "claude", cskipped & stored["claude"], stats, "excluded_removed")
    if full and previous is not None:
        _carry_kept(previous, con, "claude", keep, cskipped, stats, accept_removals, cleanup)
        _check_shrink(previous, con, "claude", stats, accept_removals, ignore=cskipped)
    elif use_claude:
        _reconcile(con, "claude", {a[1] for a in logs} - cskipped, stats, log, accept_removals, keep, cleanup=cleanup)
    con.commit()

    def load_keys(tid):
        try:
            return [t.get("raw_hash") for t in codex.parse_thread(on_disk[tid])["turns"]] if tid in on_disk else None
        except Exception:
            return None
    stats["inherited_trimmed"] = trim_inherited(con, parsed_ids, copy_keys, load_keys)
    # What the scanners found, so `smem health` can tell when discovery itself stops finding sources.
    # Not recorded when a guard refused this run: the old baseline is what health must compare against.
    if not any(f.get("kind") == "guard" for f in stats["failures"]):
        resolvable = sum(1 for r in threads.values() if codex.resolve_log(r["log_path"]))
        con.execute("insert or replace into meta values ('inventory', ?)", (json.dumps(
            {"codex_threads": len(threads), "codex_log_threads": len(on_disk), "codex_resolvable": resolvable,
             "claude_logs": len(logs)}),))
    elif previous is not None:
        old = previous.execute("select value from meta where key='inventory'").fetchone()
        if old:
            con.execute("insert or replace into meta values ('inventory', ?)", (old[0],))
    # Sessions stored before a folder or project was excluded (or the session forgotten), whatever their log's state.
    for agent, ids in (("codex", skipped), ("claude", cskipped)):
        late = {r[0] for r in con.execute("select session_id, cwd, project from sessions where agent=?", (agent,))
                if r[0] in forgotten.get(agent, ()) or excluded(r[1], r[2])}
        _drop(con, agent, late, stats, "excluded_removed")
        ids |= late
    # Sessions skipped by the user's exclusions, so health doesn't report them as missing.
    # Deleted logs seen in the last 120 days, for the retention check in `smem health`.
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120 * 86400))
    history = json.loads((ref.execute("select value from meta where key='deletions'").fetchone() or ["[]"])[0])
    history = [e for e in history + stats.get("deletions", []) if e["noticed"] >= cutoff][-5000:]
    con.execute("insert or replace into meta values ('deletions', ?)", (json.dumps(history),))
    con.execute("insert or replace into meta values ('forgotten', ?)",
                (json.dumps({a: sorted(ids) for a, ids in forgotten.items()}),))
    con.execute("insert or replace into meta values ('excluded', ?)",
                (json.dumps({"codex": sorted(skipped), "claude": sorted(cskipped)}),))
    rebuild_links(con)
    stats["seconds"] = round(time.time() - t0, 1)
    con.execute("insert into runs (started_at, finished_at, mode, stats) values (?,?,?,?)",
                (started, store.now_iso(), "full" if full else "incremental", json.dumps(stats)))
    # The start, not the end: logs written while this update ran count as changed since it, not as missed.
    con.execute("insert or replace into meta values ('last_update', ?)", (started,))
    con.commit()
    if any(k in stats for k in ("excluded_removed", "codex_source_off_removed", "claude_source_off_removed")):
        store.purge_deleted(con)  # removed for privacy: erase the text from the file too, not just the index
    return stats


def trim_inherited(con, parsed_ids, copy_keys, load_keys):
    """Older Codex forks start their log with a copy of the parent's history. Remove that copied prefix
    so it isn't shown as the child's own work.

    Only Codex sessions whose log says they were forked from that parent qualify, and a turn counts as
    copied only if its raw (unscrubbed) request *and* answer are identical to the parent's turn at the
    same position. The comparison uses hashes held in memory during this update (`copy_keys`, or
    `load_keys` re-reading a log); they are never stored, because a stored hash of a short secret can
    be guessed back from the redacted text."""
    trimmed = 0
    rows = con.execute("""select agent, session_id, parent_id from sessions
                          where agent='codex' and parent_id is not null and forked_from = parent_id
                          and turn_count > 1 and inherited_turns = 0""").fetchall()
    for r in rows:
        if r["session_id"] not in parsed_ids and r["parent_id"] not in parsed_ids:
            continue
        child = copy_keys.get(r["session_id"]) or load_keys(r["session_id"])
        parent = copy_keys.get(r["parent_id"]) or load_keys(r["parent_id"])
        if not child or not parent:
            continue
        copy_keys.setdefault(r["parent_id"], parent)
        k = 0
        while k < len(child) - 1 and k < len(parent) and child[k] and child[k] == parent[k]:
            k += 1  # the child's last turn is always kept
        if k:
            _drop_leading_turns(con, r["agent"], r["session_id"], k)
            trimmed += 1
    con.commit()
    return trimmed


def _drop_leading_turns(con, agent, sid, k):
    key = (agent, sid)
    for table in ("turns", "turns_fts", "files_touched"):
        con.execute(f"delete from {table} where agent=? and session_id=? and turn_index<?", key + (k,))
        # Two steps so the shifted indexes never collide with rows not yet moved.
        con.execute(f"update {table} set turn_index=turn_index+1000000 where agent=? and session_id=?", key)
        con.execute(f"update {table} set turn_index=turn_index-1000000-? where agent=? and session_id=?", (k,) + key)
    # Everything below is recomputed from the turns that remain. MCP and helper totals are per-session
    # in older logs only, so they are recomputed from turns where available.
    con.execute("""update sessions set inherited_turns=?,
        command_count=(select coalesce(sum(commands),0) from turns where agent=? and session_id=?),
        commands_estimated=(select coalesce(sum(estimated),0) from turns where agent=? and session_id=?),
        mcp_count=(select coalesce(sum(mcp),0) from turns where agent=? and session_id=?),
        last_answer=(select answer from turns where agent=? and session_id=? and answer is not null
                     order by turn_index desc limit 1),
        turn_count=(select count(*) from turns where agent=? and session_id=?),
        request_turns=(select count(*) from turns where agent=? and session_id=? and request is not null),
        answer_turns=(select count(*) from turns where agent=? and session_id=? and answer is not null),
        file_change_count=(select count(*) from files_touched where agent=? and session_id=?),
        first_request=(select request from turns where agent=? and session_id=? and request is not null
                       order by turn_index limit 1)
        where agent=? and session_id=?""", (k,) + key * 10)


def rebuild_links(con):
    con.execute("delete from links")
    # Parent/helper and fork links, both agents.
    con.execute("""insert or ignore into links
        select agent, parent_id, agent, session_id, coalesce(parent_rel,'helper'), 'exact', 'session metadata'
        from sessions where parent_id is not null""")
    # Claude sessions whose commands or tool output name a Codex thread id.
    con.execute("""insert or ignore into links
        select 'claude', m.session_id, 'codex', m.target_id, 'mentions', 'exact', 'thread id appears in session'
        from mentions m join sessions s on s.agent='codex' and s.session_id=m.target_id
        where m.agent='claude'""")
