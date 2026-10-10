from sqlalchemy import create_engine

import app.models  # noqa: F401
from alembic import context
from app.core.config import get_settings
from app.core.db import Base

target_metadata = Base.metadata


def run():
    url = context.config.attributes.get("url") or get_settings().database_url
    eng = create_engine(url)
    with eng.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run()
