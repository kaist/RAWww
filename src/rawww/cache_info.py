from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from .cache import cache_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    db_path = cache_path(args.folder)
    if not db_path.exists():
        print(f"No cache exists for {args.folder}.")
        return
    with closing(sqlite3.connect(db_path)) as db:
        rows = db.execute(
            """
            SELECT variant, width, height, LENGTH(pixels), name
            FROM previews
            ORDER BY LENGTH(pixels) DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        total = db.execute("SELECT COALESCE(SUM(LENGTH(pixels)), 0) FROM previews").fetchone()[0]
        count = db.execute("SELECT COUNT(*) FROM previews").fetchone()[0]

    print(f"Rows: {count}")
    print(f"Blob total: {total / 1024 / 1024:.1f} MB")
    for variant, width, height, size, name in rows:
        print(f"{variant:5} {width:5}x{height:<5} {size / 1024 / 1024:6.2f} MB {name}")


if __name__ == "__main__":
    main()
