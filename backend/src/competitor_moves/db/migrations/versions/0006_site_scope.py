"""Per-competitor crawl scope: which menu categories to collect, and which products fill the product limit first.

categories: [] = the whole site, else [{"name": "Rings", "url": "https://shop/collections/rings"}, ...] picked from
            the site's own menu.
sort:       relevance (the site's own order) | newest | price_asc | price_desc | discount

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute("""
        alter table sites
          add column categories jsonb not null default '[]' check (jsonb_typeof(categories) = 'array'),
          add column sort text not null default 'relevance'
            check (sort in ('relevance','newest','price_asc','price_desc','discount'));
    """)


def downgrade() -> None:
    op.execute("alter table sites drop column categories, drop column sort")
