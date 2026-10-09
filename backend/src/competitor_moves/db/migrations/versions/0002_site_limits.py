"""Per-website scrape limits. NULL means "use the global default from Settings".

max_products: most products to collect from this site per run (what the user sees and edits).
page_budget:  hard cap on HTTP fetches per run for this site (safety net, normally left NULL).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.execute("""
        alter table sites
          add column max_products int check (max_products is null or max_products > 0),
          add column page_budget  int check (page_budget  is null or page_budget  > 0);
    """)


def downgrade() -> None:
    op.execute("alter table sites drop column if exists max_products, drop column if exists page_budget")
