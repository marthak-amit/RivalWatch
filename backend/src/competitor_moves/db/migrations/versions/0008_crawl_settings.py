"""Per-competitor crawl settings (empty = the server defaults from Settings). Validated in services/workspaces.py.

Keys: delay_sec, time_limit_sec, browser ('off'|'auto'), browser_pages, product_meta, ai_extract, ai_extract_cap.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    op.execute("alter table sites add column crawl_settings jsonb not null default '{}' "
               "check (jsonb_typeof(crawl_settings) = 'object')")


def downgrade() -> None:
    op.execute("alter table sites drop column crawl_settings")
