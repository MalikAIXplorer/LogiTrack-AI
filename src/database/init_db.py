"""Initialize the LogiTrack SQLite schema."""

import argparse
import sqlite3
from pathlib import Path

from src.config import DEFAULT_DATABASE_PATH
from src.database.schema import SCHEMA_SQL


def initialize_database(db_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    initialize_database(args.db_path)
    print(f"Initialized LogiTrack schema at {args.db_path}")


if __name__ == "__main__":
    main()
