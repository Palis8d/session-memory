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
SCHEMA_VERSION = 4

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
  unknown_shapes text,              -- json {type: field names} of those records, never their content
  ingested_at text,
  log_deleted_at text,              -- when smem found the log gone; the session is kept (settings: retention.keep)
  log_deleted_how text,             -- auto (looks like the assistant's own cleanup) | deliberate (looks deleted by hand)
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
  recalls integer default 0,            -- the assistant ran smem in this turn: its answer may quote other sessions
  primary key (agent, session_id, turn_index)
);

create table if not exists plans (
  agent text not null,
  session_id text not null,
  items text,                       -- json [{"text", "status"}]: the session's last recorded plan (its own to-do list)
  note text,                        -- the explanation given with that plan update, if any
  updated_at text,
  primary key (agent, session_id)
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
# Best effort over session text: prose, markdown, JSON (also JSON escaped inside JSON), shell, .env files and code.
# Every repeat is bounded, possessive or atomic, so the time stays linear even on long adversarial text.
_B = r"(?<![A-Za-z0-9_\-])"  # key prefixes must start a token, not sit inside a word
# A secret-named key: a dotted, dashed or snake_case name whose *ending* is sensitive, so SERVICE_API_KEY, "password",
# accessToken, PGPASSWORD, SECRET_KEY_BASE and **API key** match, but author, authentication, token_count,
# tokens_used, password_hash and api_key_rotation don't. pass/pwd/pw count only as a separate part (DB_PASS,
# MYSQL_PWD), so `pwd: /Users/me` stays; a quoted "pass" or "auth" key counts too, and so does any upper-case
# env name ending in _KEY. The name is taken whole (possessive), then its ending is checked with lookbehinds.
_KEY_QUALIFIERS = ("api", "access", "secret", "private", "signing", "encryption", "session", "master", "client",
                   "auth", "account", "subscription", "consumer", "shared", "storage", "license", "admin", "service",
                   "role", "webhook", "hmac", "jwt", "deploy", "ssh", "gpg", "pgp", "root", "cipher", "app")
_KEY = (r"(?:[A-Za-z0-9]{0,40}+[_.\-]){0,10}+[A-Za-z0-9]{1,40}+"
        r"(?<=rd|wd|se|de|et|en|al|ls|id|ey|ss|pw|th)"  # cheap first filter on the last two letters
        r"(?:(?<=password)|(?<=passwd)|(?<=passphrase)|(?<=passcode)|(?<=secret)|(?<=token)|(?<=credential)"
        r"|(?<=credentials)|(?<=sessionid)|(?<=sessid)|(?<=secret_key_base)|(?<=private_key_id)"
        r"|(?<=[_.\-]pass)|(?<=[_.\-]pwd)|(?<=[_.\-]pw)"
        r"|(?<=[\"']pass)(?=\\{0,15}[\"'])|(?<=[\"']auth)(?=\\{0,15}[\"'])"
        r"|(?-i:(?<=[A-Z0-9]_KEY))"
        r"|(?<=key)(?:" + "|".join(r"(?<=%s[_.\- ]key)|(?<=%skey)" % (q, q) for q in _KEY_QUALIFIERS) + "))")


def _quoted(k):
    """A string opened by k backslashes and a quote (k = 0 plain, 1 escaped JSON, 3 doubly escaped, ...). Inside,
    a run of r backslashes then a quote closes the string only when r % (2k+2) == k; an unclosed string runs to
    the end of the line."""
    close = r"(?:\\{%d})*+\\{%d}\"" % (2 * k + 2, k)
    return r"\\{%d}\"(?:[^\\\"\n]|(?!%s)\\++\"?){0,65536}+(?:%s)?" % (k, close, close)


_VALUE = ("(?:" + "|".join(_quoted(k) for k in (15, 7, 3, 1, 0)) +
          r"|\\++\"[^\"\n]{0,65536}+\"?"                    # any other escape depth: up to the next quote
          r"|'(?:[^'\\\n]|\\.){0,65536}+'?"                  # single-quoted
          # bare, up to a space or delimiter; an & stays in the value unless it starts the next field (&name=)
          r"|(?P<bare>[^\s,;}\"'<>&]{1,65536}+(?:&(?![\w.\-\[\]]{1,64}+=)[^\s,;}\"'<>&]{0,65536}+){0,16}+))")
_SEP = (r"\\{0,15}[\"'`]?\]?[*_]{0,2}[ \t]*"               # closing quote or ] of the key, markdown bold
        r"(?::[ \t]*[A-Za-z_][\w.]{0,40}+(?:\[[^\]\n]{0,60}+\])?"  # a type annotation: `password: str = "x"`
        r"(?:[ \t]*\|[ \t]*[A-Za-z_][\w.]{0,40}+(?:\[[^\]\n]{0,60}+\])?){0,3}[ \t]*=(?![=>])"
        r"|:=|=>|[:=](?![:=]))"
        r"[ \t]*(?:[*_]{1,2}[ \t]*)?")
_AUTH_SCHEMES = r"(?:basic|bearer|token|digest|negotiate|ntlm|apikey|api-key|key|sso-key|hmac|jwt|otp|ssws|dsn)"
SECRET_PATTERNS = [
    # PEM and PGP private keys, also escaped inside JSON; then a header whose END line is missing
    re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----(?:[^-]|-(?!-)){0,20000}+"
               r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
               r"(?:(?:\s|\\{1,7}[nr]){1,8}+[A-Za-z0-9+/=:,\-]{16,}+){0,400}+"),
    # tokens recognisable by their prefix
    re.compile(_B + r"(?:sk-(?=[A-Za-z0-9_\-]{0,300}?[0-9])[A-Za-z0-9_\-]{20,}+"  # OpenAI, Anthropic, DeepSeek...
               r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}+|whsec_[A-Za-z0-9+/=]{16,}+"  # Stripe
               r"|gh[pousr]_[A-Za-z0-9]{30,}+|github_pat_[A-Za-z0-9_]{30,}+"
               r"|gl(?:pat|dt|rt|ptt|ft|soat|cbt|imt|oas)-[A-Za-z0-9_\-]{20,}+"
               r"|(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}"
               r"|xox[abeoprs]-[A-Za-z0-9\-]{10,}+|xapp-[A-Za-z0-9\-]{10,}+"
               r"|AIza[0-9A-Za-z_\-]{35}|GOCSPX-[A-Za-z0-9_\-]{20,}+|ya29\.[A-Za-z0-9_\-]{20,}+"
               r"|1//0[A-Za-z0-9_\-]{30,}+"
               r"|hf_[A-Za-z0-9]{30,}+|re_[A-Za-z0-9]{8,}+_[A-Za-z0-9]{16,}+|npm_[A-Za-z0-9]{30,}+"
               r"|SG\.[A-Za-z0-9_\-]{16,}+\.[A-Za-z0-9_\-]{16,}+"
               r"|sbp_[A-Za-z0-9]{30,}+|sb_secret_[A-Za-z0-9_\-]{20,}+|do[opr]_v1_[A-Za-z0-9]{40,}+"
               r"|pypi-AgE[A-Za-z0-9_\-]{50,}+|dckr_pat_[A-Za-z0-9_\-]{20,}+|hvs\.[A-Za-z0-9_\-]{24,}+"
               r"|(?:gsk_|xai-|pplx-|nvapi-|pcsk_|lsv2_|r8_)[A-Za-z0-9_\-]{30,}+|tvly-[A-Za-z0-9\-]{20,}+"
               r"|lin_api_[A-Za-z0-9]{30,}+|sntry[su]_[A-Za-z0-9+/=_]{30,}+|gl(?:sa|c)_[A-Za-z0-9+/=_]{30,}+"
               r"|shp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}|sq0(?:atp|csp|idp)-[A-Za-z0-9_\-]{20,}+"
               r"|ntn_[A-Za-z0-9]{40,}+|PMAK-[A-Za-z0-9\-]{40,}+|ATATT[A-Za-z0-9_\-=]{50,}+|tskey-[A-Za-z0-9\-]{20,}+"
               r"|dp\.(?:pt|st|sa|ct)\.[A-Za-z0-9]{40,}+|key-[0-9a-f]{32}|AGE-SECRET-KEY-1[0-9A-Z]{58}"
               r"|sk\.eyJ[A-Za-z0-9_\-]{20,}+\.[A-Za-z0-9_\-]{10,}+|[0-9]{8,10}:AA[A-Za-z0-9_\-]{33}"
               r"|eyJ[A-Za-z0-9_\-]{10,}+\.[A-Za-z0-9_\-]{10,}+\.[A-Za-z0-9_\-]{10,}+)"),  # JWT
    # the secret part of webhook URLs; a password in scheme://user:password@host (it may hold @ or /)
    re.compile(r"(?i)https?://(?:hooks\.slack\.com/(?:services|workflows|triggers)"
               r"|(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks)/(?P<v>[A-Za-z0-9/_\-]{8,}+)"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,20}+://[^\s:/@?#'\"<>]{0,128}+:(?![0-9]{1,5}(?:[/?#]|$))"
               r"(?P<v>[^\s'\"<>]{1,256}?)@(?=[A-Za-z0-9\[])[A-Za-z0-9.\-\[\]:%_]{1,253}+"
               r"(?=[/?#\s'\"<>,;)\]]|$)"),
    # credentials in HTTP headers, then standalone bearer tokens
    re.compile(r"(?i)(?<![A-Za-z0-9_\-])(?:proxy-)?authorization\\{0,15}[\"'`]?[ \t]*[:=][ \t]*\\{0,15}[\"'`]?[ \t]*"
               r"(?>" + _AUTH_SCHEMES + r"[ \t]+)?(?P<loose>)(?P<bare>(?P<v>[^\s\"'`\\<>]{1,4096}+))"),
    re.compile(r"(?i)\bbearer[ \t]+(?P<v>[A-Za-z0-9._~+/\-]{16,}+=*)"),
    re.compile(r"(?i)(?<![A-Za-z0-9_\-])(?:set-)?cookie\\{0,15}[\"'`]?[ \t]*[:=][ \t]*\\{0,15}[\"'`]?[ \t]*"
               r"(?P<v>[^\s=;,\"'`\\<>]{1,128}+=[^\r\n\"'`\\<>]{1,65536}+)"),
    # passwords in commands; each is checked for its command earlier on the line (see _keep)
    re.compile(r"(?<![A-Za-z0-9_\-])(?:-u|--user)(?:[ \t]+|=)?(?P<cmd>)[\"']?[^\s:\"'\\]{1,128}+:"
               r"(?P<v>[^\s\"'\\]{1,512}+)"),  # curl -u user:password
    re.compile(r"(?<![A-Za-z0-9_\-])-p(?P<cmd>[ \t]+|=)?[\"']?(?P<v>[^\s\"'|;]{1,512}+)"),  # docker login -p, mysql -pX
    re.compile(r"(?<![A-Za-z0-9_\-])-pass(?:in|out)?[ \t]+pass:(?P<v>[^\s\"']{1,512}+)"),  # openssl
    # URL query parameters (the value stops at the next &name= or #)
    re.compile(r"(?i)(?<=[?&;])(?:" + _KEY + r"|key|sig|signature|x-amz-signature|x-goog-signature|code_verifier)"
               r"=(?P<v>[^&#\s\"'<>]{1,4096}+(?:&(?![\w.\-\[\]]{1,64}+=)[^&#\s\"'<>]{0,4096}+){0,16}+)"),
    # markdown table rows (| password | value |), XML elements (<password>value</password>), --flag value
    re.compile(r"(?i)\|[ \t]*[*_`]{0,2}(?<![A-Za-z0-9_.\-])(?P<k>" + _KEY + r")[*_`]{0,2}[ \t]*\|[ \t]*(?P<table>)"
               r"(?P<bare>(?P<v>[^|\s][^|\r\n]{0,510}?))[ \t]*\|"),
    re.compile(r"(?i)<(?P<k>" + _KEY + r")>(?P<bare>(?P<v>[^<\s][^<\n]{0,65536}+))</"),
    re.compile(r"(?i)(?<![A-Za-z0-9_\-])--?(?P<k>" + _KEY + r")[ \t]+(?P<loose>)(?!-)(?P<v>" + _VALUE + ")"),
    # key = value, key: value, "key": "value", key => value, `key: type = value`; also a lone value on the next line
    re.compile(r"(?i)(?<![A-Za-z0-9_.\-])(?P<k>" + _KEY + r")(?![A-Za-z0-9])(?P<sep>" + _SEP + r")"
               r"(?P<nl>\r?\n(?=[^\s]{1,256}+[ \t]*(?:\r?\n|\Z)))?(?P<v>" + _VALUE + ")"),
    # prose: "the password is X", redacted only when X looks generated (see _keep)
    re.compile(r"(?i)[ \t](?=(?:is|was)[ \t])(?:(?<=password[ \t])|(?<=passwd[ \t])|(?<=passphrase[ \t])"
               r"|(?<=secret[ \t])|(?<=token[ \t])|(?<=key[ \t]))(?:is|was)[ \t]+(?:now[ \t]+|still[ \t]+)?"
               r"[\"'`\u201c]?(?P<prose>)(?P<v>[^\s\"'`\u201d<>]{8,256}(?<![.,;:!?)]))"),
    # card numbers in groups (Luhn-checked) and labelled US social security numbers
    re.compile(r"(?<![0-9\-])(?P<card>)(?P<v>[0-9]{4}(?P<cs>[ \-])[0-9]{4}(?P=cs)[0-9]{4}(?P=cs)[0-9]{1,7}"
               r"|3[47][0-9]{2}[ \-][0-9]{6}[ \-][0-9]{5})(?![0-9\-])"),
    re.compile(r"(?i)\b(?:ssn|social[ \t]+security(?:[ \t]+(?:number|no\.?|#))?)[ \t:#=]{0,4}(?:is[ \t]+)?"
               r"(?P<v>[0-9]{3}-[0-9]{2}-[0-9]{4})(?![0-9])"),
]
MAX_TEXT = 200000
_SCAN_SLACK = 8192  # scrub a little past the cut, so a secret crossing MAX_TEXT is still recognised whole

