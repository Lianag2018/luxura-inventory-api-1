import os
from typing import Generator

from sqlalchemy import create_engine
from sqlmodel import Session


def mask_db_url(url: str) -> str:
    if not url or "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, tail = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing in environment variables")

print("[DB] Using DATABASE_URL =", mask_db_url(DATABASE_URL))

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=3,
    max_overflow=2,
)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
