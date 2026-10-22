"""`smem health`: canaries that go red the day a source format changes, instead of months later.

Codex and Claude Code change their log formats without notice. Each check watches for one kind of change:
  format drift      new record types                        (unknown event/item names)
  extraction        known types whose fields stopped reading  (messages/commands present but none read)
  trends            capture collapsing against recent weeks  (e.g. commands read in 96% of sessions, then 0%)
  new tools         a tool name never seen before            (e.g. commands moving to a new tool)
  discovery         logs on disk missing from memory         (e.g. logs moved, renamed or compressed)
  cross-check       counts below Codex's own item index
"""
import calendar
import copy
import json
import os
import sqlite3
import tempfile
import time

from . import claude, codex, config, store

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"
RANK = {GREEN: 0, YELLOW: 1, RED: 2}
WINDOW_DAYS = 14
MIN_SAMPLE = 5
FIXTURES = os.path.join(store.PACKAGE_DIR, "selftest")


def in_use(agent):
    """An assistant counts as in use when its source is on in settings and its folder exists."""
    cfg = config.load()
    folder = codex.CODEX_HOME if agent == "codex" else claude.CLAUDE_PROJECTS
    return cfg["sources"][agent] and os.path.isdir(folder)


def _excluded(con, agent):
    raw = (con.execute("select value from meta where key='excluded'").fetchone() or ["{}"])[0]
    return set(json.loads(raw).get(agent, []))


def _since(days, now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - days * 86400))


def _epoch(iso):
    return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))


def _level(share, red, yellow=0.0):
    return RED if share > red else YELLOW if share > yellow else GREEN


def check_capture(con, days=WINDOW_DAYS):
    """Recent top-level sessions with a log must mostly yield a readable request and answer."""
    out = []
    for agent in ("codex", "claude"):
        # A session whose every turn was cancelled isn't expected to have an answer; one with no
        # turns at all still is (that is what a total format break looks like).
        r = con.execute("""select count(*) n, sum(request_turns>0) req,
                                  sum(answer_turns>0 or (turn_count>0 and not exists (
                                      select 1 from turns t where t.agent=s.agent and t.session_id=s.session_id
                                      and t.aborted=0 and t.rewound=0))) ans
                           from sessions s where agent=? and kind='top' and log_path is not null and updated_at>=?""",
                        (agent, _since(days))).fetchone()
        n = r["n"] or 0
        if n < MIN_SAMPLE:
            out.append((GREEN, f"{agent} capture", f"only {n} recent sessions with logs; too few to judge yet"))
            continue
        req, ans = 100 * (r["req"] or 0) // n, 100 * (r["ans"] or 0) // n
        level = RED if min(req, ans) < 60 else YELLOW if min(req, ans) < 85 else GREEN
        out.append((level, f"{agent} capture", f"{n} recent sessions: {req}% have your request, {ans}% have an answer"))
    return out


def check_extraction(con, days=WINDOW_DAYS):
    """Known record types whose content stopped being read, fully or partly.

    * messages seen but none read, or commands seen but none counted;
    * per message kind, records seen vs records with readable text (a renamed field leaves records
      seen but unread, even for a few messages in an otherwise healthy session);
    * long sessions whose finished turns mostly lack an answer (cancelled turns and automation
      heartbeats aren't expected to have one)."""
    rows = con.execute("""select agent, session_id, message_signals, command_signals, request_turns, answer_turns,
                                 turn_count, command_count, user_records, user_read, agent_records, agent_read
                          from sessions where log_path is not null and updated_at>=?""", (_since(days),)).fetchall()
    if not rows:
        return [(GREEN, "extraction", "no recent sessions")]
    msg_lost = [r for r in rows if r["message_signals"] and not (r["request_turns"] or r["answer_turns"])]
    cmd_lost = [r for r in rows if r["command_signals"] and not r["command_count"]]
    unread = [r for r in rows
              if (r["user_records"] >= 3 and r["user_read"] < 0.8 * r["user_records"])
              or (r["agent_records"] >= 3 and r["agent_read"] < 0.8 * r["agent_records"])]
    msg_thin = []
    for r in rows:
        if r["turn_count"] >= 5 and r["answer_turns"] < 0.5 * r["turn_count"]:
            n_real, n_ans = con.execute("""select count(*), count(answer) from turns where agent=? and session_id=?
                                           and aborted=0 and rewound=0
                                           and coalesce(request,'') not like '<heartbeat>%'""",
                                        (r["agent"], r["session_id"])).fetchone()
            if n_real >= 5 and n_ans < 0.5 * n_real:
                msg_thin.append(r)
    n = len(rows)
    worst = max(len(msg_lost), len(msg_thin), len(cmd_lost), len(unread)) / n
    detail = (f"{n} recent sessions: {len(msg_lost)} with messages but none read, {len(unread)} with unreadable "
              f"messages, {len(msg_thin)} long sessions with answers on under half their finished turns, "
              f"{len(cmd_lost)} with commands but none counted")
    examples = [r["session_id"][:13] for r in (msg_lost + unread + msg_thin + cmd_lost)[:3]]
    return [(_level(worst, 0.02), "extraction", detail + (f" (e.g. {', '.join(examples)})" if examples else ""))]


