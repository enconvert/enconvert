import os
from sqlmodel import create_engine, Session
from config import DATABASE_URL

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSL_CERT_PATH = os.path.join(BASE_DIR, "ca-certificate.crt")

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"sslrootcert": SSL_CERT_PATH},
)


def get_session():
    """Get database session."""
    with Session(engine) as session:
        yield session


def get_db():
    """Get database session (non-generator)."""
    return Session(engine)
