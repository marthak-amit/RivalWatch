"""Plans, crawl credits and per-user API keys (the UI's Account & API page), plus async crawl job fields.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute("""
        alter table users
          add column name text,
          add column plan text not null default 'starter' check (plan in ('starter','pro','business')),
          add column api_key text not null default ('rw_' || replace(gen_random_uuid()::text, '-', '')),
          add column credits int not null default 300 check (credits >= 0),
          add column last_login_at timestamptz;
        create unique index users_api_key on users (api_key);

        alter table jobs
          add column public_id text unique,
          add column user_id int references users on delete cascade,
          add column progress jsonb,
          add column result jsonb,
          add column started_at timestamptz,
          add column finished_at timestamptz;
    """)


def downgrade() -> None:
    op.execute("""
        alter table jobs drop column public_id, drop column user_id, drop column progress, drop column result,
          drop column started_at, drop column finished_at;
        drop index users_api_key;
        alter table users drop column name, drop column plan, drop column api_key, drop column credits,
          drop column last_login_at;
    """)
