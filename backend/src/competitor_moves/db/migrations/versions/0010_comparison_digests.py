"""Comparison settings per workspace and stored digests (AI analyst or rule-based).

compare_settings: {min_price, min_group_size, fx_rates: {"GBP": 1.27, ...}} (rates convert into our store's currency)

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.execute("""
        alter table projects add column compare_settings jsonb not null default '{}'
          check (jsonb_typeof(compare_settings) = 'object');
        create table digests(
          id serial primary key,
          project_id int not null references projects on delete cascade,
          created_at timestamptz not null default now(),
          change_count int not null default 0,
          summary text not null,
          actions jsonb not null default '[]',
          evidence jsonb not null default '{}',
          source text not null,
          inputs_hash text,
          prompt_version text,
          model text
        );
        create index digests_project on digests (project_id, created_at desc);
    """)


def downgrade() -> None:
    op.execute("drop table digests; alter table projects drop column compare_settings;")
