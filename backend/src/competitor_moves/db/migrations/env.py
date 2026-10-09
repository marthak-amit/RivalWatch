from alembic import context
from sqlalchemy import create_engine

from competitor_moves.config import get_settings

# Raw-SQL migrations (no ORM models), so there is no target metadata to autogenerate from.
url = get_settings().database_url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
