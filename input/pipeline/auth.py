"""Role + permission helpers.

The schema (`users.role`) mirrors Ghost's staff role taxonomy 1:1 so
the eventual Ghost integration is a row-by-row promotion: each Ghost
staff member's role string drops straight into our column. Members
(non-staff) carry role='member' and get the same permission set as
Contributor — own-recipes only, no master write.

The permission map lives here (in code, not schema) so we can evolve
the rules without a migration. Add a new permission key, add it to the
roles that should have it, redeploy.

User identification today: the frontend sends X-Self-User-Id on each
request (read from the localStorage `app:self_user_id` set at picker
login). `resolve_user(request)` looks it up. Pre-Ghost this trusts the
client header for MEMBER identities — acceptable only because the
perimeter is closed (Cloudflare Access in front of the tunnel). When
Ghost integrates, the same function reads + verifies the Ghost session
JWT cookie instead; callers and gates don't change.

MASTER (user 0) IS NOT HEADER-GRANTABLE — 2026-07-29.
Until this date `X-Self-User-Id: 0` alone returned a synthetic 'owner'
with admin_ui/edit_master/delete_master/manage_users/configure_system.
Once the app went public on recipes.tbotb.com that meant anyone who set
one header was the curator. Now uid 0 additionally requires a valid
X-Master-Token, minted by POST /auth/master against a scrypt hash of the
password in env BCC_MASTER_PASSWORD.

FAIL CLOSED: if BCC_MASTER_PASSWORD is unset, uid 0 resolves to None and
no one is master. That is deliberate — an unconfigured install must not
have a guessable admin, and a missing secret must never mean "allow".
Set one with:  python set_master_password.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Optional, Iterable

# Self-sufficient secret loading: save_recipe_api.py calls load_dotenv() at
# import, but `python -m jobs` and the test suite may not, and a silently
# missing BCC_MASTER_PASSWORD reads as "no master configured". Best-effort so
# this module is correct wherever it's imported from.
try:                                              # pragma: no cover
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", ".env"))
except Exception:
    pass

# === Permission map ==========================================================
# Keys are role names matching the `users.role` column. Values are sets of
# permission strings. A user has a permission iff it's in their role's set.
# The "staff" set (anything but 'member') derives from `is_staff()` below.

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # Owner: one per site, billing + everything Admin can do.
    "owner": frozenset({
        "admin_ui", "edit_master", "delete_master",
        "refresh_dishes", "manage_users", "manage_dishes",
        "view_costs", "configure_system", "own_recipes",
    }),
    # Administrator: everything except billing-level (configure_system).
    "admin": frozenset({
        "admin_ui", "edit_master", "delete_master",
        "refresh_dishes", "manage_users", "manage_dishes",
        "view_costs", "own_recipes",
    }),
    # Editor: can publish + manage curator content, but not users or money.
    "editor": frozenset({
        "admin_ui", "edit_master", "delete_master",
        "refresh_dishes", "manage_dishes", "own_recipes",
    }),
    # Author: can curate but not delete master rows or trigger refreshes.
    "author": frozenset({
        "edit_master", "own_recipes",
    }),
    # Contributor + Member: own recipes only.
    "contributor": frozenset({"own_recipes"}),
    "member":      frozenset({"own_recipes"}),
}

# Convenience predicates for the most common checks.
STAFF_ROLES = frozenset({"owner", "admin", "editor", "author", "contributor"})
MASTER_WRITE_ROLES = frozenset(
    r for r, perms in ROLE_PERMISSIONS.items() if "edit_master" in perms
)


def can(user: Optional[dict], perm: str) -> bool:
    """True iff the user's role grants `perm`. Unknown user OR unknown
    role → False (deny-by-default)."""
    if not user:
        return False
    role = user.get("role") or "member"
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


def is_staff(user: Optional[dict]) -> bool:
    """Anyone whose role is NOT 'member'. The simplest gate for
    admin-console visibility."""
    if not user:
        return False
    return (user.get("role") or "member") in STAFF_ROLES


def permissions_for(role: str) -> list[str]:
    """Sorted list of permission strings for a role — used by
    /auth/me to surface what the caller can do."""
    return sorted(ROLE_PERMISSIONS.get(role, frozenset()))


# === Secrets: password hashing + master token ================================
# scrypt from the stdlib. Format: scrypt$<n>$<r>$<p>$<salt_hex>$<key_hex> so the
# cost parameters travel with the hash and can be raised later without breaking
# existing values.

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
MASTER_TOKEN_TTL = 12 * 3600          # a curator session, not a permanent grant


def hash_password(password: str) -> str:
    """scrypt hash of `password`, safe to store in .env / config."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                         n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time check of `password` against a hash from hash_password().
    A missing/malformed stored value is a FAILURE, never a pass."""
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                             n=int(n), r=int(r), p=int(p), dklen=len(key_hex) // 2)
    except Exception:
        return False
    return hmac.compare_digest(key.hex(), key_hex)


def _token_secret() -> Optional[bytes]:
    """HMAC key for master session tokens. Falls back to deriving from the
    password hash so a single configured secret is enough to run."""
    explicit = os.environ.get("BCC_MASTER_TOKEN_SECRET")
    if explicit:
        return explicit.encode("utf-8")
    pw = os.environ.get("BCC_MASTER_PASSWORD")
    return hashlib.sha256(("tok:" + pw).encode("utf-8")).digest() if pw else None


def master_password_configured() -> bool:
    return bool(os.environ.get("BCC_MASTER_PASSWORD"))


def verify_master_password(password: str) -> bool:
    return verify_password(password, os.environ.get("BCC_MASTER_PASSWORD"))


def mint_master_token(ttl: int = MASTER_TOKEN_TTL) -> Optional[str]:
    """`<expiry>.<hex sig>` — opaque to the client, verifiable without state."""
    secret = _token_secret()
    if not secret:
        return None
    exp = int(time.time()) + ttl
    sig = hmac.new(secret, f"master:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_master_token(token: Optional[str]) -> bool:
    """True only for an unexpired, correctly-signed token. Any parse problem,
    a missing secret, or an expired token is False — fail closed."""
    secret = _token_secret()
    if not secret or not token:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < time.time():
        return False
    want = hmac.new(secret, f"master:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


# === Per-user API keys (the bookmarklets) ====================================
# Each user gets their own key, generated off their users-table record, and it
# is what their bookmarklet carries. The key AUTHENTICATES IDENTITY — it does not
# grant anything by itself. What a given bookmarklet can do falls out of the
# permissions its owner holds: a member's key reaches /extract-from-url because
# that needs own_recipes, and bounces off /extract-review because that needs
# edit_master. One mechanism, no parallel scope system.
#
# CRUCIALLY, a key never unlocks staff. resolve_user() still requires the curator
# token for any non-member role, so a leaked admin bookmarklet is worth exactly
# member access. That matters because a key pasted into a browser bookmark is
# semi-public by nature — it sits in plaintext in the bookmarks bar and is sent
# from arbitrary publisher pages.
#
# Format: bcc_<user_id>_<43 urlsafe chars>. The id travels in the clear so lookup
# is one indexed row rather than a scan over every hash.
#
# Hashed with plain SHA-256, NOT scrypt. Deliberate: scrypt exists to make
# guessing a low-entropy human password expensive. This is 256 bits of CSPRNG
# output — unguessable by construction — and it is verified on every bookmarklet
# request, so a deliberately slow hash would only tax us.
API_KEY_PREFIX = "bcc"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def ensure_api_keys_table(conn: sqlite3.Connection) -> None:
    """One row per DEVICE, not per user.

    A single key per account meant generating one for your phone silently killed
    the one on your laptop — and the plaintext is shown once, so there was no way
    to re-display the old one. Phone + tablet + desktop is the normal case, so
    the key is a device credential and belongs in its own table.

    Migrates any existing users.api_key_hash across on first run, labelled so it
    is obvious where it came from. The old column is left in place rather than
    dropped: it is the only copy of that hash, and SQLite column drops are not
    worth the risk for a value we can simply stop reading."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_api_keys (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            key_hash     TEXT NOT NULL,
            label        TEXT NOT NULL DEFAULT 'Unnamed device',
            created_at   TEXT NOT NULL,
            last_used_at TEXT,
            last_seen_ua TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_api_keys_hash ON user_api_keys(key_hash)")
    conn.commit()

    have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "api_key_hash" in have:
        rows = conn.execute(
            "SELECT user_id, api_key_hash, api_key_created_at, api_key_last_used_at "
            "FROM users WHERE api_key_hash IS NOT NULL").fetchall()
        for uid, h, created, used in rows:
            if conn.execute("SELECT 1 FROM user_api_keys WHERE key_hash = ?", (h,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO user_api_keys (user_id, key_hash, label, created_at, last_used_at) "
                "VALUES (?,?,?,?,?)",
                (uid, h, "Original bookmarklet",
                 created or _utc_now(), used))
        conn.commit()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_api_key(conn: sqlite3.Connection, user_id: int, label: str) -> str:
    """Mint a key for one device. Returns the plaintext ONCE — only the hash is
    stored, so a lost key is replaced, never recovered. Other devices are
    untouched, which is the entire point of this table."""
    ensure_api_keys_table(conn)
    plain, hashed = generate_api_key(user_id)
    conn.execute(
        "INSERT INTO user_api_keys (user_id, key_hash, label, created_at) VALUES (?,?,?,?)",
        (user_id, hashed, (label or "").strip()[:60] or "Unnamed device", _utc_now()))
    conn.commit()
    return plain


def list_api_keys(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Metadata only — never a hash, never anything that could be replayed."""
    ensure_api_keys_table(conn)
    return [
        {"id": r[0], "label": r[1], "created_at": r[2],
         "last_used_at": r[3], "last_seen_ua": r[4]}
        for r in conn.execute(
            "SELECT id, label, created_at, last_used_at, last_seen_ua "
            "FROM user_api_keys WHERE user_id = ? ORDER BY id", (user_id,))
    ]


def revoke_api_key(conn: sqlite3.Connection, user_id: int, key_id: int) -> bool:
    """Delete one device's key. Scoped by user_id so a key id from another
    account cannot be revoked by guessing the number."""
    ensure_api_keys_table(conn)
    cur = conn.execute("DELETE FROM user_api_keys WHERE id = ? AND user_id = ?",
                       (key_id, user_id))
    conn.commit()
    return bool(cur.rowcount)


def generate_api_key(user_id: int) -> tuple[str, str]:
    """Return (plaintext, hash). The plaintext is shown ONCE and never stored."""
    plain = f"{API_KEY_PREFIX}_{user_id}_{secrets.token_urlsafe(32)}"
    return plain, hash_api_key(plain)


def parse_api_key(key: Optional[str]) -> Optional[int]:
    """user_id embedded in a well-formed key, else None. Shape check only —
    says nothing about whether the key is real."""
    if not key or not key.startswith(API_KEY_PREFIX + "_"):
        return None
    parts = key.split("_", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    try:
        uid = int(parts[1])
    except ValueError:
        return None
    return uid if uid > 0 else None      # uid 0 has no key; master needs the password


def resolve_api_key(conn: sqlite3.Connection, key: Optional[str],
                    user_agent: Optional[str] = None) -> Optional[int]:
    """Verify `key` against this user's DEVICE keys. Returns the user_id or None.

    The uid travels in the key's plaintext, so this reads the handful of rows for
    that one account rather than scanning every hash. Stamps last_used_at and the
    user-agent so the owner can tell their devices apart in the list — which is
    the only recognition signal a browser honestly offers."""
    uid = parse_api_key(key)
    if uid is None:
        return None
    ensure_api_keys_table(conn)
    want = hash_api_key(key)
    rows = conn.execute(
        "SELECT id, key_hash FROM user_api_keys WHERE user_id = ?", (uid,)).fetchall()
    match = next((r[0] for r in rows if hmac.compare_digest(r[1], want)), None)
    if match is None:
        return None
    try:
        conn.execute(
            "UPDATE user_api_keys SET last_used_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
            "last_seen_ua = COALESCE(?, last_seen_ua) WHERE id = ?",
            ((user_agent or "")[:200] or None, match))
        conn.commit()
    except Exception:
        pass                              # never fail a request over telemetry
    return uid


# === Per-user passwords ======================================================
# Same primitives as the master password, with the user_id bound INTO the
# signature so a token minted for one account cannot be replayed as another.
#
# Rollout without a flag day: a password being set is what enforces it. An
# account with a password_hash must present a valid token and the
# X-Self-User-Id header alone is refused; an account without one still resolves
# by header (logged, loudly). Set passwords one at a time and each account
# hardens as you go — no global switch to flip and forget.

USER_TOKEN_TTL = 30 * 24 * 3600      # a customer session, not an admin one


def mint_user_token(user_id: int, ttl: int = USER_TOKEN_TTL) -> Optional[str]:
    """`<uid>.<expiry>.<hex sig>` — stateless, verifiable, and bound to the uid."""
    secret = _token_secret()
    if not secret:
        return None
    exp = int(time.time()) + ttl
    sig = hmac.new(secret, f"u{user_id}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{user_id}.{exp}.{sig}"


def verify_user_token(token: Optional[str], user_id: int) -> bool:
    """True only for an unexpired token signed for THIS user_id."""
    secret = _token_secret()
    if not secret or not token:
        return False
    try:
        uid_s, exp_s, sig = token.split(".", 2)
        uid, exp = int(uid_s), int(exp_s)
    except Exception:
        return False
    if uid != int(user_id) or exp < time.time():
        return False
    want = hmac.new(secret, f"u{uid}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


# === Purpose tokens (email links) ============================================
# A token that travels by EMAIL is not a session token, and the distinction is
# load-bearing. `mint_user_token` returns a 30-day credential: mail one and
# anyone who reads the mailbox, a forwarded copy, a proxy log or a Referer
# header holds a logged-in account. So these are signed over a different
# payload and cannot be presented as a session.
#
# The payload binds THREE things:
#   purpose  — a verify token cannot be replayed as a reset token
#   user_id  — inside the signature, so it cannot be replayed as another account
#   bound    — a value the endpoint re-reads from the DB at redemption time
#
# `bound` is what makes these single-purpose in practice. For verification it is
# the email address: a token minted for the old address stops verifying once the
# address changes, because the signature no longer reproduces. For a reset it
# would be the current password hash, which makes the link die the moment the
# password is used or changed — no revocation table needed.

VERIFY_TOKEN_TTL = 24 * 3600           # long enough to find the mail tomorrow
RESET_TOKEN_TTL = 2 * 3600             # short: it changes a credential


def mint_purpose_token(purpose: str, user_id: int, bound: str,
                       ttl: int) -> Optional[str]:
    """`<uid>.<expiry>.<hex sig>` for a one-shot emailed link."""
    secret = _token_secret()
    if not secret:
        return None
    exp = int(time.time()) + ttl
    msg = f"{purpose}:{user_id}:{bound}:{exp}".encode("utf-8")
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{user_id}.{exp}.{sig}"


def read_purpose_token(token: Optional[str]) -> Optional[tuple[int, int]]:
    """(user_id, expiry) if the SHAPE parses and it hasn't expired — signature
    NOT checked, because verifying it needs `bound`, which the caller must look
    up by user_id first. Never trust this alone."""
    if not token:
        return None
    try:
        uid_s, exp_s, _sig = token.split(".", 2)
        uid, exp = int(uid_s), int(exp_s)
    except Exception:
        return None
    if exp < time.time():
        return None
    return uid, exp


def verify_purpose_token(token: Optional[str], purpose: str, user_id: int,
                         bound: str) -> bool:
    """True only for an unexpired token signed for exactly this purpose, user
    and bound value. Any parse problem, missing secret, or mismatch is False."""
    secret = _token_secret()
    parsed = read_purpose_token(token)
    if not secret or not parsed:
        return False
    uid, exp = parsed
    if uid != int(user_id):
        return False
    try:
        sig = token.split(".", 2)[2]
    except Exception:
        return False
    msg = f"{purpose}:{uid}:{bound}:{exp}".encode("utf-8")
    want = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


def ensure_email_verification_columns(conn: sqlite3.Connection) -> None:
    """`email_verified` / `email_verified_at`. Auto-migrated like the rest.

    Deliberately NOT enforced anywhere yet: existing accounts predate mail
    entirely, and a flag day that locks six real users out of an app on a public
    hostname is a worse outcome than an unproven address. Verification is
    recorded first; anything that depends on it comes later, one surface at a
    time."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "email_verified" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
    if "email_verified_at" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
    conn.commit()


def mark_email_verified(conn: sqlite3.Connection, user_id: int) -> bool:
    ensure_email_verification_columns(conn)
    cur = conn.execute(
        "UPDATE users SET email_verified = 1, email_verified_at = ? WHERE user_id = ?",
        (_utc_now(), user_id))
    conn.commit()
    return cur.rowcount > 0


def ensure_password_column(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col in ("password_hash", "password_set_at"):
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    conn.commit()


def set_user_password(conn: sqlite3.Connection, user_id: int, password: str) -> bool:
    ensure_password_column(conn)
    cur = conn.execute(
        "UPDATE users SET password_hash = ?, password_set_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE user_id = ?", (hash_password(password), user_id))
    conn.commit()
    return bool(cur.rowcount)


def user_password_hash(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
    except sqlite3.OperationalError:      # column not migrated yet
        return None
    return row[0] if row else None


MASTER_USER: dict = {
    "user_id": 0,
    "ghost_uuid": None,
    "email": None,
    "name": "Master (curator)",
    "status": "system",
    "subscription_tier": None,
    "role": "owner",
    "created_at": None,
    "updated_at": None,
}


# === User resolution =========================================================

def ensure_api_key_columns(conn: sqlite3.Connection) -> None:
    """Add the api-key columns if this DB predates them. Same auto-migrate shape
    the domains/enrich columns use — no separate migration step to forget."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, decl in (("api_key_hash", "TEXT"),
                      ("api_key_created_at", "TEXT"),
                      ("api_key_last_used_at", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
    conn.commit()


def resolve_user(conn: sqlite3.Connection, self_user_id_header: Optional[str],
                 master_token: Optional[str] = None,
                 api_key: Optional[str] = None,
                 session_token: Optional[str] = None) -> Optional[dict]:
    """Look up the user identified by the X-Self-User-Id header. Returns
    the row dict or None if the header is missing/invalid or the user
    doesn't exist.

    Pre-Ghost: trusts the client header (set by users.html picker login,
    stored in localStorage). Post-Ghost: this function gets rewritten to
    validate the Ghost session JWT cookie; callers don't change."""
    # A valid API key is REAL authentication and outranks the self-id header,
    # which is only a claim. This is the path the bookmarklets take: they run on
    # a publisher's page with no session, so they carry the key instead.
    if api_key:
        key_uid = resolve_api_key(conn, api_key)
        if key_uid is not None:
            self_user_id_header = str(key_uid)
        else:
            return None                   # a key was offered and it was wrong

    if not self_user_id_header:
        return None
    try:
        uid = int(self_user_id_header)
    except (ValueError, TypeError):
        return None
    if uid < 0:
        return None
    if uid == 0:
        # user_id 0 is the curator/master identity — the dispatch target for
        # master_recipes. It is NOT a row in the users table (the PK is
        # AUTOINCREMENT from 1 and bootstrap skips 0); it resolves to a
        # synthetic 'owner' granting edit_master + admin_ui.
        #
        # THE HEADER ALONE IS NOT ENOUGH (2026-07-29). Before this, sending
        # X-Self-User-Id: 0 was all it took to become owner — on a public
        # hostname that is a full admin bypass. A valid X-Master-Token from
        # POST /auth/master is now required as well. No password configured
        # => no master, ever.
        return MASTER_USER if verify_master_token(master_token) else None
    row = conn.execute(
        "SELECT user_id, ghost_uuid, email, name, status, "
        "subscription_tier, role, created_at, updated_at "
        "FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()
    if not row:
        return None

    # If this account has a password, the header alone is NOT enough — a valid
    # session token for this exact uid is required. An API key already proved
    # identity cryptographically, so it satisfies this too.
    #
    # Accounts with no password still resolve by header, which is how the
    # rollout avoids a flag day: each account hardens the moment a password is
    # set. The warning is deliberately noisy — an un-passworded account is a
    # spoofable one and should not go unnoticed.
    if not api_key and user_password_hash(conn, uid):
        if not verify_user_token(session_token, uid):
            return None
    elif not api_key:
        print(f"[AUTH] user {uid} resolved by UNVERIFIED header "
              f"(no password set on this account)")

    # STAFF PERMISSIONS REQUIRE THE CURATOR PASSWORD — not just uid 0.
    # Gating only uid 0 left an identical bypass one number away: user 5 carries
    # role='owner' in the users table, so `X-Self-User-Id: 5` alone granted all
    # ten permissions, configure_system included. Any staff role now needs the
    # same X-Master-Token.
    #
    # IDENTITY IS PRESERVED, ONLY THE ROLE IS LOCKED. A staff member without a
    # token stays themselves — own recipes, own history — and simply reads as
    # 'member'. Admins are customers too; browsing your own recipes shouldn't
    # require unlocking admin. `staff_locked` lets the UI offer an unlock prompt
    # instead of silently hiding the admin nav.
    role = row[6] or "member"
    staff_locked = False
    if role != "member" and not verify_master_token(master_token):
        staff_locked = True
        role = "member"

    return {
        "user_id": row[0],
        "ghost_uuid": row[1],
        "email": row[2],
        "name": row[3],
        "status": row[4],
        "subscription_tier": row[5],
        "role": role,
        "actual_role": row[6] or "member",
        "staff_locked": staff_locked,
        "created_at": row[7],
        "updated_at": row[8],
    }
