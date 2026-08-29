"""Copy the live database down for inspection.

    python3 tools/pull_snapshot.py [output.db]

Read-only BY INTENT. The running app owns the real file, and anything written into a local
copy is overwritten by the next snapshot the server pushes — or worse, pushed over the live
snapshot if you point the app at it. Read it, don't edit it.

Needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in .env.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db  # noqa: E402


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "snapshot.db"
    if not config.TURSO_URL:
        print("No TURSO_DATABASE_URL set — there is no cloud snapshot to pull.")
        raise SystemExit(1)
    res = db._turso_pipeline([{"type": "execute",
                               "stmt": {"sql": "SELECT data, saved_at FROM db_backup WHERE id=1"}}])
    try:
        row = res[0]["response"]["result"]["rows"][0]
        blob, saved_at = row[0]["base64"], row[1].get("value", "?")
    except Exception:
        print("No snapshot found in the cloud yet.")
        raise SystemExit(1)
    data = base64.b64decode(blob + "=" * (-len(blob) % 4))
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data) // 1024}KB, snapshot taken {saved_at})")
    print("read-only by intent — the running app owns the real database")


if __name__ == "__main__":
    main()
