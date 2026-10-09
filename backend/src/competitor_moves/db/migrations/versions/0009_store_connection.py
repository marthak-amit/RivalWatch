"""Our own store (Magento): the token is optional (public GraphQL needs none) and the connection keeps its status.

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.execute("""
        alter table magento_connections
          alter column token_encrypted drop not null,
          add column site_id int references sites on delete cascade,
          add column store_name text,
          add column currency text,
          add column last_error text;
        create unique index projects_one_store on sites (project_id) where role = 'ours';
    """)


def downgrade() -> None:
    op.execute("""
        drop index if exists projects_one_store;
        alter table magento_connections drop column site_id, drop column store_name, drop column currency,
          drop column last_error;
    """)
