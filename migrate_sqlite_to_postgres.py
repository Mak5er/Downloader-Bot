"""
One-off script to migrate existing SQLite data to a new Postgres database (e.g., Neon).

Usage:
1) Add your Postgres connection string to the environment as DATABASE_URL
   (example: postgres://user:password@host:port/dbname).
2) Ensure the Postgres driver is installed (psycopg2-binary is added to requirements.txt).
3) Run: python migrate_sqlite_to_postgres.py
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.db import Base, AnalyticsEvent, DownloadedFile, Settings, User

load_dotenv()

SQLITE_PATH = "services/maxload.db"
POSTGRES_DSN = os.getenv("DATABASE_URL")

if not POSTGRES_DSN:
    sys.exit("Please set DATABASE_URL in your environment before running the migration.")


def copy_table(src_session: Session, dst_session: Session, model) -> int:
    rows = src_session.query(model).all()
    for row in rows:
        data = {col.name: getattr(row, col.name) for col in model.__table__.columns}
        dst_session.merge(model(**data))  # merge handles existing PKs gracefully
    dst_session.commit()
    return len(rows)


def main():
    sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
    pg_engine = create_engine(POSTGRES_DSN)

    # Ensure target schema exists
    Base.metadata.create_all(pg_engine)

    with Session(sqlite_engine) as sqlite_sess, Session(pg_engine) as pg_sess:
        copied = {}
        copied["users"] = copy_table(sqlite_sess, pg_sess, User)
        copied["settings"] = copy_table(sqlite_sess, pg_sess, Settings)
        copied["downloaded_files"] = copy_table(sqlite_sess, pg_sess, DownloadedFile)
        copied["analytics_events"] = copy_table(sqlite_sess, pg_sess, AnalyticsEvent)

    print("Migration complete.")
    for table, count in copied.items():
        print(f"- {table}: {count} rows copied")


if __name__ == "__main__":
    main()
