"""Claude source adapter: Claude Code session logs in ~/.claude/projects (read-only)."""
import glob
import hashlib
import json
import os
import re
from collections import Counter

CLAUDE_PROJECTS = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"),
                               "projects")  # see config.claude_home
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
# Blocks Claude Code injects into user messages that the user didn't write: reminders, slash-command
# plumbing, the output of `!` shell commands, and background-task notices. Output can hold secrets.
TAG_BLOCK_RE = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z]+|bash-stdout|bash-stderr|"
                          r"task-notification)>[\s\S]*?</\1>")
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
NON_TEXT_BLOCKS = {"image", "document"}                       # user content that isn't text
AGENT_NON_TEXT = {"tool_use", "thinking", "redacted_thinking", "server_tool_use", "web_search_tool_result"}
KNOWN_TYPES = {"user", "assistant", "attachment", "api-request", "api-request-blob", "custom-title", "last-prompt",
               "agent-name", "atis-latch", "bridge-session", "queue-operation", "summary", "system",
               "file-history-snapshot", "progress", "permission-mode", "pr-link", "tag", "mode",
               "file-history-delta", "frame-link", "cost-state", "artifact-autoreact-ledger", "ai-title",
               "artifact-comment-monitor", "api-request-shape"}


def list_logs():
    """(path, session_id, parent_session_id) for main sessions and their helper-agent logs."""
    out = []
    for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        out.append((f, os.path.basename(f)[:-6], None))
    # Helper logs sit in <session>/subagents/, and workflow helpers one level deeper
    # (subagents/workflows/<run>/agent-*.jsonl, beside a journal.jsonl that isn't a session).
    for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*", "subagents", "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(f, CLAUDE_PROJECTS).split(os.sep)
        if len(rel) > 4 and not os.path.basename(f).startswith("agent-"):
            continue
        parent = rel[1]
        out.append((f, f"{parent}/{os.path.basename(f)[:-6]}", parent))
    return out


ADAPTER_VERSION = 7  # bump when parsing changes so `smem update` re-reads every Claude log


def cleanup_days():
    """(days, set_by_user): how long Claude Code keeps session logs, from its cleanupPeriodDays setting
    (30 days when unset). Claude Code deletes logs whose last activity is older than this."""
    path = os.path.join(os.path.dirname(CLAUDE_PROJECTS), "settings.json")
    try:
        with open(path) as f:
            days = json.load(f).get("cleanupPeriodDays")
        if isinstance(days, int) and not isinstance(days, bool) and days > 0:
            return days, True
    except (OSError, ValueError, AttributeError):
        pass
    return 30, False


def log_sig(path):
    st = os.stat(path)
    return f"v{ADAPTER_VERSION}:{os.path.realpath(path)}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _clean(text):
    if not isinstance(text, str):
        return ""
    return TAG_BLOCK_RE.sub("", text).strip()


def _user_prompt(msg):
    """The user's own words in a user entry, or '' for tool results and injected context."""
    c = msg.get("content")
    if isinstance(c, str):
        return _clean(c)
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        return _clean("\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"))
    return ""


def project_from_cwd(cwd):
    from .codex import project_from_cwd as by_repo
    return by_repo(cwd)


