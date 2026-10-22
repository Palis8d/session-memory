"""Tests for secret redaction (store.clean / store.scrub): realistic secret formats, ordinary text that must stay
as it is, the truncation contract, and speed on long adversarial text.
Run: python3 -I tests/test_redaction.py (from the repository folder). Nothing outside a throwaway folder is touched.
Every secret below is synthetic; most are built by joining two strings so secret scanners leave this file alone."""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
SANDBOX = tempfile.mkdtemp(prefix="smem-test-")
ENV = {"HOME": "home", "SESSION_MEMORY_HOME": "data", "SESSION_MEMORY_CONFIG": "config/config.toml",
       "CODEX_HOME": "home/.codex", "CLAUDE_CONFIG_DIR": "home/.claude", "XDG_CONFIG_HOME": "home/.config",
       "XDG_DATA_HOME": "home/.local/share"}
for var, sub in ENV.items():
    os.environ[var] = os.path.join(SANDBOX, sub)
os.environ["SESSION_MEMORY_DRY_SYSTEM"] = "1"
from session_memory import store  # noqa: E402

AWS_ID = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GH_FINE = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWX"
OAI = "sk-proj-" + "Ab3dEf6hIj9kLmNoPqRsTuVwXyZ0123456789abcdEFGH"
OAI_OLD = "sk-" + "Ab3dEf6hIj9kLmNoPqRsTuVwXyZ01234567890abcdEFGHij"
ANT = "sk-ant-api03-" + "Ab3dEf6hIj9kLmNoPqRsTuVwXyZ0123456789abcdEFGH-ijklmnopQRSTUV_wxyz012345-ABCDEFGAA"
STRIPE_SK = "sk_live_" + "51HabcdEFGhijkLMNopqRSTuvwXYZ0123456789abcdef"
STRIPE_TEST = "sk_test_" + "4eC39HqLyjWDarjtT1zdp7dc"
STRIPE_RK = "rk_live_" + "51HabcdEFGhijkLMNopqRSTuv"
STRIPE_WH = "whsec_" + "MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLSGQ"
GCP_KEY = "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
GCP_SA = ('{"type": "service_account", "project_id": "demo", "private_key_id": "' +
          "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b" + '", "private_key": "-----BEGIN PRIVATE KEY-----\\n' +
          'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\\n-----END PRIVATE KEY-----\\n", '
          '"client_email": "x@demo.iam.gserviceaccount.com"}')
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0." +
       "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
SLACK_WEBHOOK = "https://hooks.slack.com/services/" + "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
NPM = "npm_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GLPAT = "glpat-" + "xxxxxxxxxxxxxxxxxxxx"
SENDGRID = "SG." + "abcdefghijklmnopqrstuv.ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
RESEND = "re_" + "123abcde_ABCDEFGHIJKLMNOPQRSTUV"
HF = "hf_" + "abcdefghijklmnopqrstuvwxyzABCDEF"
SUPA = "sbp_" + "0123456789abcdef0123456789abcdef01234567"
DO = "dop_v1_" + "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
AZURE_KEY = "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4" + "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4QQ=="
AZURE = ("DefaultEndpointsProtocol=https;AccountName=demo;AccountKey=" + AZURE_KEY +
         ";EndpointSuffix=core.windows.net")
PW = "Tr0ub4dor" + "&3xyzzy"

