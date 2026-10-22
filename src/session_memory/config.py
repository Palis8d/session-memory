"""Settings: one small TOML file the user can read and edit by hand.

Locations (each can be overridden by an environment variable):
  settings  $SESSION_MEMORY_CONFIG  or  $XDG_CONFIG_HOME/session-memory/config.toml  (~/.config/...)
  memory    $SESSION_MEMORY_HOME    or  $XDG_DATA_HOME/session-memory/              (~/.local/share/...)
"""
import copy
import os
import tomllib

DEFAULTS = {
    "sources": {
        "codex": True,                     # read Codex history
        "claude": True,                    # read Claude Code history
        "codex_home": "",                  # Codex's folder; empty = $CODEX_HOME, else ~/.codex
        "claude_home": "",                 # Claude Code's folder; empty = $CLAUDE_CONFIG_DIR, else ~/.claude
        "codex_extra_log_dirs": [],        # more folders (under codex_home) holding Codex rollout logs
    },
    "refresh": {
        "mode": "manual",                  # auto | manual | off
        "interval_minutes": 15,            # how often `auto` refreshes
    },
    "retention": {
        "keep": "all",                     # after an assistant deletes a session's log: all | auto | main | none
    },
    "privacy": {
        "exclude_paths": [],               # sessions whose working folder is inside these are never stored
        "exclude_projects": [],            # project names never stored
    },
    "skills": {
        "claude": True,                    # install the skill for Claude Code
        "codex": True,                     # install the skill for Codex
        "claude_dir": "",                  # Claude Code's personal skills; empty = <claude_home>/skills
        "codex_dir": "",                   # Codex's personal skills; empty = ~/.agents/skills
    },
}

COMMENTS = {
    "sources": "Which assistants' history to read, and where it lives. Their files are only ever read.",
    "refresh": "auto = refresh in the background every interval_minutes; manual = only when asked; "
               "off = nothing updates (search still works on what's stored).",
    "retention": "When a session's log is deleted: all = the memory keeps the session; auto = keeps it only "
                 "when the assistant's own cleanup deleted it (Claude Code: older than its cleanupPeriodDays) and "
                 "forgets sessions you deleted yourself; main = keeps main sessions, forgets helpers; none = forgets "
                 "it too. `smem forget ID` erases any one session; `smem forget --deleted` those you deleted.",
    "privacy": "Sessions matching these are skipped and removed from the memory.",
    "skills": "Skills teach Claude and Codex how to use the memory. `smem skills` shows where they are.",
}

_cache = None


def config_path():
    explicit = os.environ.get("SESSION_MEMORY_CONFIG")
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "session-memory", "config.toml")


def data_dir():
    explicit = os.environ.get("SESSION_MEMORY_HOME")
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "session-memory")


def _merge(base, extra):
    """Defaults overlaid with the user's values. Unknown names are reported, not silently ignored: a typo
    in a privacy setting would otherwise leave a folder unprotected."""
    out = copy.deepcopy(base)
    for section, values in (extra or {}).items():
        if not isinstance(values, dict) or not isinstance(out.get(section), dict):
            raise SystemExit(f"Settings: unknown section [{section}]. File: {config_path()}")
        for k, v in values.items():
            if k not in out[section]:
                raise SystemExit(f"Settings: unknown setting {section}.{k}; `smem settings` lists the names. "
                                 f"File: {config_path()}")
            out[section][k] = v
    return out


def load(reload=False):
    """Current settings: defaults overlaid with the user's file."""
    global _cache
    if _cache is not None and not reload:
        return _cache
    user = {}
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                user = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"Settings file can't be read ({e}). Fix or delete it: {path}")
    _cache = validate(_merge(DEFAULTS, user))
    return _cache


def _kind(v):
    return "true/false" if isinstance(v, bool) else "a whole number" if isinstance(v, int) else \
        "a list of text" if isinstance(v, list) else "text"


def validate(cfg):
    """Every value must have its default's type; lists hold text; folders to exclude are absolute."""
    where = f" File: {config_path()}"
    for section, values in DEFAULTS.items():
        for k, default in values.items():
            v = cfg[section][k]
            ok = isinstance(v, type(default)) and not (isinstance(v, bool) and not isinstance(default, bool))
            if ok and isinstance(default, list):
                ok = all(isinstance(x, str) and x.strip() for x in v)
            if not ok:
                raise SystemExit(f"Settings: {section}.{k} must be {_kind(default)}, not {v!r}.{where}")
    mode = cfg["refresh"]["mode"]
    if mode not in ("auto", "manual", "off"):
        raise SystemExit(f"Settings: refresh.mode must be auto, manual or off (it is {mode!r}).{where}")
    if cfg["retention"]["keep"] not in ("all", "auto", "main", "none"):
        raise SystemExit(f"Settings: retention.keep must be all, auto, main or none (it is {cfg['retention']['keep']!r}).{where}")
    if cfg["refresh"]["interval_minutes"] < 5:
        raise SystemExit(f"Settings: refresh.interval_minutes must be at least 5.{where}")
    for p in cfg["privacy"]["exclude_paths"]:
        if not (p.startswith("/") or p.startswith("~")):
            raise SystemExit(f"Settings: privacy.exclude_paths needs full folder paths (starting with / or ~), "
                             f"not {p!r}.{where}")
    return cfg


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(cfg):
    """Write the settings file with a comment over each section."""
    global _cache
    cfg = validate(cfg)
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["# Settings for smem (Session Memory). Edit values freely; `smem settings` shows the result.",
             "# `smem settings KEY VALUE` and `smem setup` rewrite this file, so comments you add aren't kept.", ""]
    for section, values in cfg.items():
        lines.append(f"# {COMMENTS.get(section, '')}")
        lines.append(f"[{section}]")
        for k, v in values.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)
    _cache = cfg
    return path


def set_value(dotted, raw):
    """`section.key` = value, parsed as TOML (so true, 15, ["a"] and "text" all work)."""
    section, _, key = dotted.partition(".")
    cfg = copy.deepcopy(load())
    if section not in cfg or key not in cfg[section]:
        raise SystemExit(f"Unknown setting {dotted!r}. Run `smem settings` to see the names.")
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw  # plain words are taken as text
    cfg[section][key] = value
    return save(cfg)  # validates first; a refused value leaves the file as it was


def codex_home(cfg=None):
    """Where Codex keeps its data. Codex itself honors $CODEX_HOME, so this does too."""
    cfg = cfg or load()
    return os.path.expanduser(cfg["sources"]["codex_home"] or os.environ.get("CODEX_HOME") or "~/.codex")


def claude_home(cfg=None):
    """Where Claude Code keeps its data. Claude Code itself honors $CLAUDE_CONFIG_DIR, so this does too."""
    cfg = cfg or load()
    return os.path.expanduser(cfg["sources"]["claude_home"] or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def skills_dir(agent, cfg=None):
    cfg = cfg or load()
    chosen = cfg["skills"][f"{agent}_dir"]
    if chosen:
        return os.path.expanduser(chosen)
    return os.path.join(claude_home(cfg), "skills") if agent == "claude" else os.path.expanduser("~/.agents/skills")


def apply(cfg=None):
    """Point the source readers at the configured folders."""
    from . import claude, codex
    cfg = cfg or load()
    codex.CODEX_HOME = codex_home(cfg)
    codex.EXTRA_LOG_DIRS = list(cfg["sources"]["codex_extra_log_dirs"])
    claude.CLAUDE_PROJECTS = os.path.join(claude_home(cfg), "projects")
    return cfg
