from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "thresherr.db")

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)


# -------------------------------------------------
# SQLite WAL mode (concurrency safety)
# -------------------------------------------------
# The app (FastAPI, concurrent requests) and the worker (separate process)
# access the same SQLite database. In the default 'delete' journal mode,
# every write locks the whole database file and concurrent readers/writers
# can hit 'database is locked'. WAL allows readers to run concurrently with
# a single writer and is the recommended mode for this workload.
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """
    Dependency to provide a database session to FastAPI endpoints.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