# (name, text, secret) from the security review's harness. One review case is deliberately left out: a bare
# password in free prose ("log in as admin with Tr0ub4dor&3xyzzy") has no key, prefix or shape to recognise
# without also blanking ordinary words.
REVIEW_CASES = [
    ("AWS id in prose", f"my key is {AWS_ID} ok", AWS_ID),
    ("AWS secret bare (.aws/credentials)", f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET),
    ("AWS secret as env", f"export AWS_SECRET_ACCESS_KEY={AWS_SECRET}", AWS_SECRET),
    ("AWS secret in prose", f"the secret is {AWS_SECRET}", AWS_SECRET),
    ("AWS secret JSON SecretAccessKey", json.dumps({"AccessKeyId": AWS_ID, "SecretAccessKey": AWS_SECRET}), AWS_SECRET),
    ("AWS session token", f"AWS_SESSION_TOKEN=FwoGZXIvYXdzEBYaDH{AWS_SECRET}", AWS_SECRET),
    ("GitHub classic", f"use {GH}", GH),
    ("GitHub fine-grained", f"use {GH_FINE}", GH_FINE),
    ("OpenAI proj", f"OPENAI_API_KEY={OAI}", OAI),
    ("OpenAI proj in prose", f"here's my key {OAI}", OAI),
    ("OpenAI legacy", f"key {OAI_OLD}", OAI_OLD),
    ("Anthropic", f"x-api-key: {ANT}", ANT),
    ("Anthropic prose", f"try {ANT} now", ANT),
    ("Stripe sk_live", f"Stripe.api_key = '{STRIPE_SK}'", STRIPE_SK),
    ("Stripe sk_live prose", f"charge with {STRIPE_SK}", STRIPE_SK),
    ("Stripe sk_test prose", f"charge with {STRIPE_TEST}", STRIPE_TEST),
    ("Stripe rk_live", f"restricted {STRIPE_RK}", STRIPE_RK),
    ("Stripe webhook secret", f"STRIPE_WEBHOOK_SECRET={STRIPE_WH}", STRIPE_WH),
    ("Stripe webhook secret prose", f"signing secret is {STRIPE_WH}", STRIPE_WH),
    ("GCP API key", f"maps key {GCP_KEY}", GCP_KEY),
    ("GCP service account JSON", GCP_SA, "MIIEvQIBADANBg"),
    ("GCP SA private_key_id", GCP_SA, "1a2b3c4d5e6f7a8b9c0d"),
    ("JWT bare", f"token {JWT}", JWT),
    ("JWT in JSON", json.dumps({"id_token": JWT}), JWT),
    ("JWT in Authorization header", f"curl -H 'Authorization: Bearer {JWT}' https://api", JWT),
    ("Bearer short opaque", "curl -H 'Authorization: Bearer abcdef0123456789xyzQ' https://api", "abcdef0123456789xyzQ"),
    ("Basic auth header", "curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==' https://api",
     "dXNlcjpwYXNzd29yZDEyMw=="),
    ("curl -u", "curl -u admin:" + PW + " https://api.example.com", PW),
    ("Slack bot", f"{SLACK}", SLACK),
    ("Slack webhook URL", f"post to {SLACK_WEBHOOK}", "XXXXXXXXXXXXXXXXXXXXXXXX"),
    ("npm token", f"//registry.npmjs.org/:_authToken={NPM}", NPM),
    ("npm token prose", f"token is {NPM}", NPM),
    ("GitLab PAT", f"use {GLPAT}", GLPAT),
    ("SendGrid", f"SENDGRID_API_KEY={SENDGRID}", SENDGRID),
    ("SendGrid prose", f"use {SENDGRID}", SENDGRID),
    ("Resend", f"use {RESEND}", RESEND),
    ("HF", f"use {HF}", HF),
    ("Supabase PAT", f"use {SUPA}", SUPA),
    ("DigitalOcean", f"use {DO}", DO),
    ("Azure conn string", AZURE, AZURE_KEY),
    ("Postgres URL", f"DATABASE_URL=postgres://app:{PW}@db.example.com:5432/app", PW),
    ("Postgres URL password with @", "postgres://app:p@ss" + "w0rd!x@db.example.com/app", "p@ssw0rd!x"),
    ("Postgres URL password with /", "postgres://app:ab/cd" + "Secret9@db.example.com/app", "ab/cdSecret9"),
    ("Mongo SRV", f"mongodb+srv://admin:{PW}@cluster0.abcde.mongodb.net/test", PW),
    ("Redis URL no user", f"redis://:{PW}@cache:6379/0", PW),
    ("JDBC style", f"jdbc:postgresql://db/app?user=app&password={PW}", PW),
    ("libpq keyword string", f"host=db user=app password={PW} dbname=app", PW),
    ("PGPASSWORD env", f"PGPASSWORD={PW} psql -h db", PW),
    ("MYSQL -p", f"mysql -u root -p{PW} app", PW),
    (".env DB_PASS", f"DB_PASS={PW}", PW),
    (".env SECRET_KEY_BASE", f"SECRET_KEY_BASE={PW}", PW),
    (".env NEXTAUTH_SECRET", f"NEXTAUTH_SECRET={PW}", PW),
    (".env STRIPE_SECRET_KEY", f"STRIPE_SECRET_KEY={STRIPE_SK}", STRIPE_SK),
    (".env SESSION_KEY", f"SESSION_KEY={PW}", PW),
    (".env ENCRYPTION_KEY", f"ENCRYPTION_KEY={PW}", PW),
    (".env SIGNING_KEY", f"JWT_SIGNING_KEY={PW}", PW),
    (".env TWILIO_AUTH_TOKEN", "TWILIO_AUTH_TOKEN=0123456789abcdef" + "0123456789abcdef",
     "0123456789abcdef0123456789abcdef"),
    (".env quoted with space", f'DB_PASSWORD="{PW} more"', PW),
    (".env export", f"export API_TOKEN={PW}", PW),
    ("YAML", f"database:\n  password: {PW}\n", PW),
    ("YAML quoted", f"password: '{PW}'", PW),
    ("TOML", f'api_key = "{PW}"', PW),
    ("JSON nested", json.dumps({"db": {"password": PW}}), PW),
    ("JSON key 'pass'", json.dumps({"user": "a", "pass": PW}), PW),
    ("JSON key 'Authorization'", json.dumps({"headers": {"Authorization": "Token " + PW}}), PW),
    ("JSON key 'x-api-key'", json.dumps({"x-api-key": PW}), PW),
    ("JSON key 'apiKey' camel", json.dumps({"apiKey": PW}), PW),
    ("JSON key 'clientSecret' camel", json.dumps({"clientSecret": PW}), PW),
    ("JSON key 'accessToken' camel", json.dumps({"accessToken": PW}), PW),
    ("JSON key 'refresh_token'", json.dumps({"refresh_token": PW}), PW),
    ("JSON key 'privateKey' camel", json.dumps({"privateKey": PW}), PW),
    ("Python kwarg", f'client = OpenAI(api_key="{PW}")', PW),
    ("Python kwarg password", f'connect(password="{PW}")', PW),
    ("Shell flag --password", f"tool login --password {PW}", PW),
    ("Shell flag --password=", f"tool login --password={PW}", PW),
    ("Shell flag --token", f"gh auth login --with-token {GH}", GH),
    ("Shell flag --api-key value", f"cli --api-key {PW}", PW),
    ("Shell flag -p for docker login", f"docker login -u me -p {PW}", PW),
    ("Markdown table", f"| key | value |\n|---|---|\n| password | {PW} |", PW),
    ("Markdown bold colon", f"**API key**: {PW}", PW),
    ("Prose 'password is'", f"my password is {PW}", PW),
    ("Prose 'the token is'", f"the token is {PW}", PW),
    ("Private key RSA", "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7\n-----END RSA PRIVATE KEY-----",
     "MIIEpAIBAAKCAQEA7"),
    ("Private key OPENSSH", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n"
     "-----END OPENSSH PRIVATE KEY-----", "b3BlbnNzaC1rZXktdjE"),
    ("Private key truncated (no END)", "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n...",
     "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"),
    ("Private key escaped in JSON", json.dumps({"k": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0\n"
                                                     "-----END PRIVATE KEY-----"}), "MIIEvQIBADANBgkqhkiG9w0"),
    ("PGP private", "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0\n-----END PGP PRIVATE KEY BLOCK-----", "lQOYBF0"),
    ("Escaped JSON in tool echo", json.dumps(json.dumps({"api_key": PW})), PW),
    ("Double-escaped JSON", json.dumps(json.dumps(json.dumps({"password": PW}))), PW),
    ("Key separated by newline", f"password:\n{PW}", PW),
    ("Key with unicode quote", f"password: \u201c{PW}\u201d", PW),
    ("URL query token", f"https://api.example.com/v1?access_token={PW}&x=1", PW),
    ("URL query key", f"https://maps.example.com/api?key={PW}", PW),
    ("URL query sig (S3 presigned)", "https://bucket.s3.amazonaws.com/f?X-Amz-Credential=AKIA/x&X-Amz-Signature=" +
     "a" * 64, "a" * 64),
    ("Cookie header", f"Cookie: session={PW}", PW),
    ("Set-Cookie sessionid", f"sessionid={PW}; Path=/", PW),
    ("Credit card", "card 4242 4242 4242 4242 exp 12/30 cvc 123", "4242 4242 4242 4242"),
    ("SSN", "ssn 123-45-6789", "123-45-6789"),
]

