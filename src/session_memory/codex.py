"""Codex source adapter: thread metadata from Codex's own databases, content from its rollout logs.

Reads ~/.codex read-only. Handles the legacy event format, the current item_completed format,
compressed .jsonl.zst logs, and threads split across several log segments.
"""
import functools
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.parse
from collections import Counter

CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")  # see config.codex_home
EXTRA_LOG_DIRS = []  # extra folders under CODEX_HOME holding rollout logs (settings: sources.codex_extra_log_dirs)
LOG_DIRS = ("sessions", "archived_sessions")  # where Codex itself keeps rollout logs
ADAPTER_VERSION = 9  # bump when parsing changes so `smem update` re-reads every Codex log

# Every record type seen in a broad sample of real logs. Anything else is reported as unknown,
# which is how `smem health` notices a format change on the day it happens.
KNOWN_TOP = {"session_meta", "event_msg", "response_item", "world_state", "turn_context",
             "token_usage_record", "compacted", "inter_agent_communication_metadata"}
KNOWN_EVENT = {"thread_settings_applied", "task_started", "item_completed", "token_count", "task_complete",
               "turn_aborted", "thread_goal_updated", "user_message", "agent_reasoning", "agent_message",
               "context_compacted", "item_started", "item_updated"}
KNOWN_ITEM = {"UserMessage", "AgentMessage", "CommandExecution", "McpToolCall", "Reasoning", "SubAgentActivity",
              "FileChange", "FunctionCallOutput", "Extension", "ContextCompaction", "ImageView",
              "CollabAgentToolCall", "DynamicToolCall", "EnteredReviewMode", "ExitedReviewMode", "Plan", "WebSearch"}
KNOWN_RESPONSE = {"message", "custom_tool_call", "custom_tool_call_output", "reasoning", "agent_message",
                  "function_call", "function_call_output", "compaction", "web_search_call", "local_shell_call",
                  "local_shell_call_output"}
EXEC_FUNCTIONS = {"exec_command", "shell", "local_shell"}

AMBIENT = ("<environment_context", "<in-app-browser-context", "# AGENTS.md", "<user_instructions",
           "<turn_aborted", "<permissions", "<skills_instructions", "<system-reminder", "<user_shell_command")
PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.M)
EXEC_LITERAL_RE = re.compile(r"tools\.exec_command\s*\(")
ROLLOUT_RE = re.compile(r"rollout-[0-9T:-]+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                        r"(?:_[0-9a-f-]{36})?\.jsonl(?:\.zst)?$")


def ro(db):
    """Read-only connection that never creates a file in Codex's folder. With no pending write-ahead log
    (Codex isn't writing), the database is read as immutable, which needs no -wal/-shm files. While Codex
    is writing, its own -wal/-shm already exist and a normal read-only open shares them."""
    path = os.path.join(CODEX_HOME, db)
    if not os.path.exists(path):
        raise sqlite3.OperationalError(f"unable to open database file: {path}")
    uri = "file:" + urllib.parse.quote(path)
    wal = path + "-wal"
    pending = os.path.exists(wal) and os.path.getsize(wal) > 0
    con = sqlite3.connect(uri + ("?mode=ro" if pending else "?mode=ro&immutable=1"), uri=True)
    con.execute("select 1 from sqlite_master limit 1")
    con.row_factory = sqlite3.Row
    return con


def iso_from_epoch(sec):
    # All stored times are UTC ISO-8601 with a Z, matching the log timestamps.
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sec)) if sec else None


@functools.lru_cache(maxsize=4096)
def project_from_cwd(cwd):
    """A session's project: the git repository it ran in, else the folder's name."""
    if not cwd:
        return None
    cwd = cwd.replace("file://", "").rstrip("/")
    if "/.codex/worktrees/" in cwd:  # Codex-managed worktree: <home>/worktrees/<id>/<repo>/...
        parts = cwd.split("/.codex/worktrees/", 1)[1].split("/")
        if len(parts) > 1:
            return parts[1]
    probe = cwd
    while probe and probe != os.path.dirname(probe):
        if os.path.exists(os.path.join(probe, ".git")):
            return os.path.basename(probe)
        probe = os.path.dirname(probe)
    return os.path.basename(cwd) or cwd