# Values that are not secrets: placeholders, types, code that reads a secret from elsewhere, and file paths.
_PLACEHOLDER = re.compile(
    r"(?i)(?:true|false|null|none|nil|undefined|yes|no|on|off|required|optional|empty|unset|unknown|hidden|masked"
    r"|redacted|str|string|bytes|int|integer|number|bool|boolean|any|object|secretstr|void|text|char|varchar"
    r"|include|omit|same-origin|await|new|async|lambda|yield|function|typeof|self|this|basic|bearer|digest)\Z"
    r"|[*xX.\u2022\u2026]{1,64}\Z")
_REFERENCE = re.compile(
    r"\$[({]|\{\{|<|%\(|#\{"                                    # templates, $(cmd), ${VAR}, {{ x }}, <x>
    r"|[$%][A-Za-z_][A-Za-z0-9_]*%?\Z"                          # $VAR, %VAR%
    r"|(?:~|\.{1,2}|\$HOME|[A-Za-z]:)?[/\\](?:Users|home|etc|var|tmp|opt|usr|run|private|Volumes|mnt|srv|root|app"
    r"|workspace|data|secrets|proc|dev|Library|System|Windows|Program Files)[/\\]"
    r"|(?:~|\.{1,2})/")                                         # file paths
_CODE = re.compile(
    r"[({\[]"                                                   # an expression or structure, not a value
    r"|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*[ \t]*[(\[]"      # a call or index: getToken(), os.environ[...]
    r"|(?:[A-Za-z_$]+\.)+[A-Za-z_$]+\Z")                        # a dotted name: process.env.API_KEY