# (text, secret) for formats beyond the review: annotations, other languages' syntax, commands, deeper escaping.
MORE_CASES = [
    ('SECRET_KEY: str = "django-insecure-abc123xyz"', "django-insecure"),
    ('os.environ["OPENAI_API_KEY"] = "Zx9-secret-value-1"', "Zx9-secret"),
    ("'password' => 'Zz12Yy34Xx'", "Zz12Yy34"),
    ('token := "Zz12Yy34Xx"', "Zz12Yy34"),
    ('$env:API_KEY = "Zz12Yy34Xx"', "Zz12Yy34"),
    ("<password>Hunter2Hunter2</password>", "Hunter2Hunter2"),
    ("openssl rsa -in k.pem -passout pass:Hunter2xyz", "Hunter2xyz"),
    ("sshpass -p Hunter2xyz ssh host", "Hunter2xyz"),
    ("mysql -u root -p'Tr0ub4dor&3xyzzy' app", "3xyzzy"),
    ("docker login -u me -p Tr0ub4dor&3xyzzy registry.example.com", "3xyzzy"),
    ("https://api.example.com/v1?access_token=Tr0ub4dor&3xyzzy&x=1", "3xyzzy"),
    ("https://acct.blob.core.windows.net/c?sv=2020&sig=AbC%2Bdef123xyz&se=2026", "AbC%2Bdef123xyz"),
    ("-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIFHDBOBgkqhkiG9w0BBQ0wQTApBgkqhkiG9w0BBQwwHAQI\n"
     "-----END ENCRYPTED PRIVATE KEY-----", "MIIFHDBOBg"),
    ("X-Auth-Token: 0123456789abcdef0123", "0123456789abcdef"),
    ("PRIVATE-TOKEN: Zz12Yy34Xx56Ww78Vv90", "Zz12Yy34"),
    ("API Key: AbC123xyz789", "AbC123xyz789"),
    ("Client secret: AbC123xyz789", "AbC123xyz789"),
    ("the api key is AbC123xyz789!", "AbC123xyz789"),
    ("POSTGRES_PASSWORD: postgres\n", ": postgres"),
    ("token:\nabc123xyz", "abc123xyz"),
    ("ANTHROPIC_KEY=Zz12Yy34Xx56", "Zz12Yy34"),
    ("discord https://discord.com/api/webhooks/123456/AbCdEf_GhIjKl-MnOpQr", "AbCdEf_GhIjKl"),
    ("gsk_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2", "a1B2c3D4e5F6"),
    ("ya29." + "a0AfH6SMBx1y2z3A4b5C6d7E8f9G0h", "a0AfH6SMBx"),
    ("card 4111-1111-1111-1111", "4111-1111-1111-1111"),
    ("social security number: 123-45-6789", "123-45-6789"),
]