def check_trends(con, now=None):
    """Capture this week against the previous seven weeks, per agent: a sudden collapse is a break."""
    out = []
    recent, base_from = _since(7, now), _since(56, now)
    for agent in ("codex", "claude"):
        def shares(lo, hi):
            r = con.execute("""select count(*) n, avg(command_count>0) c, avg(request_turns>0) q, avg(answer_turns>0) a
                               from sessions where agent=? and kind='top' and turn_count>0
                               and updated_at>=? and updated_at<?""", (agent, lo, hi)).fetchone()
            return r["n"] or 0, {"commands": r["c"] or 0, "requests": r["q"] or 0, "answers": r["a"] or 0}
        n_now, now_s = shares(recent, "9999")
        n_base, base_s = shares(base_from, recent)
        if n_now < MIN_SAMPLE or n_base < 2 * MIN_SAMPLE:
            out.append((GREEN, f"{agent} trends", f"not enough history to compare ({n_now} this week, {n_base} before)"))
            continue
        drops = [f"{k} {base_s[k]:.0%}→{now_s[k]:.0%}" for k in now_s
                 if base_s[k] >= 0.3 and now_s[k] < 0.5 * base_s[k]]
        out.append((RED if drops else GREEN, f"{agent} trends",
                    ("collapse vs previous weeks: " + ", ".join(drops)) if drops else
                    f"this week in line with previous weeks ({n_now} vs {n_base} sessions)"))
    return out


def check_new_tools(con, days=WINDOW_DAYS):
    """Tool names whose first use anywhere (from log timestamps, not session dates) is recent. A new way
    of running commands or editing files is how a past log-format change first showed, and it can first appear
    inside a long-running older session."""
    since = _since(days)
    first, has_old = {}, set()
    for r in con.execute("select agent, tool_first_seen from sessions where tool_first_seen is not null"):
        for name, ts in json.loads(r["tool_first_seen"] or "{}").items():
            key = (r["agent"], name)
            if ts and (key not in first or ts < first[key]):
                first[key] = ts
            if ts and ts < since:
                has_old.add(r["agent"])
    acked = set(json.loads((con.execute("select value from meta where key='acked_tools'").fetchone() or ["[]"])[0]))
    out = []
    for agent in sorted({a for a, _ in first}):
        if agent not in has_old:
            out.append((GREEN, f"{agent} new tools", "no history older than the window to compare against yet"))
            continue
        fresh = sorted(n for (a, n), ts in first.items()
                       if a == agent and ts >= since and n not in acked and not n.startswith("mcp__"))
        if not fresh:
            out.append((GREEN, f"{agent} new tools", "no tool names that weren't seen before"))
        else:
            out.append((YELLOW, f"{agent} new tools", f"first used in the last {days} days: {', '.join(fresh[:8])}; "
                        "check commands/edits are still counted, then `smem health --ack NAME`"))
    return out or [(GREEN, "new tools", "no baseline yet")]


def check_unknown(con, days=WINDOW_DAYS):
    """Record types the adapters don't recognize are the earliest sign of a format change."""
    seen, sessions, total = {}, 0, 0
    for r in con.execute("select unknown_types from sessions where updated_at>=? and log_path is not null",
                         (_since(days),)):
        total += 1
        u = json.loads(r["unknown_types"] or "{}")
        if u:
            sessions += 1
            for k, v in u.items():
                seen[k] = seen.get(k, 0) + v
    if not seen:
        return [(GREEN, "format drift", f"no unrecognized record types in {total} recent sessions")]
    top = ", ".join(f"{k} ×{v}" for k, v in sorted(seen.items(), key=lambda kv: -kv[1])[:6])
    level = RED if total and sessions / total > 0.05 else YELLOW
    return [(level, "format drift", f"{sessions} of {total} recent sessions contain unrecognized types: {top}")]


