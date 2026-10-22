"""Search quality, measured: labelled questions over a small realistic history, each with the session that
answers it, scored by mean reciprocal rank (1 when the right session is first, 1/2 when second...) and by how
often it's in the top 3. The history includes what makes real searches hard: the session that's asking (it
mentions everything), sessions that recalled others (they quote the same words), and a long session that
mentions topics in passing. The old ranking (one row per matching turn, plain relevance, nothing left out) is
scored too, so a change to ranking can be judged by numbers, not impressions.

Run: python3 -I tests/test_search_quality.py (from the repository folder)."""
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
SANDBOX = tempfile.mkdtemp(prefix="smem-test-")
for var, sub in {"HOME": "home", "SESSION_MEMORY_HOME": "data", "SESSION_MEMORY_CONFIG": "config/config.toml",
                 "CODEX_HOME": "home/.codex", "CLAUDE_CONFIG_DIR": "home/.claude"}.items():
    os.environ[var] = os.path.join(SANDBOX, sub)
for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
    os.environ.pop(var, None)
from session_memory import recall, store  # noqa: E402

# (id, name, [(request, answer)], recalls) — a small but realistic history
HISTORY = [
    ("csv-importer", "CSV importer for orders", [
        ("build an importer for the orders csv files", "The importer reads orders CSV files, handles quoted commas, and "
                                                       "skips the header row."),
        ("it fails on semicolons", "Added a delimiter sniffer so semicolon CSV exports import too.")], False),
    ("excel-importer", "Excel support", [("can the importer take xlsx files too?",
                                          "Yes: Excel import now goes through openpyxl and reuses the CSV mapping.")], False),
    ("invoice-nightly", "Nightly invoice export", [("export the invoices every night",
                                                    "Invoice export runs nightly at 2am through cron and writes to exports/.")], False),
    ("invoice-template", "Invoice PDF", [("put our logo on the invoice", "The invoice PDF template now shows the logo "
                                                                         "top left.")], False),
    ("auth-oauth", "Google login", [("google login fails after the callback",
                                     "Fixed the OAuth redirect URI mismatch; the callback now uses the https origin.")], False),
    ("auth-logout", "Logout bug", [("logging out doesn't log me out", "The session cookie kept its expiry; logout now "
                                                                      "clears the cookie and the server session.")], False),
    ("db-migration", "Postgres 16 move", [("move the database to postgres 16", "Upgraded with pg_upgrade, checked row "
                                                                               "counts, and switched the connection string.")], False),
    ("db-index", "Slow orders page", [("the orders page is slow", "The orders query scanned the table; an index on "
                                                                   "created_at took it from 2s to 30ms.")], False),
    ("deploy-fly", "Fly deploy", [("deploy the api to fly", "Deployed with flyctl; secrets set, health check green.")], False),
    ("deploy-rollback", "Bad release", [("the release broke checkout, undo it", "Rolled back to the previous release "
                                                                                 "and pinned the image tag.")], False),
    ("rate-limit", "Gateway limits", [("add rate limiting to the gateway", "Rate limiting uses a token bucket per API "
                                                                           "key: 60 requests a minute.")], False),
    ("webhook", "Stripe webhooks", [("stripe webhooks arrive twice", "Verified the Stripe signature and made the handler "
                                                                     "idempotent on the event id.")], False),
    ("product-search", "Product search", [("let people search products", "Product search uses SQLite full-text search "
                                                                          "with prefix matching.")], False),
    ("screenshot-tool", None, [("make something that tells me what changed between two screenshots",
                                "Built a small tool that compares screenshots pixel by pixel and outlines the changes.")], False),
    ("nginx-script", None, [("write a script that summarizes nginx logs by status code",
                             "The script reads access logs and prints counts per status code and the slowest paths.")], False),
    ("digest", "Weekly digest", [("send people a weekly email", "The digest email goes out on Mondays with last week's "
                                                                "activity.")], False),
    ("flaky", "Flaky checkout test", [("the checkout test fails sometimes", "The test raced the payment mock; it now waits "
                                                                            "for the mock before checking out.")], False),
    ("dark-mode", "Dark mode", [("add a dark mode", "Dark mode follows prefers-color-scheme through CSS variables.")], False),
    ("french", "French translations", [("translate the app to french", "Added French strings and a language switch.")], False),
    ("backups", "Database backups", [("back up the database every night", "Nightly backups go to S3 with 30 days of "
                                                                          "retention.")], False),
    ("long-misc", "Docs cleanup", [
        ("tidy the docs", "Reworded the deploy section; don't deploy on Fridays, and the importer page needs screenshots."),
        ("more docs", "The invoice and backup pages now link to each other; OAuth and logout notes moved to auth."),
        ("and the faq", "FAQ: rate limiting, webhooks, dark mode, translations and search each have an entry.")], False),
    ("recall-1", None, [("what did we do about invoices and backups and the importer?",
                         "From memory: invoice export runs nightly at 2am; invoice PDF has the logo; nightly backups go "
                         "to S3; the CSV importer handles quoted commas; Excel import uses openpyxl.")], True),
    ("recall-2", None, [("remind me about deploys", "From memory: deployed the api to Fly with flyctl; rolled back a bad "
                                                     "release and pinned the image tag; rate limiting on the gateway.")], True),
    ("asking", None, [("catch me up on invoices, importer, oauth login, logout cookie, postgres, orders index, fly "
                       "deploy, rollback, rate limiting, stripe webhooks, screenshots, nginx logs, digest, flaky test, "
                       "dark mode, french, backups", "Searching your history for those topics now.")], False),
]

