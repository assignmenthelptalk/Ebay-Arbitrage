import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_db_path = os.getenv("DB_PATH", "")
DATABASE_URL = f"sqlite:///{_db_path}" if _db_path else "sqlite:///./arbitrage.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate(engine):
    """Safe column additions for SQLite — ALTER TABLE is idempotent via try/except."""
    migrations = [
        "ALTER TABLE orders ADD COLUMN sale_price REAL",
        "ALTER TABLE orders ADD COLUMN buyer_username VARCHAR",
        "ALTER TABLE orders ADD COLUMN line_item_id VARCHAR",
        # §4A.7 layer 1 — competitor-sourcing scan signals + promote linkage.
        "ALTER TABLE competitor_listings ADD COLUMN scan_id INTEGER",
        "ALTER TABLE competitor_listings ADD COLUMN watch_count INTEGER",
        "ALTER TABLE competitor_listings ADD COLUMN competing_sellers INTEGER",
        "ALTER TABLE competitor_listings ADD COLUMN price_min REAL",
        "ALTER TABLE competitor_listings ADD COLUMN price_median REAL",
        "ALTER TABLE competitor_listings ADD COLUMN price_spread REAL",
        "ALTER TABLE competitor_listings ADD COLUMN saturation_level VARCHAR",
        "ALTER TABLE competitor_listings ADD COLUMN demand_level VARCHAR",
        "ALTER TABLE competitor_listings ADD COLUMN demand_confidence VARCHAR",
        "ALTER TABLE competitor_listings ADD COLUMN velocity_signal VARCHAR",
        "ALTER TABLE competitor_listings ADD COLUMN selected BOOLEAN DEFAULT 0",
        "ALTER TABLE competitor_listings ADD COLUMN promoted BOOLEAN DEFAULT 0",
        "ALTER TABLE competitor_listings ADD COLUMN candidate_id INTEGER",
        "ALTER TABLE candidates ADD COLUMN awaiting_amazon_cost BOOLEAN DEFAULT 0",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # column already exists


def init_db():
    import models  # noqa: F401 — registers all ORM models with Base.metadata
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