def check_parse_errors(con, days=WINDOW_DAYS):
    r = con.execute("""select count(*) n, sum(parse_errors>0) bad, sum(parse_errors) lines from sessions
                       where log_path is not null and updated_at>=?""", (_since(days),)).fetchone()
    n, bad = r["n"] or 0, r["bad"] or 0
    if not bad:
        return [(GREEN, "parse errors", f"no unreadable lines in {n} recent sessions")]
    return [(_level(bad / max(n, 1), 0.05, 0), "parse errors", f"{bad} of {n} recent sessions have unreadable lines ({r['lines']} lines)")]


def check_discovery(con):
    """Sources the scanners should find. Compared three ways, so discovery failing can't hide itself:
    against the store, against the inventory recorded at the last update, and (for Codex) against the
    log paths in Codex's own thread table, which doesn't depend on the directory scan. Sessions the user
    excluded are expected to be absent."""
    out = []
    prev = json.loads((con.execute("select value from meta where key='inventory'").fetchone() or ["{}"])[0])
    if not in_use("codex"):
        out.append((GREEN, "codex discovery", "Codex isn't in use here (off in settings, or not installed)"))
    else:
        on_disk = codex.scan_rollouts()
        skip = _excluded(con, "codex")
        have = {r[0] for r in con.execute("select session_id from sessions where agent='codex' and log_path is not null")}
        missing = [t for t in on_disk if t not in have and t not in skip]
        level = _level(len(missing) / max(1, len(on_disk)), 0.02, 0)
        detail = f"{len(on_disk) - len(missing)} of {len(on_disk)} threads with logs on disk are in memory"
        if skip:
            detail += f" ({len(skip)} excluded by your settings)"
        if prev.get("codex_log_threads") and len(on_disk) < 0.95 * prev["codex_log_threads"]:
            level, detail = RED, detail + f"; but the last update found {prev['codex_log_threads']} (logs unreadable or moved?)"
        try:
            listed = {r[0]: r[1] for r in codex.ro("state_5.sqlite").execute("select id, rollout_path from threads")}
            resolvable = {tid for tid, pth in listed.items() if codex.resolve_log(pth)}
            if listed and prev.get("codex_resolvable") and len(resolvable) < 0.95 * prev["codex_resolvable"]:
                level = RED
                detail += f"; only {len(resolvable)} of Codex's listed log paths open now vs {prev['codex_resolvable']} before"
            # Identity check that doesn't depend on the directory scan: logs Codex's table points at and that
            # open, but that the scan didn't find.
            unscanned = resolvable - set(on_disk) - have - skip  # the update also reads table-listed paths directly
            if unscanned:
                share = len(unscanned) / max(1, len(resolvable))
                level = max(level, RED if share > 0.02 else YELLOW, key=RANK.get)
                detail += f"; {len(unscanned)} logs listed by Codex weren't found by the scan (e.g. {min(unscanned)[:13]})"
        except sqlite3.Error as e:
            # Unreadable where it used to be readable means something changed; never seen means new or log-only.
            if prev.get("codex_resolvable"):
                level, detail = RED, detail + f"; Codex's thread table was readable before and isn't now: {e}"
            else:  # never seen here: a new or older Codex, not a break; logs alone carry everything that matters
                detail += "; Codex's thread table wasn't found, so threads are read from their logs alone"
        out.append((level, "codex discovery", detail + ("; run `smem update`" if missing else "")))
    if not in_use("claude"):
        out.append((GREEN, "claude discovery", "Claude Code isn't in use here (off in settings, or not installed)"))
    else:
        skip = _excluded(con, "claude")
        logs = [a for a in claude.list_logs() if a[1] not in skip]
        have = {r[0] for r in con.execute("select session_id from sessions where agent='claude'")}
        missing = [sid for _, sid, _ in logs if sid not in have]
        level = _level(len(missing) / max(1, len(logs)), 0.02, 0)
        detail = f"{len(logs) - len(missing)} of {len(logs)} Claude logs are in memory"
        if skip:
            detail += f" ({len(skip)} excluded by your settings)"
        if prev.get("claude_logs") and len(logs) < 0.95 * prev["claude_logs"]:
            level, detail = RED, detail + f"; but the last update found {prev['claude_logs']} (folder unreadable?)"
        out.append((level, "claude discovery", detail + ("; run `smem update`" if missing else "")))
    return out