QUESTIONS = [  # (what someone would type, the session that answers it)
    ("csv importer", "csv-importer"),
    ("tool that compares screenshots", "screenshot-tool"),
    ("nginx logs script", "nginx-script"),
    ("nightly invoice export", "invoice-nightly"),
    ("invoice logo", "invoice-template"),
    ("oauth redirect", "auth-oauth"),
    ("logout cookie", "auth-logout"),
    ("postgres upgrade", "db-migration"),
    ("slow orders query index", "db-index"),
    ("deploy fly", "deploy-fly"),
    ("rollback release", "deploy-rollback"),
    ("rate limiting gateway", "rate-limit"),
    ("stripe webhook signature", "webhook"),
    ("excel import", "excel-importer"),
    ("backups s3", "backups"),
    ("flaky checkout test", "flaky"),
    ("dark mode css", "dark-mode"),
    ("french translations", "french"),
    ("weekly digest email", "digest"),
    ("product search full text", "product-search"),
]
NOTHING = ["kubernetes helm chart", "quarterly tax filing"]  # nothing in the history answers these


def build():
    con = store.connect()
    now = time.time()
    for n, (sid, name, turns, recalls) in enumerate(HISTORY):
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - (len(HISTORY) - n) * 3600))
        store.replace_session(con, {
            "agent": "claude", "session_id": sid, "kind": "top", "name": name, "project": "shop", "cwd": "/work/shop",
            "log_path": f"/logs/{sid}", "created_at": when, "updated_at": when, "files": {},
            "turns": [{"request": q, "answer": a, "started_at": when, "recalls": int(recalls)} for q, a in turns]})
    con.commit()
    return con


def smem_ranking(con, q):
    with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "asking"}):
        return [r["session"]["session_id"] for r in recall.search(con, q, limit=10)["results"]]


def old_ranking(con, q):
    """One row per matching turn by plain relevance, sessions in order of first appearance, nothing left out."""
    seen = []
    for (sid,) in con.execute("select session_id from turns_fts where turns_fts match ? order by bm25(turns_fts) limit 50",
                              (" ".join(f'"{w}"' for w in recall.words(q)),)):
        if sid not in seen:
            seen.append(sid)
    return seen[:10]


def score(con, rank):
    rr = top3 = 0.0
    for q, want in QUESTIONS:
        got = rank(con, q)
        if want in got:
            rr += 1 / (got.index(want) + 1)
            top3 += got.index(want) < 3
    return rr / len(QUESTIONS), top3 / len(QUESTIONS)


def test_ranking_finds_the_right_session_first():
    con = build()
    mrr, top3 = score(con, smem_ranking)
    old_mrr, old_top3 = score(con, old_ranking)
    print(f"     smem ranking: MRR {mrr:.3f}, in top 3 {top3:.0%}; old ranking: MRR {old_mrr:.3f}, in top 3 {old_top3:.0%}")
    assert mrr >= 0.95 and top3 == 1.0, (mrr, top3)
    assert mrr - old_mrr >= 0.05, (mrr, old_mrr)
    for q, want in QUESTIONS:
        assert "asking" not in smem_ranking(con, q), q
    con.close()


def test_questions_with_no_answer_return_nothing_relevant():
    con = store.connect()
    for q in NOTHING:
        got = smem_ranking(con, q)
        assert got == [], (q, got)
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
                    print(f"ERROR {name}: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(1 if failed else 0)
