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
client header — fine for a private-app threat model. When Ghost
integrates, the same function reads + verifies the Ghost session JWT
cookie instead; callers and gates don't change.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Iterable

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


# === User resolution =========================================================

def resolve_user(conn: sqlite3.Connection, self_user_id_header: Optional[str]) -> Optional[dict]:
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
    if uid <= 0:
        return None
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
