"""The documents' checkable claims, checked against the code: counts the README cites, the tests that SECURITY.md
names, and every `smem` command and option shown in the README, the skill and the install guide. A claim that
drifts from the code fails here instead of misleading a reader.

Run: python3 -I tests/test_docs.py (from the repository folder)."""
import argparse
import ast
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
SANDBOX = tempfile.mkdtemp(prefix="smem-test-")
for var, sub in {"HOME": "home", "SESSION_MEMORY_HOME": "data", "SESSION_MEMORY_CONFIG": "config/config.toml",
                 "CODEX_HOME": "home/.codex", "CLAUDE_CONFIG_DIR": "home/.claude"}.items():
    os.environ[var] = os.path.join(SANDBOX, sub)
from session_memory import cli, health  # noqa: E402

SKILL = os.path.join("src", "session_memory", "skill_templates", "SKILL.md")
DOCS = ("README.md", "AGENT_INSTALL.md", SKILL, "SECURITY.md")
NETWORK = r"(socket|ssl|http|urllib\.request|urllib3|requests|httpx|aiohttp|ftplib|smtplib|xmlrpc|websocket)"


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def all_test_files():
    return sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))


def tests_in(name):
    return re.findall(r"^def (test_\w+)", read(os.path.join("tests", name)), re.M)


def literal_lengths(path):
    """The length of each list or tuple assigned at the top of a file, by name, without running it."""
    out = {}
    for node in ast.parse(read(path)).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = len(node.value.elts)
    return out


def test_readme_counts_match_the_code():
    readme = read("README.md")
    total = sum(len(tests_in(f)) for f in all_test_files())
    assert re.search(rf"\*\*{total} automated tests\*\*", readme), f"README should say {total} automated tests"
    breaks = literal_lengths(os.path.join("tests", "break_on_purpose.py"))["CASES"]
    assert f"All {breaks} breaks are caught" in readme, f"README should say all {breaks} breaks"
    red = literal_lengths(os.path.join("tests", "test_redaction.py"))
    assert f"{red['REVIEW_CASES'] + red['MORE_CASES']} realistic fake secrets" in readme
    assert f"{red['KEEP']} ordinary snippets" in readme
    lines = read("install.sh").count("\n")
    assert f"[install.sh](install.sh) ({lines} lines)" in readme, f"install.sh has {lines} lines"
    outcomes = health.selftest()
    assert all(ok for ok, _, _ in outcomes), [m for ok, m, _ in outcomes if not ok]
    # Each case's expected verdict is in the source: GREEN for the quiet ones, RED for the breakages.
    src = read(os.path.join("src", "session_memory", "health.py"))
    cases = src[src.index("    cases = ("):src.index("    outcomes = []")]
    quiet, caught = cases.count(", GREEN, "), cases.count(", RED, ")
    assert quiet + caught == len(outcomes)
    words = {2: "two", 5: "five"}
    assert f"catch {caught} kinds of breakage in built-in samples, and stay quiet in {quiet} normal ones" in readme
    assert f"catching {words.get(caught, caught)} kinds of breakage" in readme
    assert f"quiet in {words.get(quiet, quiet)} normal situations" in readme


def test_python_versions_match_the_package_and_ci():
    readme, ci, project = read("README.md"), read(".github/workflows/tests.yml"), read("pyproject.toml")
    lowest = re.search(r'requires-python\s*=\s*">=(3\.\d+)"', project).group(1)
    versions = re.findall(r'"(3\.\d+)"', re.search(r"python: \[([^\]]*)\]", ci).group(1))
    assert versions[0] == lowest, (versions, lowest)
    assert f"on Python {versions[0]} to {versions[-1]}" in readme, versions
    assert "for t in tests/test_*.py" in ci  # so a new test file runs without editing the CI


def test_tests_named_in_the_docs_exist():
    for doc in ("README.md", "SECURITY.md"):
        text = read(doc)
        for name, path in re.findall(r"\[`(test_\w+)`\]\((tests/test_\w+\.py)\)", text):
            assert name in tests_in(os.path.basename(path)), f"{doc}: {name} isn't in {path}"
        for name in set(re.findall(r"`(test_\w+)`", text)):
            assert any(name in tests_in(f) for f in all_test_files()), f"{doc}: no test named {name}"
        for path in set(re.findall(r"tests/(\w+\.py)", text)):
            assert os.path.exists(os.path.join(HERE, path)), f"{doc}: tests/{path} doesn't exist"


def commands():
    """{command: its option strings}, with "" for the options before any command."""
    ap = cli.parser()
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    out = {name: set(p._option_string_actions) for name, p in sub.choices.items()}
    out[""] = set(ap._option_string_actions)
    return out


def code_in(text):
    """Inline code and the lines of fenced code blocks (comments dropped), leaving out diagrams."""
    spans = []
    for lang, block in re.findall(r"^```(\w*)\n(.*?)^```", text, re.M | re.S):
        if lang != "mermaid":
            spans += [line.split(" #")[0] for line in block.splitlines()]
    no_blocks = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
    return spans + re.findall(r"`([^`\n]+)`", no_blocks)


def check_use(known, cmd, flags, where):
    assert cmd in known, f"{where}: `smem {cmd}` isn't a command"
    for flag in flags:
        assert flag in known[cmd], f"{where}: `smem {cmd}` has no option {flag}"


def test_every_command_and_option_shown_exists():
    known = commands()
    seen = set()
    for doc in DOCS:
        text = read(doc)
        for span in code_in(text):
            for m in re.finditer(r"(?<![\w/.-])smem\s+(--?[a-z-]+|[a-z][a-z-]*)\b(.*)", span):
                first, rest = m.group(1), m.group(2)
                cmd = "" if first.startswith("-") else first
                flags = ([first] if first.startswith("-") else []) + re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest)
                check_use(known, cmd, flags, doc)
                seen.add(cmd)
        # In a table row that starts with a command, every option in the row belongs to that command.
        for row in re.findall(r"^\| `smem ([a-z]+)[^|]*\|(.*)\|\s*$", text, re.M):
            check_use(known, row[0], re.findall(r"`(--[a-z][a-z-]*)", row[1]), doc + " table")
    reading = {"catchup", "search", "session", "brief", "handoff", "timeline", "decisions", "recent", "project", "files",
               "goals", "links", "stats", "sql", "schema"}
    assert reading <= seen, f"commands the docs never show: {sorted(reading - seen)}"
    skill = read(SKILL)
    missing = [c for c in reading if f"`smem {c}" not in skill]
    assert not missing, f"the skill doesn't describe: {missing}"


def test_skill_placeholders_are_the_ones_filled_in():
    found = set(re.findall(r"{{(\w+)}}", read(SKILL)))
    filled = set(re.findall(r'"{{(\w+)}}"', read(os.path.join("src", "session_memory", "skills.py"))))
    assert found == filled == {"FRONTMATTER_EXTRA", "COMMAND_NOTE"}, (found, filled)


def test_no_network_library_is_imported():
    pattern = re.compile(r"^\s*(import|from)\s+" + NETWORK + r"\b", re.M)
    for folder, _, names in os.walk(os.path.join(ROOT, "src")):
        for name in names:
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    assert not pattern.search(f.read()), f"{name} imports a network library"
    assert NETWORK in read("README.md"), "the README's grep should look for the same libraries"


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