def helper_info(source):
    """(kind, parent_id, role) from a thread's `source`, given as a JSON string or a dict."""
    if isinstance(source, str):
        if not source.startswith("{"):
            return "top", None, None
        try:
            source = json.loads(source)
        except ValueError:
            return "top", None, None
    if not isinstance(source, dict) or "subagent" not in source:
        return "top", None, None
    sub = source.get("subagent") or {}
    if isinstance(sub, dict):
        ts = sub.get("thread_spawn") or {}
        role = ts.get("agent_path") or ts.get("agent_role") or ts.get("agent_nickname") or (
            sub.get("other") if isinstance(sub.get("other"), str) else None)
        return "helper", ts.get("parent_thread_id"), role
    return "helper", None, None


def load_threads():
    """All Codex threads with metadata, keyed by id."""
    st = ro("state_5.sqlite")
    projects = {r["id"]: r["name"] for r in st.execute("select id, name from projects")}
    edges = {r["child_thread_id"]: r["parent_thread_id"] for r in st.execute("select * from thread_spawn_edges")}
    out = {}
    for r in st.execute("select * from threads"):
        kind, src_parent, role = helper_info(r["source"] or "")
        if kind == "helper":
            origin = "helper"
        else:
            origin = {"vscode": "app", "exec": "exec", "cli": "cli"}.get(r["source"], r["source"] or "unknown")
            if r["thread_source"] == "automation":
                origin = "automation"
        parent = edges.get(r["id"]) or src_parent
        out[r["id"]] = {
            "agent": "codex", "session_id": r["id"], "kind": kind,
            "name": r["name"] or r["title"] or None,
            "project": projects.get(r["project_id"]) or project_from_cwd(r["cwd"]),
            "cwd": r["cwd"], "origin": origin, "model": r["model"], "effort": r["reasoning_effort"],
            "parent_id": parent, "parent_rel": "helper" if parent else None,
            "parent_source": "table" if parent else None,
            "helper_role": r["agent_path"] or role or r["agent_role"],
            "git_branch": r["git_branch"],
            "created_at": iso_from_epoch(r["created_at"]), "updated_at": iso_from_epoch(r["updated_at"]),
            "archived": int(r["archived"] or 0), "tokens_used": r["tokens_used"],
            "log_path": r["rollout_path"],
        }
    return out


def load_goals():
    """Current goals, or None when the goals database can't be read (callers keep what they had)."""
    try:
        g = ro("goals_1.sqlite")
        return [dict(thread_id=r[0], status=r[1], objective=r[2], tokens_used=r[3],
                     updated_at=iso_from_epoch(r[4] / 1000))
                for r in g.execute("select thread_id, status, objective, tokens_used, updated_at_ms from thread_goals")]
    except sqlite3.Error:
        return None


def resolve_log(path):
    if path and os.path.exists(path):
        return path
    if path and os.path.exists(path + ".zst"):
        return path + ".zst"
    return None


def log_sig(path):
    """Changes whenever the file's full path, identity, size or modification time (ns) changes."""
    st = os.stat(path)
    return f"{os.path.realpath(path)}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _open_zst(path):
    """Compressed Codex logs: Python 3.14's built-in zstd, else the `zstandard` package, else the `zstd` tool."""
    try:
        from compression import zstd  # Python 3.14+
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    except ImportError:
        pass
    try:
        import zstandard
        raw = zstandard.ZstdDecompressor().stream_reader(open(path, "rb"), closefd=True, read_across_frames=True)
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
    except ImportError:
        pass
    import shutil
    import subprocess
    if not shutil.which("zstd"):
        raise RuntimeError("compressed Codex logs need Python 3.14+, the 'zstandard' package, or the zstd tool")
    data = subprocess.run(["zstd", "-dcq", path], capture_output=True, check=True).stdout
    return io.StringIO(data.decode("utf-8", "replace"))