# Ordinary developer text that must come back unchanged.
KEEP = [
    # assertions that already held before this change (tests/test_adapters.py)
    "author: Alice Example", "authentication: required", "token_count=123456", "tokens_used=99",
    "see /Users/x/api_key_rotation.md", "<automation_id>weekly-task-summary-and-cleanup-run</automation_id>",
    "are_you_sure_about_this_value_here_x",
    # code that names a secret without holding one
    "const token = getToken();", "password_hash = bcrypt(password, salt)", "if not secret:\n    raise ValueError('x')",
    "token = await getToken()", 'api_key = os.environ["OPENAI_API_KEY"]', "const apiKey = process.env.OPENAI_API_KEY;",
    "password = request.form['password']", "def login(user: str, password: str) -> bool:",
    "password: Optional[str] = None", "token: string;", "api_key: SecretStr", "self.token = token",
    "connect(user=user, password=password)", "tokenizer.pad_token = tokenizer.eos_token", "eos_token='</s>'",
    "if token is None:", "secret = secrets.token_hex(16)", "fetch(url, { credentials: 'include' })",
    'headers = {"Authorization": f"Bearer {token}"}', "TOKEN=$(cat ~/.config/token)",
    "token: ${{ secrets.GITHUB_TOKEN }}", 'password: "${DB_PASSWORD}"', "api_key=${API_KEY}", "token=$GITHUB_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS=/Users/me/keys/sa.json", "ssh_key: ~/.ssh/id_ed25519", "assert token == expected",
    "return {'token': token}", "if password != confirm:", "secrets: inherit", "max_tokens=4096", "token_type: Bearer",
    "secret: true", "password: str",
    # commands whose -p / -u flags are not passwords
    "docker login --password-stdin", "git push -u origin main", "docker run -u 1000:1000 image", "mkdir -p /tmp/x",
    "ssh -p 2222 host", "docker run -p 8080:80 nginx", "mysql -u root -p app",
    # prose
    "The secret to good tests is small, fast feedback.", "We hit the token limits again.",
    "The secret: keep the tests small.", "Password: (empty)", "The token: count went down.",
    "Use the --token flag to pass it.", "the password is incorrect", "the token is expired",
    "Bearer tokens are passed in the Authorization header as Bearer <token>", "Authorization: Basic",
    "Authorization: handled by middleware", "password: required field on the signup form",
    "credentials: see 1Password vault 'Prod'", "The access_key: field is deprecated in favour of role auth.",
    "| token | string | Your API token |", "| password | The user's password |",
    # paths, ids, data and URLs without credentials
    "pwd: /Users/me/project", "The tool printed pwd: /home/me", "cd /Users/me/secrets/project && ls",
    "session_id: 7f3a2b1c-0000-4000-8000-000000000001", "UUID 7f3a2b1c-0000-4000-8000-000000000001",
    "Commit 3f2a1b9c0d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a fixed it",
    "![chart](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJ"
    "RU5ErkJggg==)",
    "https://example.com/docs?page=2&sort=asc", "http://localhost:3000/@user/profile", "postgres://localhost:5432/app",
    "https://user@example.com/path", "git clone git@github.com:org/repo.git", "ssh://git@host.example.com:22/repo.git",
    "mailto:someone@example.com", "run sk-learn-compatible-estimator-wrapper tests",
    "Use the risk-assessment-framework-document-v2 template", "1729500000000 ms since epoch", "call 555-123-4567",
    "order 1234 5678 9012 3456", "call 123-45-6789", "version 1.2.3-beta.4", "timestamp 2026-10-21T10:00:00Z",
    "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqh\n-----END PUBLIC KEY-----",
]