def parse_log(path, session_id, parent_id):
    turns, cur = [], None
    files, pending_edits = {}, {}
    unknown, tools, tool_first, reads = Counter(), Counter(), {}, Counter()
    errors = 0
    title = agent_name = ai_title = cwd = branch = model = first_ts = last_ts = inherited = None
    mentions = set()
    mcp = helpers = command_signals = message_signals = 0

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(d, dict):
                continue
            typ = d.get("type")
            if typ not in KNOWN_TYPES:
                unknown[f"type:{typ}"] += 1
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if typ == "custom-title":
                title = d.get("customTitle") or title
            elif typ == "agent-name":
                agent_name = d.get("agentName") or agent_name
            elif typ == "ai-title":
                ai_title = d.get("aiTitle") or ai_title
            cwd = d.get("cwd") or cwd
            branch = d.get("gitBranch") or branch
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if typ == "user" and msg and not d.get("isMeta"):
                if d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                    # Generated continuation summary after a compaction: context, not the user's words.
                    inherited = inherited or _user_prompt(msg) or None
                    continue
                prompt = _user_prompt(msg)
                c = msg.get("content")
                blocks = c if isinstance(c, list) else []
                is_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
                if not is_result and not (blocks and all(isinstance(b, dict) and b.get("type") in NON_TEXT_BLOCKS
                                                         for b in blocks)):
                    # A user message is read when its text is where the adapter looks for it: a string, or
                    # a text block with a `text` string. Image-only messages aren't text records.
                    reads["user_records"] += 1
                    reads["user_read"] += isinstance(c, str) or any(
                        isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                        for b in blocks)
                if prompt:
                    message_signals += 1
                    cur = {"started_at": ts, "request": prompt, "answer": None, "commands": 0, "files": set()}
                    turns.append(cur)
                    continue
                for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        mentions.update(UUID_RE.findall(json.dumps(b.get("content"))[:200000]))
                        edit = pending_edits.pop(b.get("tool_use_id"), None)
                        if edit and not b.get("is_error"):
                            fp, change, ti = edit
                            files.setdefault(fp, (change, ti))
                            turns[ti]["files"].add(fp)
            elif typ == "assistant" and msg:
                model = msg.get("model") or model
                if cur is None:
                    cur = {"started_at": ts, "request": None, "answer": None, "commands": 0, "files": set()}
                    turns.append(cur)
                texts = []
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        texts.append(b["text"].strip())
                    elif b.get("type") == "tool_use":
                        name = b.get("name") or ""
                        inp = b.get("input") or {}
                        tools[name] += 1
                        if ts and (name not in tool_first or ts < tool_first[name]):
                            tool_first[name] = ts
                        if name == "Bash":
                            command_signals += 1
                            cur["commands"] += 1
                            mentions.update(UUID_RE.findall(str(inp.get("command", ""))))
                        elif name in FILE_TOOLS and (inp.get("file_path") or inp.get("notebook_path")):
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            # Counted once its tool result arrives without an error.
                            pending_edits[b.get("id")] = (fp, "add" if name == "Write" else "update", len(turns) - 1)
                        elif name in ("Agent", "Task"):
                            helpers += 1
                        elif name.startswith("mcp__"):
                            mcp += 1
                other = [b for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") not in AGENT_NON_TEXT]
                if other:
                    # Anything that isn't a tool call or thinking should be readable text; a renamed block
                    # type or field shows up as a record that wasn't read.
                    reads["agent_records"] += 1
                    reads["agent_read"] += any(b.get("type") == "text" and isinstance(b.get("text"), str) for b in other)
                if texts:
                    message_signals += 1
                    cur["answer"] = "\n\n".join(texts)

    out_turns = [{"started_at": t["started_at"], "request": t["request"], "answer": t["answer"],
                  "commands": t["commands"], "estimated": 0, "mcp": 0,
                  "aborted": int(bool(t["request"]) and t["request"].startswith("[Request interrupted")),
                  "files": sorted(t["files"]),
                  "raw_hash": hashlib.sha1(f"{t['request']}\x00{t['answer']}".encode("utf-8", "replace")).hexdigest()}
                 for t in turns]
    return {
        "agent": "claude", "session_id": session_id, "kind": "helper" if parent_id else "top",
        "name": title or agent_name or ai_title, "project": project_from_cwd(cwd), "cwd": cwd,
        "origin": "claude", "model": model, "effort": None,
        "parent_id": parent_id, "parent_rel": "helper" if parent_id else None, "helper_role": None,
        "git_branch": branch, "created_at": first_ts[:19] + "Z" if first_ts else None,
        "updated_at": last_ts[:19] + "Z" if last_ts else None,
        "archived": 0, "tokens_used": None, "log_path": path,
        "turns": out_turns, "files": files, "unknown_types": dict(unknown), "parse_errors": errors,
        "command_count": sum(t["commands"] for t in turns), "commands_estimated": 0,
        "file_change_count": len(files), "mcp_count": mcp, "helper_count": helpers, "inherited_context": inherited,
        "command_signals": command_signals, "message_signals": message_signals, "tool_names": dict(tools),
        "tool_first_seen": tool_first, "forked_from": None,
        "user_records": reads["user_records"], "user_read": reads["user_read"],
        "agent_records": reads["agent_records"], "agent_read": reads["agent_read"],
        "_mentions": sorted(mentions),
    }
