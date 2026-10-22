"""SQLite store for Session Memory. Derived data only: it can always be rebuilt from the sources."""
import json
import os
import re
import sqlite3
import time
import urllib.parse

from . import config

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = config.data_dir()
_DB_NAME = os.environ.get("SESSION_MEMORY_DB_NAME", "memory.sqlite")
if os.path.basename(_DB_NAME) != _DB_NAME or _DB_NAME in ("", ".", ".."):
    raise SystemExit(f"SESSION_MEMORY_DB_NAME must be a plain file name, not {_DB_NAME!r}")
DB_PATH = os.path.join(DATA_DIR, _DB_NAME)
SCHEMA_VERSION = 1

SCHEMA = """
create table if not exists meta (key text primary key, value text);

create table if not exists sessions (
  agent text not null,              -- codex | claude
  session_id text not null,
  kind text,                        -- top | helper
  name text,
  project text,
  cwd text,
  origin text,                      -- app | exec | cli | automation | log-only | claude-desktop ...
  model text,
  effort text,
  parent_id text,                   -- helper -> parent session; fork -> forked-from
  parent_rel text,                  -- helper | fork
  parent_source text,               -- table (Codex's thread table) | log (session_meta); only table links clear
  helper_role text,                 -- e.g. /root/rails_architecture
  git_branch text,
  created_at text,
  updated_at text,
  archived integer default 0,
  tokens_used integer,
  log_path text,
  log_sig text,                     -- size:mtime of the log when parsed
  turn_count integer default 0,
  request_turns integer default 0,
  answer_turns integer default 0,
  command_count integer default 0,
  file_change_count integer default 0,
  mcp_count integer default 0,
  helper_count integer default 0,
  commands_estimated integer default 0, -- commands counted from code-mode source text, not completions
  command_signals integer default 0,    -- raw command-bearing records seen (health invariant)
  message_signals integer default 0,    -- raw message-bearing records seen (health invariant)
  inherited_turns integer default 0,    -- leading turns copied from the parent thread, removed here
  tool_names text,                      -- json {tool name: calls}
  tool_first_seen text,                 -- json {tool name: first use timestamp in this session}
  user_records integer default 0,       -- user-message records seen in the log
  user_read integer default 0,          --   ...of which had readable text (health compares the two)
  agent_records integer default 0,      -- agent-message records seen
  agent_read integer default 0,         --   ...of which had readable text
  forked_from text,                     -- session_meta.forked_from_id: the log may start with a copy
  rewound_turns integer default 0,      -- turns on a branch Codex later rewound past (kept, marked)
  parse_errors integer default 0,
  inherited_context text,           -- helper logs: context carried over from the parent, not the helper's own ask
  first_request text,
  last_answer text,
  unknown_types text,               -- json {type: count} not recognized by the adapter
  ingested_at text,
  primary key (agent, session_id)
);
create index if not exists idx_sessions_updated on sessions(updated_at);
create index if not exists idx_sessions_project on sessions(project);
create index if not exists idx_sessions_parent on sessions(parent_id);

create table if not exists turns (
  agent text not null,
  session_id text not null,
  turn_index integer not null,
  started_at text,
  request text,
  answer text,
  commands integer default 0,
  files integer default 0,
  truncated integer default 0,          -- 1 when request or answer hit MAX_TEXT
  estimated integer default 0,          -- commands in this turn counted from code-mode source text
  mcp integer default 0,
  aborted integer default 0,            -- the turn was cancelled; no answer is expected
  rewound integer default 0,            -- on a branch Codex later rewound past; real but not on the visible history
  primary key (agent, session_id, turn_index)
);

create table if not exists files_touched (
  agent text not null,
  session_id text not null,
  path text not null,
  change text,
  turn_index integer,
  primary key (agent, session_id, path)
);
create index if not exists idx_files_path on files_touched(path);

create table if not exists goals (
  thread_id text primary key,
  status text,
  objective text,
  tokens_used integer,
  updated_at text
);

create table if not exists links (
  from_agent text, from_id text, to_agent text, to_id text,
  kind text,                        -- helper | fork | mentions (a Claude session names a Codex thread id)
  confidence text,                  -- exact | likely
  evidence text,
  primary key (from_agent, from_id, to_agent, to_id, kind)
);

create table if not exists mentions (
  agent text, session_id text, target_id text,   -- a session that names another session's id
  primary key (agent, session_id, target_id)
);

create table if not exists runs (
  run_id integer primary key autoincrement,
  started_at text, finished_at text, mode text, stats text
);

create virtual table if not exists turns_fts using fts5(
  request, answer, agent unindexed, session_id unindexed, turn_index unindexed,
  tokenize = 'porter unicode61'
);
"""

