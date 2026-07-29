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
        "admin_ui", "edit_master", "delete_master", "promote_to_master",
        "refresh_dishes", "manage_users", "manage_dishes",
        "view_costs", "configure_system", "own_recipes",
    }),
    # Administrator: everything except billing-level (configure_system).
    "admin": frozenset({
        "admin_ui", "edit_master", "delete_master", "promote_to_master",
        "refresh_dishes", "manage_users", "manage_dishes",
        "view_costs", "own_recipes",
    }),
    # Editor: can publish + manage curator content, but not users or money.
    "editor": frozenset({
        "admin_ui", "edit_master", "delete_master", "promote_to_master",
        "refresh_dishes", "manage_dishes", "own_recipes",
    }),
    # Author: can curate but not delete master rows or trigger refreshes.
    "author": frozenset({
        "edit_master", "promote_to_master", "own_recipes",
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

def resolve_user(conn: sqlite3.Connection, self_user_id_header: Optional[str],
                 master_token: Optional[str] = None) -> Optional[dict]:
    """Look up the user identified by the X-Self-User-Id header. Returns
    the row dict or None if the header is missing/invalid or the user
    doesn't exist.

    Pre-Ghost: trusts the client header (set by users.html picker login,
    stored in localStorage). Post-Ghost: this function gets rewritten to
    validate the Ghost session JWT cookie; callers don't change."""
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
    return {
        "user_id": row[0],
        "ghost_uuid": row[1],
        "email": row[2],
        "name": row[3],
        "status": row[4],
        "subscription_tier": row[5],
        "role": row[6] or "member",
        "created_at": row[7],
        "updated_at": row[8],
    }