def check_crosscheck(con, top_n=20, helper_n=10):
    """Compare our counts with Codex's own item index, for recent top-level threads and helpers.

    Codex's index can lag (it may stop at a thread's first log segment), so only counts *below* the
    index mean we missed something; counts above it are expected for multi-segment threads."""
    if not in_use("codex"):
        return []
    try:
        h = codex.ro("thread_history_1.sqlite")
    except sqlite3.Error:
        return [(GREEN, "codex cross-check", "Codex's own history index isn't available here; nothing to compare")]
    checked = mism = 0
    notes = []
    for kind, limit in (("top", top_n), ("helper", helper_n)):
        rows = con.execute("""select session_id, request_turns, answer_turns, command_count, file_change_count,
                                     user_read, agent_read from sessions
                              where agent='codex' and kind=? and log_path is not null
                              order by updated_at desc limit ?""", (kind, limit * 4)).fetchall()
        done = 0
        for r in rows:
            counts = dict(h.execute("select item_type, count(*) from thread_items where thread_id=? group by item_type",
                                    (r["session_id"],)).fetchall())
            if not counts:
                continue
            done += 1
            bad = []
            for theirs_key, ours_key, label in (("userMessage", "user_read", "user messages read"),
                                                ("agentMessage", "agent_read", "agent messages read")):
                theirs = counts.get(theirs_key, 0)
                if theirs and theirs - (r[ours_key] or 0) > max(2, theirs // 10):
                    bad.append(f"{label} {r[ours_key]} vs {theirs}")
            theirs = counts.get("commandExecution", 0)
            if theirs and theirs - r["command_count"] > max(3, theirs // 10):
                bad.append(f"commands {r['command_count']} vs {theirs}")
            if counts.get("fileChange", 0) and not r["file_change_count"]:
                bad.append("file edits")
            if bad:
                mism += 1
                notes.append(f"{r['session_id'][:8]}: {', '.join(bad)}")
            if done >= limit:
                break
        checked += done
    if not checked:
        return [(GREEN, "codex cross-check", "no recent threads in Codex's history index to compare")]
    level = RED if mism / checked > 0.2 else YELLOW if mism else GREEN
    detail = f"{checked - mism} of {checked} recent threads (top-level and helpers) are at or above Codex's own index"
    return [(level, "codex cross-check", detail + (f"; below it: {'; '.join(notes[:4])}" if notes else ""))]


def check_freshness(con):
    last, changed, cchanged = changed_since_update(con)
    if not last:
        return [(RED, "freshness", "never updated; run `smem update`")]
    refresh = config.load()["refresh"]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(_epoch(last)))
    detail = f"last update {when}; since then {changed} Codex and {cchanged} Claude sessions changed"
    if not (changed or cchanged):
        return [(GREEN, "freshness", detail)]
    if refresh["mode"] == "auto":
        # The session asking is itself always "changed"; behind is only worth a flag when the job seems stopped.
        if time.time() - _epoch(last) < 2 * 60 * refresh["interval_minutes"]:
            return [(GREEN, "freshness", detail + "; auto refresh will read them")]
        return [(YELLOW, "freshness", detail + "; auto refresh hasn't run for a while (`smem refresh` shows the job)")]
    return [(YELLOW, "freshness", detail + ("; run `smem update`" if refresh["mode"] == "manual" else
                                           "; refresh is off in settings"))]


def changed_since_update(con):
    """(last update time, Codex sessions changed since, Claude sessions changed since). Reads only file times."""
    last = (con.execute("select value from meta where key='last_update'").fetchone() or [None])[0]
    if not last:
        return None, 0, 0
    last_epoch = _epoch(last)  # whole seconds, so file times are compared in whole seconds too
    changed = cchanged = 0
    if in_use("codex"):
        try:
            st = codex.ro("state_5.sqlite")
            changed = st.execute("select count(*) from threads where updated_at > ?", (int(last_epoch),)).fetchone()[0]
        except sqlite3.Error:  # no thread table: fall back to log modification times
            changed = sum(1 for paths in codex.scan_rollouts().values() if int(os.path.getmtime(paths[-1])) > last_epoch)
    if in_use("claude"):
        cchanged = sum(1 for p, _, _ in claude.list_logs() if int(os.path.getmtime(p)) > last_epoch)
    return last, changed, cchanged


def check_last_run(con):
    r = con.execute("select stats from runs order by run_id desc limit 1").fetchone()
    if not r:
        return []
    s = json.loads(r["stats"])
    f = s.get("failures") or []
    if not f:
        return [(GREEN, "last update", f"no unreadable logs ({s.get('seconds')}s)")]
    return [(YELLOW, "last update", f"{len(f)} problems, e.g. {f[0]['path']}: {f[0]['error']}")]


def run(con, crosscheck=True):
    results = (check_freshness(con) + check_last_run(con) + check_discovery(con) + check_capture(con)
               + check_extraction(con) + check_trends(con) + check_unknown(con) + check_new_tools(con)
               + check_parse_errors(con))
    if crosscheck:
        results += check_crosscheck(con)
    overall = max((r[0] for r in results), key=RANK.get, default=GREEN)
    return overall, results


# ---------------------------------------------------------------- self-test

def _load(con, names, ages_days=(0,), copies=MIN_SAMPLE, prefix=""):
    for age in ages_days:
        for i in range(copies):
            for n in names:
                parsed = codex.parse_thread([os.path.join(FIXTURES, n)])
                parsed.pop("meta")
                parsed.pop("segments")
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age * 86400))
                rec = {"agent": "codex", "session_id": f"{prefix}{n}-{age}-{i}", "kind": "top", "log_path": n,
                       "updated_at": ts, "created_at": ts}
                rec.update(parsed)
                store.replace_session(con, rec)