def _leaks(text, secret):
    out = store.clean(text)
    return out if secret in out else None


def test_review_cases_are_redacted():
    leaked = [(name, _leaks(text, secret)) for name, text, secret in REVIEW_CASES if _leaks(text, secret)]
    assert not leaked, leaked
    # no partial leak either, such as the part of a password after an & or an @
    partial = [(name, store.clean(text)) for name, text, secret in REVIEW_CASES
               if secret[:6] in store.clean(text) or secret[-6:] in store.clean(text)]
    assert not partial, partial


def test_more_formats_are_redacted():
    leaked = [(text, _leaks(text, secret)) for text, secret in MORE_CASES if _leaks(text, secret)]
    assert not leaked, leaked


def test_ordinary_text_is_left_alone():
    changed = [(text, store.clean(text)) for text in KEEP if store.clean(text) != text]
    assert not changed, changed


def test_key_names_and_structure_stay_visible():
    expected = {
        "password=12345": "password=[REDACTED]",
        "**Password:** hunter2": "**Password:** [REDACTED]",
        "token=synthetic-token-123": "token=[REDACTED]",
        '{"password": "synthetic-123", "user": "alice"}': '{"password": "[REDACTED]", "user": "alice"}',
        "connect(password=abc123)": "connect(password=[REDACTED])",
        "DB_PASS=" + PW: "DB_PASS=[REDACTED]",
        "client_secret=abc123&redirect_uri=https://x/cb": "client_secret=[REDACTED]&redirect_uri=https://x/cb",
        f"postgres://app:{PW}@db.example.com:5432/app": "postgres://app:[REDACTED]@db.example.com:5432/app",
        f"curl -u admin:{PW} https://x": "curl -u admin:[REDACTED] https://x",
        f"OPENAI_API_KEY={OAI}": "OPENAI_API_KEY=[REDACTED]",
        f"post to {SLACK_WEBHOOK}": "post to https://hooks.slack.com/services/[REDACTED]",
        "| password | hunter2 |": "| password | [REDACTED] |",
        "password: changeme # default": "password: [REDACTED] # default",
    }
    for text, want in expected.items():
        assert store.clean(text) == want, (text, store.clean(text))