_TYPE_NAME = re.compile(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+\Z")      # `token: AccessToken`, `api_key: SecretStr`
_NOT_SECRET_NAME = re.compile(r"(?i)(?:^|[_.\-])(?:eos|bos|pad|unk|sep|cls|mask|special|max|min|num|total)"
                              r"[_\-]?token\Z")  # tokenizer settings, not credentials
_CMD_CONTEXT = {"-u": re.compile(r"\bcurl\b"),
                "-p": re.compile(r"(?i)\b(?:docker|podman|nerdctl|buildah|skopeo|oras|registry)[ \t]+login\b"
                                 r"|\bsshpass\b"),
                "-pX": re.compile(r"(?i)\b(?:mysql\w*|mariadb)\b")}


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _keep(m, value):
    """True when a matched value is clearly not a secret, so the text is left as it was."""
    g = m.groupdict()
    if "card" in g:
        return not _luhn(re.sub(r"[^0-9]", "", value))
    if "cmd" in g:  # a password flag counts only after its command on the same line: curl -u, docker login -p...
        line = m.string[max(0, m.start() - 300):m.start()].rpartition("\n")[2]
        kind = "-u" if m.group(0).startswith(("-u", "--user")) else "-p" if g["cmd"] is not None else "-pX"
        return (kind == "-u" and value.isdigit()) or not _CMD_CONTEXT[kind].search(line)
    inner = value.strip("\\\"'`*\u201c\u201d")
    if not inner or inner.startswith("[REDACTED]"):  # empty, or already redacted by an earlier pattern
        return True
    word = inner.rstrip(",;)]")
    if _PLACEHOLDER.match(word) or _REFERENCE.match(inner):
        return True
    key = g.get("k") or ""
    if key and (_NOT_SECRET_NAME.search(key) or word.lower() in (key.lower(), key.rpartition(".")[2].lower())):
        return True  # eos_token=..., or self.token = token
    if "prose" in g:  # "the password is X": only when X looks generated, not like a word
        return not (re.search(r"[0-9]", inner) and re.search(r"[A-Za-z]", inner)
                    or len(inner) >= 20 and re.search(r"[a-z]", inner) and re.search(r"[A-Z]", inner))
    if g.get("bare") is None:
        return False
    if _CODE.match(inner):
        return True
    if "table" in g and re.search(r"\s", inner):  # a description cell, not a value
        return True
    if "loose" in g and re.fullmatch(r"[a-z]{1,24}", inner):  # `--token flag`, `Authorization: none`
        return True
    sep = g.get("sep") or ""
    if ":" not in sep or "=" in sep:
        return False
    if _TYPE_NAME.match(word):
        return True
    # prose such as "The secret: keep the tests small." (a lower-case word followed by more words)
    return bool(re.fullmatch(r"[a-z]{1,24}", inner) and re.match(r"[ \t]+[A-Za-z0-9(]", m.string[m.end("v"):]))


def scrub(text):
    """Remove secrets. Returns (text, truncated); truncated means the original was longer than MAX_TEXT."""
    if not text:
        return text, False
    truncated = len(text) > MAX_TEXT
    text = text[:MAX_TEXT + _SCAN_SLACK]
    for pat in SECRET_PATTERNS:
        text = pat.sub(_redact, text)
    return text[:MAX_TEXT], truncated


def _redact(m):
    """Replace the named group "v" (or the whole match when there is none), unless the value is clearly not a secret.
    Quotes around a value are kept, so JSON stays JSON: "password": "[REDACTED]"."""
    if "v" not in m.re.groupindex:
        return "[REDACTED]"
    value = m.group("v")
    if value is None or _keep(m, value):
        return m.group(0)
    start, end = m.span("v")
    quote = re.match(r"\\*[\"']", value)
    if quote:
        start += len(quote.group())
        if len(value) >= 2 * len(quote.group()) and value.endswith(quote.group()):
            end -= len(quote.group())
    elif "(" not in value and "[" not in value:  # keep the brackets that close around a bare value: f(password=x)
        end -= len(value) - len(value.rstrip(")]"))
    whole = m.group(0)
    return whole[:start - m.start()] + "[REDACTED]" + whole[end - m.start():]


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
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700)  # session text is private: only this account may read it
    for f in (path, path + "-wal", path + "-shm"):
        if os.path.exists(f):
            os.chmod(f, 0o600)
        elif f == path:
            os.close(os.open(f, os.O_CREAT | os.O_WRONLY, 0o600))
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


