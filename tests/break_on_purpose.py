"""Break each safety rule on purpose and check that a test catches it.

Run: python3 -I tests/break_on_purpose.py (from the repository folder; takes a few minutes).

Each case copies src/ and tests/ into a throwaway folder, changes one line so a safety rule stops working,
and runs the test file that should notice. A case passes when that test file fails. Your copy of the code
is never changed.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (what the break does, file, text to replace, replacement, test file that must fail)
CASES = [
    ("reading opens the memory for writing", "cli.py",
     "return store.connect_readonly()", "return store.connect()", "test_package.py"),
    ("`smem sql` may write (no read-only guard)", "store.py",
     "    con.set_authorizer(_read_only_authorizer)", "    pass", "test_package.py"),
    ("the memory folder is created readable by everyone", "store.py",
     "os.makedirs(folder, mode=0o700)", "os.makedirs(folder)", "test_package.py"),
    ("reading Codex's database creates files in its folder", "codex.py",
     '("?mode=ro" if pending else "?mode=ro&immutable=1")', '"?mode=ro"', "test_package.py"),
    ("shell command output is stored as a request", "claude.py",
     "|bash-stdout|bash-stderr|", "|", "test_package.py"),
    ("Stripe secret keys aren't redacted", "store.py",
     "(?:sk|rk|pk)_(?:live|test)_", "(?:rk|pk)_(?:live|test)_", "test_redaction.py"),
    ("Claude Code may run `smem update --force` and setup without asking", "skills.py",
     'READ_COMMANDS = ("search",', 'READ_COMMANDS = ("update", "setup", "search",', "test_package.py"),
    ("an excluded folder also excludes its siblings", "ingest.py",
     'return any(_FOLD(folder) == re.sub(r"[^A-Za-z0-9]", "-", p) for p in excluded_paths)',
     'return any(_FOLD(folder).startswith(re.sub(r"[^A-Za-z0-9]", "-", p)) for p in excluded_paths)', "test_package.py"),
    ("sessions stored before an exclusion stay", "ingest.py",
     '        _drop(con, agent, late, stats, "excluded_removed")', "        late = set()", "test_package.py"),
    ("excluded text stays in the file", "ingest.py",
     "        store.purge_deleted(con)  # removed", "        pass  # removed", "test_package.py"),
    ("a forgotten session comes back at the next update", "ingest.py",
     'if a[1] in forgotten.get("claude", ()) or _claude_dir_excluded', "if _claude_dir_excluded", "test_package.py"),
    ("sessions are never kept after their logs are deleted", "ingest.py",
     '    return (keep == "all" or', '    return False and (keep == "all" or', "test_package.py"),
    ("a full rebuild drops kept sessions", "ingest.py",
     "    ids = [s for s in missing if _keeps(keep, old[s][0], how[s])]", "    ids = []", "test_package.py"),
    ("an unplugged drive is taken for deletion", "ingest.py",
     "    if len(recent) > MAX_VANISH_SHARE * max(1, len(rows)) and not accept_removals:", "    if False:",
     "test_package.py"),
    ("a normal cleanup after weeks away raises the unplugged-drive alarm", "ingest.py",
     '    recent = [s for s, how in new.items() if how != "auto"]\n    if len(recent) > MAX_VANISH_SHARE * max(1, len(rows))',
     "    recent = list(new)\n    if len(recent) > MAX_VANISH_SHARE * max(1, len(rows))", "test_package.py"),
    ("no deletion is ever recognised as Claude Code's own cleanup", "ingest.py",
     "            if age >= cleanup - 1:", "            if False:", "test_package.py"),
    ("keep = auto keeps sessions the user deleted", "ingest.py",
     '            or (keep == "auto" and how == "auto"))', '            or keep == "auto")', "test_package.py"),
    ("the user's own Claude Code cleanup period is ignored", "claude.py",
     "            return days, True", "            return 30, False", "test_package.py"),
    ("no warning when many younger logs vanish at once", "health.py",
     "        if batch >= RETENTION_BATCH:", "        if False:", "test_package.py"),
    ("the background job ignores the refresh mode", "cli.py",
     '    if a.scheduled and mode != "auto":\n        return', '    if False:\n        return', "test_package.py"),
    ("Claude Code workflow helper logs are missed", "claude.py",
     '"subagents", "**", "*.jsonl"), recursive=True', '"subagents", "*.jsonl"), recursive=True', "test_package.py"),
]


def failures(folder, test_file):
    r = subprocess.run([sys.executable, "-I", os.path.join("tests", test_file)], cwd=folder,
                       capture_output=True, text=True)
    return r.returncode != 0 or any(line.startswith(("FAIL", "ERROR")) for line in r.stdout.splitlines())


def main():
    caught = 0
    with tempfile.TemporaryDirectory() as base:
        clean = os.path.join(base, "clean")
        for part in ("src", "tests"):
            shutil.copytree(os.path.join(ROOT, part), os.path.join(clean, part))
        for test_file in sorted({c[4] for c in CASES}):
            if failures(clean, test_file):
                sys.exit(f"{test_file} already fails on the unchanged code; fix that first.")
        for i, (what, name, old, new, test_file) in enumerate(CASES, 1):
            folder = os.path.join(base, f"case{i}")
            shutil.copytree(clean, folder)
            path = os.path.join(folder, "src", "session_memory", name)
            text = open(path).read()
            if text.count(old) != 1:
                print(f"SKIP  {what}: the line to change wasn't found once in {name} (the code moved on)")
                continue
            open(path, "w").write(text.replace(old, new))
            ok = failures(folder, test_file)
            caught += ok
            print(f"{'CAUGHT' if ok else 'MISSED'}  {what}  ({name}, {test_file})")
    print(f"{caught} of {len(CASES)} deliberate breaks were caught by the tests.")
    sys.exit(0 if caught == len(CASES) else 1)


if __name__ == "__main__":
    main()
