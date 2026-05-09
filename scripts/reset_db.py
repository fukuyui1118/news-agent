"""Delete all rows from seen.db. Schema is preserved.

Usage:
    .venv/bin/python scripts/reset_db.py

Prints the row count before and after, so the operation is auditable.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "seen.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"{DB_PATH} does not exist — nothing to do.")
        return 0

    with sqlite3.connect(DB_PATH) as conn:
        before = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        conn.execute("DELETE FROM seen")
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    print(f"seen rows: {before} -> {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
