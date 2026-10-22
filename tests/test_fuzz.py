"""Random and broken input for the parts that read untrusted text: the secret redactor, both log readers, and
search. Each must finish quickly without crashing, and planted secrets must still be removed. The inputs are
random but repeatable: set FUZZ_SEED to replay a run and FUZZ_ITERATIONS for a longer one.

Run: python3 -I tests/test_fuzz.py (from the repository folder)."""
import json
import os
import random
import shutil
import string
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
SANDBOX = tempfile.mkdtemp(prefix="smem-test-")
for var, sub in {"HOME": "home", "SESSION_MEMORY_HOME": "data", "SESSION_MEMORY_CONFIG": "config/config.toml",
                 "CODEX_HOME": "home/.codex", "CLAUDE_CONFIG_DIR": "home/.claude"}.items():
    os.environ[var] = os.path.join(SANDBOX, sub)
from session_memory import claude, codex, recall, store  # noqa: E402

SEED = int(os.environ.get("FUZZ_SEED", "20261022"))
N = int(os.environ.get("FUZZ_ITERATIONS", "150"))
rng = random.Random(SEED)

PIECES = ["sk-", "sk-ant-api03-", "ghp_", "AKIA", "password=", "token: ", "\"api_key\": \"", "Bearer ", "-----BEGIN PRIVATE KEY-----",
          "-----END PRIVATE KEY-----", "postgres://u:", "@", ":", "=", "\"", "'", "\\", "\\\"", "\n", " ", "-", "_", "/", ".",
          "{", "}", "[", "]", "é", "日本", "‮", "\x1b[2J", "\x00", "**", "|", "# Next steps\n- ", "- [ ] "]


def noise(n):
    out = []
    while sum(map(len, out)) < n:
        r = rng.random()
        if r < 0.35:
            out.append(rng.choice(PIECES))
        elif r < 0.7:
            out.append("".join(rng.choices(string.ascii_letters + string.digits, k=rng.randint(1, 40))))
        else:
            out.append(rng.choice(["a-" * rng.randint(1, 500), "a_" * rng.randint(1, 500), "=" * rng.randint(1, 200),
                                   "x" * rng.randint(1, 3000), "\\" * rng.randint(1, 50)]))
    return "".join(out)[:n]


def secret():
    return "ghp_" + "".join(rng.choices(string.ascii_letters + string.digits, k=36))


def test_redaction_survives_random_input_and_still_removes_secrets():
    worst = 0.0
    for _ in range(N):
        planted = secret()
        text = noise(rng.randint(0, 20000)) + f" {planted} " + noise(rng.randint(0, 20000))
        t = time.perf_counter()
        out, _ = store.scrub(text)
        worst = max(worst, time.perf_counter() - t)
        assert planted not in out, (SEED, planted)
    long = noise(250_000)
    t = time.perf_counter()
    store.scrub(long)
    worst = max(worst, time.perf_counter() - t)
    print(f"     {N + 1} random inputs, slowest {worst * 1000:.0f} ms (seed {SEED})")
    assert worst < 1.0, worst


def mangle(value):
    """A record field replaced with something of the wrong shape."""
    return rng.choice([None, 5, "", [], {}, [1, 2], {"x": None}, noise(50), [noise(10), None], True, -1, 1e308])


