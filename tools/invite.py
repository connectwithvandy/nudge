"""Mint or list invite codes.

    python3 tools/invite.py list
    python3 tools/invite.py new [uses]

Sign-ups are invite-only because one shared free AI tier serves everybody, so the number of
active accounts is what decides whether replies feel instant or rate-limited. Codes made here
live in the database, so they survive a redeploy — unlike the ones seeded from INVITE_CODES,
which are re-read from the environment on every boot.

Run this against the SAME database the app uses. On a hosted deployment that means pulling a
snapshot down first (tools/pull_snapshot.py) — codes minted against a local copy will not
exist on the server.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import accounts, config, db  # noqa: E402


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    db.connect().close()
    if cmd == "new":
        uses = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        code = accounts.create_invite(max_uses=uses)
        base = config.PUBLIC_URL or "http://localhost:8000"
        print(f"\n  code: {code}   ({uses} use{'s' if uses != 1 else ''})")
        print(f"  send them: {base}/signup  — and the code above\n")
        return
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT code, uses, max_uses, created_at FROM invites ORDER BY created_at").fetchall()
    if not rows:
        print("no invite codes yet — make one with:  python3 tools/invite.py new")
        return
    print(f"\n  {'code':<22} {'used':>10}  created")
    for code, used, mx, made in rows:
        spent = "  (spent)" if used >= mx else ""
        print(f"  {code:<22} {used:>4}/{mx:<5}  {made}{spent}")
    print(f"\n  {accounts.user_count()} of {config.MAX_USERS} accounts in use\n")


if __name__ == "__main__":
    main()
