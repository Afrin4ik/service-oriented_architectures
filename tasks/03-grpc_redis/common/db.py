from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def database(url):
    engine = create_engine(
        url, pool_pre_ping=True, pool_size=10, max_overflow=20, connect_args={"connect_timeout": 3}
    )
    return engine, sessionmaker(engine, expire_on_commit=False)


def migrate(engine, service):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / service / "migrations")
    )
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(18032026)"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