def open_lines(path):
    if path.endswith(".zst"):
        return _open_zst(path)
    return open(path, encoding="utf-8", errors="replace")


def scan_rollouts():
    """Every rollout file on disk, grouped by thread id. Long threads are split into consecutive
    segments (`rollout-<ts>-<thread>_<segment>.jsonl`); sorting by file name puts them in order."""
    import glob
    by_thread, seen = {}, set()
    for root in tuple(LOG_DIRS) + tuple(EXTRA_LOG_DIRS):
        for f in glob.glob(os.path.join(CODEX_HOME, root, "**", "rollout-*.jsonl*"), recursive=True):
            m = ROLLOUT_RE.search(os.path.basename(f))
            if not m:
                continue
            if os.path.islink(f):  # a link is the file it points to: skip it when that's gone or already found
                f = os.path.realpath(f)
                if not os.path.isfile(f) or not ROLLOUT_RE.search(os.path.basename(f)):
                    continue
            key = os.path.realpath(f)
            if key in seen:
                continue
            seen.add(key)
            by_thread.setdefault(m.group(1), []).append(f)
    for paths in by_thread.values():
        paths.sort(key=os.path.basename)
    return by_thread


def _texts(content):
    """All non-empty text parts of a message, ambient blocks included."""
    return [c["text"].strip() for c in content or []
            if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()]


def _has_text_field(content):
    """True when the message carries text where the adapter expects it, even if that text is empty.
    A renamed or moved field is what makes a message unreadable; an empty answer is not."""
    return any(isinstance(c, dict) and isinstance(c.get("text"), str) for c in content or [])


def _text(content, user=False):
    parts = _texts(content)
    if user:
        parts = [t for t in parts if not t.startswith(AMBIENT)]
    return "\n".join(parts)


def _merge_ordered(messages):
    """Messages from the legacy and item streams, in the order they were written, keeping one copy of
    each mirrored pair. `messages` is [(stream, text)]. A text is emitted when its stream has now
    written it more times than it has been emitted, so a message repeated on purpose (A, A) survives
    while a legacy+item mirror of one message (A in each stream) appears once."""
    seen, emitted, out = Counter(), Counter(), []
    for stream, text in messages:
        key = " ".join(text.split())
        seen[(stream, key)] += 1
        if seen[(stream, key)] > emitted[key]:
            emitted[key] += 1
            out.append(text)
    return out


def _new_turn(ts):
    return {"started_at": ts, "users": [], "resp_user": [], "agents": [], "final": None, "native_cmds": 0, "fallback_cmds": 0, "files": {},
            "mcp": 0, "aborted": False, "rewound": False}


def _patch_files(text, turn):
    for m in PATCH_FILE_RE.finditer(str(text or "")):
        turn["files"].setdefault(m.group(2).strip(), m.group(1).lower() + " (attempted)")