CLAUDE_SHAPES = [
    lambda: {"type": "user", "cwd": "/w", "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": noise(80)}},
    lambda: {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": noise(40)},
                                                                     {"type": "tool_result", "tool_use_id": "1", "content": noise(30)}]}},
    lambda: {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": noise(60)}, {"type": "tool_use", "id": "1", "name": rng.choice(["Bash", "Edit", "TodoWrite",
                                                                                             "mcp__a__update_plan", "Agent"]),
                                              "input": rng.choice([{"command": noise(20)}, {"file_path": noise(10)},
                                                                   {"todos": [{"content": noise(5), "status": "pending"}]},
                                                                   {"items": mangle(0)}, {"advance": True}, {"done": [1, 99, -3]},
                                                                   {"start": "x"}, mangle(0)])}]}},
    lambda: {"type": "custom-title", "customTitle": mangle(0)},
    lambda: {"type": noise(8), "payload": mangle(0)},
]
CODEX_SHAPES = [
    lambda: {"type": "session_meta", "payload": {"id": noise(8), "cwd": mangle(0), "source": mangle(0)}},
    lambda: {"type": "event_msg", "payload": {"type": "task_started"}},
    lambda: {"type": "event_msg", "payload": {"type": "user_message", "message": rng.choice([noise(50), mangle(0)])}},
    lambda: {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": rng.choice(["UserMessage", "AgentMessage", "CommandExecution", "FileChange", "Weird"]),
        "content": mangle(0), "command": mangle(0), "changes": mangle(0)}}},
    lambda: {"type": "response_item", "payload": {"type": "function_call", "name": rng.choice(["update_plan", "exec_command",
                                                                                               "apply_patch"]),
                                                  "arguments": rng.choice([noise(40), json.dumps({"plan": mangle(0)}),
                                                                           json.dumps({"plan": [{"step": 1, "status": None}]})])}},
    lambda: {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": mangle(0)}},
    lambda: {"type": noise(6), "ordinal": rng.choice([1, "x", None]), "payload": mangle(0)},
]


def jsonl(shapes, folder, name):
    path = os.path.join(folder, name)
    bad = 0
    with open(path, "w", encoding="utf-8", errors="surrogatepass") as f:
        for _ in range(rng.randint(0, 40)):
            r = rng.random()
            if r < 0.12:
                f.write(noise(rng.randint(1, 80)).replace("\n", " ") + "\n")  # not JSON
                bad += 1
            elif r < 0.18:
                f.write(rng.choice(["5", "[]", "null", "\"text\""]) + "\n")  # JSON, but not a record
            else:
                rec = rng.choice(shapes)()
                if rng.random() < 0.3:
                    rec[rng.choice(list(rec))] = mangle(0)
                f.write(json.dumps(rec) + "\n")
    return path, bad


def test_log_readers_never_crash_on_broken_logs():
    tmp = tempfile.mkdtemp()
    try:
        for i in range(N):
            path, bad = jsonl(CLAUDE_SHAPES, tmp, f"c{i}.jsonl")
            r = claude.parse_log(path, f"s{i}", None)
            assert isinstance(r["turns"], list) and r["parse_errors"] >= 0, SEED
            path, bad = jsonl(CODEX_SHAPES, tmp, f"rollout-2026-10-01T10-00-00-aaaaaaaa-0000-0000-0000-{i:012d}.jsonl")
            r = codex.parse_thread([path])
            assert isinstance(r["turns"], list) and r["parse_errors"] >= 0, SEED
    finally:
        shutil.rmtree(tmp)


def test_search_never_crashes_on_odd_queries():
    con = store.connect()
    store.replace_session(con, {"agent": "claude", "session_id": "s", "kind": "top", "log_path": "p", "files": {},
                                "turns": [{"request": noise(200), "answer": noise(200)}]})
    con.commit()
    odd = ['"', "AND", "OR OR", "NOT", "*", "a*b", "(", ")", "near/2", "request:", ":", "‮", "日本 語", "-", "x" * 500,
           "'", "\\", "''", "\"unterminated", "a NEAR b", "{x}", "^", "col:val other"]
    for q in odd + [noise(rng.randint(1, 60)) for _ in range(N)]:
        for mine in (False, True):
            try:
                recall.search(con, q, mine=mine)
            except ValueError:
                pass  # the query couldn't be read even with every word quoted: reported, not a crash
    con.close()


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
                    print(f"ERROR {name}: {type(e).__name__}: {e} (seed {SEED})")
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(1 if failed else 0)
