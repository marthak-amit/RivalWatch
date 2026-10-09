"""Shared (public) tables. Per-site tables live in site_<id> schemas, see db/site_schema.sql.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None

SQL = """
create table users(
  id serial primary key,
  email text not null,
  password_hash text not null,
  role text not null default 'user' check (role in ('user','admin')),
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);
create unique index users_email_key on users (lower(email));

create table revoked_tokens(jti text primary key, expires_at timestamptz not null);

create table projects(
  id serial primary key,
  name text not null,
  url text not null,
  min_margin_pct numeric not null default 0,
  created_at timestamptz not null default now()
);

create table sites(
  id serial primary key,
  project_id int not null references projects on delete cascade,
  domain text not null,
  role text not null check (role in ('ours','competitor')),
  status text not null default 'active' check (status in ('active','blocked','removed')),
  platform text,
  schema_name text not null unique,
  cron text not null default '0 2 * * *',
  next_run_at timestamptz,
  last_run_at timestamptz,
  created_at timestamptz not null default now(),
  unique (project_id, domain)
);

create table magento_connections(
  project_id int primary key references projects on delete cascade,
  base_url text not null,
  store_code text not null default 'default',
  token_encrypted text not null,
  field_map jsonb not null default '{}',
  status text not null default 'new',
  last_sync_at timestamptz,
  updated_at timestamptz not null default now()
);

create table moves(
  id serial primary key,
  project_id int not null references projects on delete cascade,
  site_id int not null references sites on delete cascade,
  type text not null, category text,
  headline text not null, why_it_matters text,
  size jsonb not null default '{}',
  evidence jsonb not null default '[]',
  ai_observed boolean not null default false,
  window_from date, window_to date,
  created_at timestamptz not null default now()
);

create table insights(
  id serial primary key,
  project_id int not null references projects on delete cascade,
  kind text not null, title text not null, body text,
  evidence jsonb not null default '[]',
  created_at timestamptz not null default now()
);

create table jobs(
  id serial primary key,
  type text not null,
  payload jsonb not null default '{}',
  status text not null default 'queued' check (status in ('queued','running','done','failed')),
  run_after timestamptz not null default now(),
  attempts int not null default 0,
  error text,
  created_at timestamptz not null default now()
);
create unique index jobs_one_active on jobs (type, coalesce(payload->>'site_id', payload->>'project_id'))
  where status in ('queued','running');

create table price_changes(
  id serial primary key,
  project_id int not null references projects on delete cascade,
  sku text not null,
  old_price numeric, new_price numeric not null,
  user_id int references users on delete set null,
  applied_at timestamptz not null default now(),
  pushed boolean not null default false,
  push_error text
);
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    for t in ("price_changes", "jobs", "insights", "moves", "magento_connections", "sites",
              "projects", "revoked_tokens", "users"):
        op.execute(f"drop table if exists {t} cascade")