# Redact secrets before anything is stored. Raw text stays only in the original logs.
_B = r"(?<![A-Za-z0-9_\-])"  # key prefixes must start a token, not sit inside a word
# A secret-named key: the sensitive word must be the key's *last* component, so SERVICE_API_KEY and
# "password" match but author, authentication, token_count and tokens_used don't.
_KEY = (r"(?i)((?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.\-])*"
        r"(?:password|passwd|pwd|passphrase|secret|token|api[_\-]?key|apikey|access[_\-]?key|"
        r"private[_\-]?key|client[_\-]?secret|auth[_\-]?token|credentials?)"
        r"(?![A-Za-z0-9_])\\?[\"']?\s*[*_]{0,2}\s*[:=]\s*[*_]{0,2}\s*)")  # **Password:** markdown too
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(_B + r"sk-(?:ant-|proj-|live_|test_)?[A-Za-z0-9_\-]{20,}"),
    re.compile(_B + r"(?:rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(_B + r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(_B + r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(_B + r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(_B + r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(_B + r"hf_[A-Za-z0-9]{30,}"),
    re.compile(_B + r"re_[A-Za-z0-9]{8,}_[A-Za-z0-9]{16,}"),
    re.compile(_B + r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/\-]{20,}=*"),
    # user:password@host in URLs
    re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)"),
    # key = value / key: value / "key": "value", with escape-aware quoted values and no length minimum
    # Value forms, tried in order: a string inside escaped JSON (\"...\"; there an embedded backslash is
    # four backslashes and an embedded quote is three backslashes and a quote, so only \" ends it), a
    # normal JSON string ("..." with \" escapes), a single-quoted string, or a bare token.
    re.compile(_KEY + r"(\\\"(?:\\\\\\\\|\\\\\\\"|\\[^\"]|[^\\])*\\\"|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,;}\"'<>]+)"),
]
MAX_TEXT = 200000


def scrub(text):
    """Remove secrets. Returns (text, truncated)."""
    if not text:
        return text, False
    for pat in SECRET_PATTERNS:
        text = pat.sub(_redact, text)
    return text[:MAX_TEXT], len(text) > MAX_TEXT


def _redact(m):
    if not m.lastindex:
        return "[REDACTED]"
    tail = m.group(2) if m.lastindex == 2 and m.group(2) == "@" else ""
    return m.group(1) + "[REDACTED]" + tail


def clean(text):
    return scrub(text)[0]


class OutdatedStore(Exception):
    """The store was written by an older schema; it needs `smem update --full`."""