def selftest():
    """Prove the canaries work: clean and legitimately quiet sessions stay GREEN, and each kind of
    format change is flagged. Runs on built-in sample logs with default settings, never on your data."""
    content_checks = (check_capture, check_extraction, check_trends, check_unknown, check_new_tools)
    cases = (
        ("clean current+legacy logs", GREEN, content_checks,
         lambda con, tmp: _load(con, ["codex_current.jsonl", "codex_legacy.jsonl"], ages_days=(1, 20, 30))),
        ("cancelled turns (must not alarm)", GREEN, content_checks,
         lambda con, tmp: _load(con, ["codex_cancelled.jsonl"])),
        ("new record types (drift)", RED, content_checks, lambda con, tmp: _load(con, ["codex_drifted.jsonl"])),
        ("known types, renamed fields", RED, content_checks,
         lambda con, tmp: _load(con, ["codex_renamed_fields.jsonl"])),
        ("some messages unreadable in a healthy-looking session", RED, content_checks,
         lambda con, tmp: (_load(con, ["codex_current.jsonl"], ages_days=(1, 20, 30), prefix="ok-"),
                           _load(con, ["codex_partial_fields.jsonl"], copies=1))),
        ("commands move to a new tool", RED, content_checks, lambda con, tmp: (
            _load(con, ["codex_current.jsonl"], ages_days=(20, 30), prefix="base-"),
            _load(con, ["codex_new_tool.jsonl"], ages_days=(1,)))),
        ("logs disappear from disk", RED, (check_discovery,), _vanishing_logs),
    )
    outcomes = []
    saved_cfg = config._cache
    config._cache = copy.deepcopy(config.DEFAULTS)  # the user's settings must not change the verdicts
    try:
        for case in cases:
            outcomes.append(_run_case(*case))
    finally:
        config._cache = saved_cfg
    return outcomes


def _run_case(label, expect, checks, setup):
    with tempfile.TemporaryDirectory() as tmp:
        con = store.connect(os.path.join(tmp, "t.sqlite"))
        saved = (codex.CODEX_HOME, claude.CLAUDE_PROJECTS)
        try:
            setup(con, tmp)
            res = [r for chk in checks for r in chk(con) if not r[1].startswith("claude")]
        finally:
            codex.CODEX_HOME, claude.CLAUDE_PROJECTS = saved
            con.close()
    got = max((r[0] for r in res), key=RANK.get)
    return got == expect, f"{label}: expected {expect}, got {got}", res


def _vanishing_logs(con, tmp):
    """Sources that were there at the last update are gone now (empty roots, no state database)."""
    _load(con, ["codex_current.jsonl"])
    con.execute("insert or replace into meta values ('inventory', ?)",
                (json.dumps({"codex_log_threads": 5, "codex_resolvable": 5, "claude_logs": 3}),))
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    codex.CODEX_HOME, claude.CLAUDE_PROJECTS = empty, os.path.join(tmp, "missing")