def purge_deleted(con):
    """Make removed text unrecoverable from the file. The search index keeps deleted words until its
    segments merge, and SQLite keeps freed pages, so: merge the index, rewrite the file, empty the log."""
    con.execute("insert into turns_fts(turns_fts) values('optimize')")
    con.commit()
    con.execute("vacuum")
    con.execute("pragma wal_checkpoint(truncate)")


# `smem sql` may only read: no writes, no ATTACH (it creates files), no VACUUM INTO, no setting pragmas.
_DESCRIBE_PRAGMAS = {"table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
                     "foreign_key_list"}  # their argument names a table, it doesn't set anything
_VALUE_PRAGMAS = {"data_version", "schema_version", "user_version", "page_count", "page_size"}  # read only bare


def _read_only_authorizer(action, arg1, arg2, db, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in _DESCRIBE_PRAGMAS or (name in _VALUE_PRAGMAS and arg2 is None):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def query_only(con):
    """Restrict a connection to reading, for queries written by someone else."""
    con.execute("pragma query_only=on")
    con.set_authorizer(_read_only_authorizer)
    return con


def copy_sessions(src, dst, agent, ids):
    """Copy whole sessions (metadata, turns, search text, files, mentions) from one store into another, by
    the columns both have, so a store built by an older version can hand its sessions on."""
    ids = list(ids)
    for table in ("sessions", "turns", "files_touched", "mentions", "plans", "turns_fts"):
        have = [r[1] for r in src.execute(f"pragma table_info({table})")]
        want = {r[1] for r in dst.execute(f"pragma table_info({table})")}
        cols = [c for c in have if c in want]
        if not cols:
            continue
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = src.execute(f"select {','.join(cols)} from {table} where agent=? and session_id in "
                               f"({','.join('?' * len(chunk))})", [agent] + chunk).fetchall()
            verb = "insert" if table == "turns_fts" else "insert or replace"  # the text index takes plain inserts
            dst.executemany(f"{verb} into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                            [tuple(r) for r in rows])


def delete_session(con, agent, sid):
    for table in ("turns", "files_touched", "plans", "turns_fts"):
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
    for f in ("unknown_shapes",):
        rec[f] = json.dumps(rec.get(f) or {})
    plan = rec.pop("plan", None)
    con.execute("delete from plans where agent=? and session_id=?", (agent, sid))
    if plan and plan.get("items"):
        items = [{"text": clean(str(i.get("text") or "")), "status": str(i.get("status") or "")} for i in plan["items"]]
        con.execute("insert into plans values (?,?,?,?,?)",
                    (agent, sid, json.dumps(items), clean(plan.get("note")) if plan.get("note") else None, plan.get("at")))
    rec["ingested_at"] = now_iso()
    cols = [c for c in rec if not c.startswith("_")]
    con.execute(f"insert or replace into sessions ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                [rec[c] for c in cols])
    for i, t in enumerate(turns):
        req, rt = scrub(t.get("request"))
        ans, at = scrub(t.get("answer"))
        con.execute("""insert into turns (agent, session_id, turn_index, started_at, request, answer, commands,
                       files, truncated, estimated, mcp, aborted, rewound, recalls) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (agent, sid, i, t.get("started_at"), req, ans, t.get("commands", 0), len(t.get("files", ())),
                     int(rt or at), t.get("estimated", 0), t.get("mcp", 0), t.get("aborted", 0), t.get("rewound", 0),
                     int(bool(t.get("recalls")))))
        if req or ans:
            con.execute("insert into turns_fts (request, answer, agent, session_id, turn_index) values (?,?,?,?,?)",
                        (req or "", ans or "", agent, sid, i))
    for path, (change, ti) in files.items():
        con.execute("insert or replace into files_touched values (?,?,?,?,?)", (agent, sid, path, change, ti))
