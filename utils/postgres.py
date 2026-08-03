import os
from sqlmodel import create_engine, Session
from config import DATABASE_URL

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSL_CERT_PATH = os.path.join(BASE_DIR, "ca-certificate.crt")

# Pool sized for the actual concurrency of this gateway: the conversion path
# is semaphore=1 and the droplet-local pollers add a handful of short-lived
# sessions — the SQLAlchemy defaults (pool_size=5, max_overflow=10, never
# recycled) pinned up to 15 psycopg2 connections whose libpq buffers grow on
# large reads and are then held for the life of the process (2026-07-28
# memory incident). Recycling every 30 min returns those buffers; pre_ping
# replaces the stale-connection errors recycling could otherwise surface.
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"sslrootcert": SSL_CERT_PATH},
    pool_size=int(os.getenv("DB_POOL_SIZE", "2")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "3")),
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")),
    pool_pre_ping=True,
)


def get_session():
    """Get database session."""
    with Session(engine) as session:
        yield session


def get_db():
    """Get database session (non-generator)."""
    return Session(engine)