def test_escaped_json_at_every_depth():
    for depth in range(1, 5):
        text = {"password": 'p"re\\fix-' + PW, "author": "Alice", "note": "keep me"}
        for _ in range(depth):
            text = json.dumps(text)
        out = store.clean(text)
        assert "fix-" not in out and "3xyzzy" not in out, (depth, out)
        assert "Alice" in out and "keep me" in out, (depth, out)
    for value in ('a\\b"c', "ends-with\\"):  # an escaped backslash right before the closing quote
        out = store.clean(json.dumps(json.dumps({"password": value, "x": "keep"}))[1:-1])
        assert "keep" in out and "ends-with" not in out and 'b\\\\\\"c' not in out, out


def test_scrub_keeps_its_contract():
    assert store.scrub(None) == (None, False) and store.scrub("") == ("", False)
    exact = "x" * store.MAX_TEXT
    assert store.scrub(exact) == (exact, False)
    out, truncated = store.scrub(exact + "y")
    assert (len(out), truncated) == (store.MAX_TEXT, True)
    # redaction can shorten the text below MAX_TEXT, but truncated still reports the original length
    long_secret = "word " * ((store.MAX_TEXT - 1000) // 5) + "password=" + "Q1" * 1000
    out, truncated = store.scrub(long_secret)
    assert truncated and len(out) < store.MAX_TEXT and "Q1Q1" not in out, out[-80:]
    out, truncated = store.scrub("SSH_PRIVATE_KEY=" + "QUJD" * 10000)  # a long base64 secret is redacted whole
    assert (out, truncated) == ("SSH_PRIVATE_KEY=[REDACTED]", False), out[:80]
    # a secret that crosses the cut is recognised whole, so no prefix of it survives
    pad = "word " * ((store.MAX_TEXT - 12) // 5)
    out, truncated = store.scrub(pad + " " + OAI + " tail" * 10)
    assert truncated and len(out) <= store.MAX_TEXT and "sk-proj" not in out, out[-60:]


def test_adversarial_inputs_stay_fast():
    """Long hostile inputs must take linear time. The limits are generous for slow machines; on a laptop each
    input takes well under 0.1 s. A small copy runs first, so a quadratic regression fails in seconds, not hours."""
    units = {
        "hyphen run": "a-", "underscore run": "a_", "key= run": "key=", "password= run": "password=",
        "api_key: run": "api_key: ", "quotes": '"', "escaped quotes": '\\"', "nested json": '{"password": "',
        "nested escaped json": '{\\"token\\": \\"', "base64": "QUJD", "base64 with slashes": "QUJ/+D9z",
        "jwt-like": "eyJ" + "a" * 50 + ".", "sk- run": "sk-", "bearer run": "Bearer ",
        "pem headers": "-----BEGIN PRIVATE KEY-----", "url userinfo": "a://u:", "url at signs": "x://u:@a",
        "curl -u": "curl -u a:", "docker login": "docker login ", "mysql": "mysql ", "table": "| password ",
        "flags": "--password ", "authorization": "Authorization: ", "cookie": "Cookie: a=", "digits": "1234 ",
        "prose": "the quick brown fox jumps over the lazy dog ", "letters": "a", "spaces": " ",
        "lone values": "password:\n", "camel": "aB", "dots": "a.", "query": "?key=a&b", "xml": "<password>",
        "glued names": "passwordpassword", "segments": "a_b-c.d ", "env names": "A_B_C_D_E_F_G_H_I_J_K_L_",
    }
    texts = {name: unit * (200000 // len(unit) + 1) for name, unit in units.items()}
    texts["backslashes"] = "password: " + "\\" * 200000 + '"'
    texts["unclosed key"] = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 64 + "\n") * 3200
    slow = []
    for name, text in texts.items():
        assert len(text) >= 200000, name
        start = time.perf_counter()
        store.scrub(text[:20000])
        small = time.perf_counter() - start
        if small > 0.2:
            slow.append((name, "20k chars", round(small, 3)))
            continue
        start = time.perf_counter()
        store.scrub(text)
        took = time.perf_counter() - start
        if took > 1.0:
            slow.append((name, f"{len(text)} chars", round(took, 3)))
    assert not slow, slow


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
