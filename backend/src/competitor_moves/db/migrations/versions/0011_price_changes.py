"""Price changes in Magento: requested (pending), then applied, with per-workspace guardrails.

projects: max_price_change_pct (one change may move the price at most this much), price_admin_only (only an admin
applies); min_margin_pct already exists (0 = never below cost).

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute("""
        alter table projects
          add column max_price_change_pct numeric not null default 50 check (max_price_change_pct > 0),
          add column price_admin_only boolean not null default false;
        alter table price_changes
          add column title text,
          add column product_url text,
          add column currency text,
          add column status text not null default 'pending' check (status in ('pending','applied','failed','cancelled')),
          add column created_at timestamptz not null default now(),
          add column applied_by int references users on delete set null,
          add column reverts int references price_changes on delete set null,
          alter column applied_at drop not null,
          alter column applied_at drop default,
          -- deleting a user nulls user_id and applied_by on rows the project cascade is deleting; checked at commit,
          -- when those rows are gone, the project key no longer fails
          alter constraint price_changes_project_id_fkey deferrable initially deferred;
        create index price_changes_project on price_changes (project_id, id desc);
        create unique index price_changes_one_pending on price_changes (project_id, sku) where status = 'pending';
    """)


def downgrade() -> None:
    op.execute("""
        drop index price_changes_one_pending; drop index price_changes_project;
        delete from price_changes where applied_at is null;
        alter table price_changes alter column applied_at set default now(), alter column applied_at set not null,
          drop column reverts, drop column applied_by, drop column created_at, drop column status, drop column currency,
          drop column product_url, drop column title,
          alter constraint price_changes_project_id_fkey not deferrable;
        alter table projects drop column price_admin_only, drop column max_price_change_pct;
    """)