def connect(path=None, allow_outdated=False):
    """Open the store. Without a path it is always memory.sqlite in the data folder (see config.data_dir);
    an explicit path is for tests only and may never point into the read-only sources.

    A store from an older schema is never modified here: it raises OutdatedStore, or with
    allow_outdated it is returned untouched so a full rebuild can read it before swapping."""
    if path is None:
        path = os.path.realpath(DB_PATH)
        if os.path.dirname(path) != os.path.realpath(DATA_DIR):
            raise ValueError(f"the store must live in {DATA_DIR}, not {path}")
    path = os.path.realpath(path)
    cfg = config.load()
    for src in ("~/.codex", "~/.claude", config.codex_home(cfg), config.claude_home(cfg)):
        if path.startswith(os.path.realpath(os.path.expanduser(src)) + os.sep):
            raise ValueError(f"refusing to open a store inside a read-only source: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=normal")
    row = None
    try:
        row = con.execute("select value from meta where key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        pass
    if row and row[0] != str(SCHEMA_VERSION):
        if allow_outdated:
            return con
        con.close()
        raise OutdatedStore(f"store schema {row[0]}, code expects {SCHEMA_VERSION}")
    con.executescript(SCHEMA)
    con.execute("insert or replace into meta values ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()  # leave no transaction open (a backup into this database needs none)
    return con


def connect_readonly(path=None):
    """Open the store for reading only. Never writes or creates a file, so it works where writing is
    blocked (an assistant's sandbox, a read-only disk). Falls back to an immutable read only when no
    write-ahead log is pending, since immutable mode would ignore its committed contents."""
    path = os.path.realpath(path or DB_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = "file:" + urllib.parse.quote(path)
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True)
        row = con.execute("select value from meta where key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        wal = path + "-wal"
        if os.path.exists(wal) and os.path.getsize(wal) > 0:
            raise
        con = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
        row = con.execute("select value from meta where key='schema_version'").fetchone()
    if not row or row[0] != str(SCHEMA_VERSION):
        con.close()
        raise OutdatedStore(f"store schema {row[0] if row else 'none'}, code expects {SCHEMA_VERSION}")
    con.row_factory = sqlite3.Row
    return con


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


TEXT_FIELDS = ("name", "first_request", "last_answer", "inherited_context", "helper_role", "git_branch")


def delete_session(con, agent, sid):
    for table in ("turns", "files_touched", "turns_fts"):
        con.execute(f"delete from {table} where agent=? and session_id=?", (agent, sid))
    con.execute("delete from sessions where agent=? and session_id=?", (agent, sid))


def replace_session(con, rec):
    """Write one parsed session (metadata + turns + files) atomically, replacing any older copy."""
    agent, sid = rec["agent"], rec["session_id"]
    if con.execute("select 1 from sessions where agent=? and session_id=?", (agent, sid)).fetchone():
        # Only existing sessions have rows to clear; the FTS delete is a full scan, so skip it otherwise.
        for table in ("turns", "files_touched", "turns_fts"):
            con.execute(f"delete from {table} where agent=? and session_id=?", (agent, sid))
    turns = rec.pop("turns", [])
    files = rec.pop("files", {})
    rec["first_request"] = next((t["request"] for t in turns if t.get("request")), None)
    rec["last_answer"] = next((t["answer"] for t in reversed(turns) if t.get("answer")), None)
    for f in TEXT_FIELDS:
        if isinstance(rec.get(f), str):
            rec[f] = clean(rec[f])
    rec["turn_count"] = len(turns)
    rec["request_turns"] = sum(1 for t in turns if t.get("request"))
    rec["answer_turns"] = sum(1 for t in turns if t.get("answer"))
    rec["unknown_types"] = json.dumps(rec.get("unknown_types") or {})
    for f in ("tool_names", "tool_first_seen"):
        if isinstance(rec.get(f), dict):
            rec[f] = json.dumps(rec[f])
    rec["ingested_at"] = now_iso()
    cols = [c for c in rec if not c.startswith("_")]
    con.execute(f"insert or replace into sessions ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                [rec[c] for c in cols])
    for i, t in enumerate(turns):
        req, rt = scrub(t.get("request"))
        ans, at = scrub(t.get("answer"))
        con.execute("""insert into turns (agent, session_id, turn_index, started_at, request, answer, commands,
                       files, truncated, estimated, mcp, aborted, rewound) values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (agent, sid, i, t.get("started_at"), req, ans, t.get("commands", 0), len(t.get("files", ())),
                     int(rt or at), t.get("estimated", 0), t.get("mcp", 0), t.get("aborted", 0), t.get("rewound", 0)))
        if req or ans:
            con.execute("insert into turns_fts (request, answer, agent, session_id, turn_index) values (?,?,?,?,?)",
                        (req or "", ans or "", agent, sid, i))
    for path, (change, ti) in files.items():
        con.execute("insert or replace into files_touched values (?,?,?,?,?)", (agent, sid, path, change, ti))
