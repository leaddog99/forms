"""set_master_password.py — set (or rotate) the Master/curator password.

Until 2026-07-29 the curator identity (user_id 0) was granted by the
X-Self-User-Id header alone, so anyone who could reach the app could send
`X-Self-User-Id: 0` and hold owner permissions — admin_ui, edit_master,
delete_master, manage_users, configure_system. On a public hostname that is a
complete admin bypass. uid 0 now additionally requires a token minted by
POST /auth/master against the password set here.

FAIL CLOSED: with no password configured, uid 0 resolves to nobody. So until you
run this, there is no master — which is the safe state, not a broken one.

Writes two keys to the repo-root .env (backed up to .env.bak first):

    BCC_MASTER_PASSWORD       scrypt hash — NOT the password itself
    BCC_MASTER_TOKEN_SECRET   random HMAC key for session tokens

.env is git-ignored and is where credentials belong (business config goes in
system_config instead — see memory/project_system_config.md).

Usage:
    python set_master_password.py              # prompts, hidden input
    python set_master_password.py --print      # show the lines, write nothing
    echo mypass | python set_master_password.py --stdin

RESTART THE SERVICE afterwards (the process reads .env at import):
    bcc_restart.bat        (self-elevates; a plain Restart-Service needs admin)
"""
from __future__ import annotations

import getpass
import importlib.util
import os
import secrets
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Load input/pipeline/auth.py BY PATH rather than `from input.pipeline import
# auth`. The package __init__ pulls in url_scoring -> dotenv, so the normal
# import needs the full venv — and this script must run on a bare system python.
# Setting the admin password is the first thing a fresh install does, possibly
# before any dependencies exist; auth.py itself is stdlib-only (dotenv is
# optional inside it), so importing the single file has no requirements at all.
_spec = importlib.util.spec_from_file_location(
    "_bcc_auth", os.path.join(_HERE, "input", "pipeline", "auth.py"))
auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auth)

ENV_PATH = os.path.join(_HERE, ".env")
KEYS = ("BCC_MASTER_PASSWORD", "BCC_MASTER_TOKEN_SECRET")


def _read_password() -> str:
    if "--stdin" in sys.argv:
        pw = sys.stdin.readline().rstrip("\n")
        if not pw:
            sys.exit("No password on stdin.")
        return pw
    if not sys.stdin.isatty():
        # Best-effort only. On Windows getpass reads the console directly via
        # msvcrt, so redirected stdin can neither feed it nor signal EOF — it
        # simply blocks with no console. And sys.stdin.isatty() proved
        # unreliable under Git Bash (reported True with `< /dev/null`, and
        # inconsistently across identical runs), so this catches some cases and
        # not others. The real rule: run it from a real terminal. That also
        # keeps the password out of any agent transcript.
        sys.exit(
            "No terminal attached, so the password can't be prompted for.\n"
            "Run this directly in PowerShell or cmd on the server:\n"
            "    python set_master_password.py\n"
            "Or pipe it (goes into shell history — rotate afterwards):\n"
            "    echo <password> | python set_master_password.py --stdin"
        )
    tries = 0
    while True:
        tries += 1
        if tries > 5:
            sys.exit("Too many attempts — nothing was written.")
        pw = getpass.getpass("New Master password: ")
        if len(pw) < 8:
            print("  Too short — use at least 8 characters. This is the only")
            print("  thing standing between the internet and edit_master.")
            continue
        if pw != getpass.getpass("Confirm: "):
            print("  Didn't match. Again.")
            continue
        return pw


def _upsert_env(lines: list[str], updates: dict[str, str]) -> list[str]:
    """Replace existing KEY= lines in place, append whatever is new. Preserves
    comments, ordering and unrelated keys — this file holds every API key we
    have and must not be rewritten wholesale."""
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(f"{k}={v}\n")
    return out


def main() -> None:
    password = _read_password()
    updates = {
        "BCC_MASTER_PASSWORD": auth.hash_password(password),
        "BCC_MASTER_TOKEN_SECRET": secrets.token_hex(32),
    }

    if "--print" in sys.argv:
        print("\nAdd these to .env yourself:\n")
        for k, v in updates.items():
            print(f"{k}={v}")
        return

    if os.path.exists(ENV_PATH):
        shutil.copy2(ENV_PATH, ENV_PATH + ".bak")
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        print(f"  backed up -> {ENV_PATH}.bak")
    else:
        lines = []

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(_upsert_env(lines, updates))

    # Prove the round trip rather than assuming it.
    os.environ["BCC_MASTER_PASSWORD"] = updates["BCC_MASTER_PASSWORD"]
    ok = auth.verify_master_password(password)
    print(f"  wrote BCC_MASTER_PASSWORD (scrypt) + BCC_MASTER_TOKEN_SECRET")
    print(f"  verify round-trip: {'OK' if ok else 'FAILED'}")
    if not ok:
        sys.exit("Verification failed — .env.bak still holds the previous file.")
    print("\nNow restart so the process picks it up:  bcc_restart.bat")
    print("Then log in as Master in the user picker; it will ask for this password.")


if __name__ == "__main__":
    main()