def parse_thread(paths):
    """Parse all log segments of one thread as one ordered stream.

    A continuation segment starts when Codex resumes the thread from an earlier point (a rewind or
    branch). Its ordinals restart a little below the end of the previous segment, but the records are
    new: in a sample of real multi-segment threads, many records reuse an ordinal and none is
    a copy. So every record is read, in file order. Turns from the part of a segment that a later
    segment rewound past (ordinal at or after the later segment's start) really happened but are no
    longer on the thread's visible history; they are kept and marked `rewound`. Turn state carries
    across segments. Pure function: runs in worker processes.

    Besides content it counts, per message kind, how many records were seen and how many had readable
    text (`*_records` / `*_read`). `smem health` compares the two: a field rename leaves records seen but
    unread, even when the record type is still known."""
    turns, cur = [], None
    unknown, tools, tool_first = Counter(), Counter(), {}
    errors = mcp = helpers = 0
    command_signals = message_signals = 0
    reads = Counter()  # user_records, user_read, agent_records, agent_read
    meta, inherited = {}, None

    read_digests = {}  # ordinal -> digests of records read under it (to skip exact duplicates)
    resume_at = [None] * len(paths)  # per segment: its header's ordinal (where Codex resumed from)
    adds_new = [False] * len(paths)  # per segment: whether it added any record that isn't a copy

    for i, path in enumerate(paths):
        with open_lines(path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
                    continue
                if not isinstance(d, dict):
                    continue
                typ, p, o = d.get("type"), d.get("payload"), d.get("ordinal")
                if isinstance(o, int) and len(paths) > 1 and typ != "session_meta":
                    # No real log has repeated a record (checked on all multi-segment threads), but if a
                    # segment ever does, an exact copy must not be counted twice.
                    digest = hashlib.blake2b(json.dumps(d, sort_keys=True).encode(), digest_size=8).digest()
                    seen = read_digests.setdefault(o, set())
                    if digest in seen:
                        continue
                    seen.add(digest)
                    adds_new[i] = True
                if typ == "session_meta":
                    if isinstance(o, int) and resume_at[i] is None:
                        resume_at[i] = o
                    if not meta and isinstance(p, dict):
                        kind, parent, role = helper_info(p.get("source"))
                        meta = {"id": p.get("id"), "forked_from_id": p.get("forked_from_id"),
                                "parent_thread_id": p.get("parent_thread_id") or parent, "kind": kind,
                                "helper_role": role, "cwd": p.get("cwd"), "timestamp": p.get("timestamp")}
                    continue
                if typ not in KNOWN_TOP:
                    unknown[f"top:{typ}"] += 1
                if not isinstance(p, dict):
                    continue
                pt = p.get("type")
                if typ == "compacted":
                    if inherited is None:
                        for m in p.get("replacement_history") or []:
                            if isinstance(m, dict) and m.get("role") == "user":
                                txt = _text(m.get("content"), user=True)
                                if txt:
                                    inherited = txt
                                    break
                    continue
                if typ == "event_msg" and pt == "task_started":
                    cur = _new_turn(d.get("timestamp"))
                    cur["seg"], cur["ordinal"] = i, o
                    turns.append(cur)
                    continue
                if typ not in ("event_msg", "response_item"):
                    continue
                if cur is None:
                    cur = _new_turn(d.get("timestamp"))
                    turns.append(cur)
                if typ == "event_msg":
                    if pt not in KNOWN_EVENT:
                        unknown[f"event:{pt}"] += 1
                    if pt == "task_complete":
                        if p.get("last_agent_message"):
                            cur["final"] = p["last_agent_message"]
                            message_signals += 1
                    elif pt == "turn_aborted":
                        cur["aborted"] = True
                    elif pt == "user_message":
                        message_signals += 1
                        reads["user_records"] += 1
                        msg = p.get("message")
                        if isinstance(msg, str):
                            reads["user_read"] += 1
                            if msg.strip() and not msg.lstrip().startswith(AMBIENT):
                                cur["users"].append(("legacy", msg.strip()))
                    elif pt == "agent_message":
                        message_signals += 1
                        reads["agent_records"] += 1
                        if isinstance(p.get("message"), str):
                            reads["agent_read"] += 1
                            if p["message"].strip():
                                cur["agents"].append(("legacy", p["message"].strip()))
                    elif pt == "item_completed":
                        it = p.get("item") or {}
                        k = it.get("type")
                        if k not in KNOWN_ITEM:
                            unknown[f"item:{k}"] += 1
                        if k == "UserMessage":
                            message_signals += 1
                            reads["user_records"] += 1
                            if _has_text_field(it.get("content")):
                                reads["user_read"] += 1
                            txt = _text(it.get("content"), user=True)
                            if txt:
                                cur["users"].append(("item", txt))
                        elif k == "AgentMessage":
                            message_signals += 1
                            reads["agent_records"] += 1
                            if _has_text_field(it.get("content")):
                                reads["agent_read"] += 1
                            txt = _text(it.get("content"))
                            if txt:
                                cur["agents"].append(("item", txt))
                                if it.get("phase") == "final_answer":
                                    cur["final"] = txt
                        elif k == "CommandExecution":
                            command_signals += 1
                            cur["native_cmds"] += 1
                        elif k == "FileChange":
                            if it.get("status") in (None, "completed", "applied"):
                                for fp, ch in (it.get("changes") or {}).items():
                                    cur["files"][fp] = (ch or {}).get("type", "change")
                        elif k == "McpToolCall":
                            mcp += 1
                            cur["mcp"] += 1
                        elif k == "SubAgentActivity" and it.get("kind") == "started":
                            helpers += 1
                    continue
                # response_item
                if pt not in KNOWN_RESPONSE:
                    unknown[f"response:{pt}"] += 1
                if pt in ("function_call", "custom_tool_call"):
                    name = p.get("name") or "?"
                    tools[name] += 1
                    ts = d.get("timestamp")
                    if ts and (name not in tool_first or ts < tool_first[name]):
                        tool_first[name] = ts
                    body = p.get("arguments") if pt == "function_call" else p.get("input")
                    if name in EXEC_FUNCTIONS:
                        command_signals += 1
                        cur["fallback_cmds"] += 1
                    elif name == "exec" and pt == "custom_tool_call":
                        n = len(EXEC_LITERAL_RE.findall(str(body or "")))
                        command_signals += n
                        cur["fallback_cmds"] += n
                    elif name == "apply_patch":
                        _patch_files(body, cur)
                elif pt == "message" and p.get("role") == "user":
                    txt = _text(p.get("content"), user=True)
                    if txt:
                        cur["resp_user"].append(txt)

    # A turn was rewound past if a later segment resumed (its header ordinal) at or before the point
    # where the turn started, and that segment added new records. A segment that only repeats earlier
    # records rewinds nothing.
    for t in turns:
        o, i = t.get("ordinal"), t.get("seg", 0)
        t["rewound"] = isinstance(o, int) and any(
            adds_new[j] and resume_at[j] is not None and resume_at[j] <= o for j in range(i + 1, len(paths)))
    thread_has_events = any(t["users"] for t in turns)
    out_turns, files = [], {}
    commands = estimated = 0
    for t in turns:
        # The same message is often written twice (legacy event + item): merge per message, in order.
        parts = _merge_ordered(t["users"]) or ([] if thread_has_events else t["resp_user"])
        agent_msgs = _merge_ordered(t["agents"])
        answer = t["final"] or (agent_msgs[-1] if agent_msgs else None)
        if t["native_cmds"]:
            cmds, est = t["native_cmds"], 0  # completed items are exact; the calls behind them are mirrors
        else:
            cmds = est = t["fallback_cmds"]
        if not (parts or answer or cmds or t["files"]):
            continue
        idx = len(out_turns)
        for fp, ch in t["files"].items():
            if not (ch.endswith("(attempted)") and fp in files):
                files[fp] = (ch, idx)
        commands += cmds
        estimated += est
        request = "\n\n".join(parts) or None
        out_turns.append({"started_at": t["started_at"], "request": request, "answer": answer,
                          "commands": cmds, "estimated": est, "mcp": t["mcp"], "aborted": int(t["aborted"]),
                          "rewound": int(t["rewound"]),
                          "files": sorted(t["files"]),
                          # In-memory only (never stored: it would let short redacted secrets be guessed
                          # back). Used during the update to recognise history copied from a parent.
                          # Turns without text get none, so they can never look copied.
                          "raw_hash": hashlib.sha256(f"{request}\x00{answer}".encode("utf-8", "replace")).hexdigest()
                          if (request or answer) else None})
    return {
        "meta": meta, "turns": out_turns, "files": files, "unknown_types": dict(unknown),
        "parse_errors": errors, "command_count": commands, "commands_estimated": estimated,
        "file_change_count": len(files), "mcp_count": mcp, "helper_count": helpers,
        "inherited_context": inherited, "command_signals": command_signals,
        "message_signals": message_signals, "tool_names": dict(tools), "tool_first_seen": tool_first,
        "user_records": reads["user_records"], "user_read": reads["user_read"],
        "agent_records": reads["agent_records"], "agent_read": reads["agent_read"],
        "forked_from": meta.get("forked_from_id"), "segments": len(paths),
        "rewound_turns": sum(1 for t in out_turns if t["rewound"]),
    }


def parse_log(path):
    return parse_thread([path])
