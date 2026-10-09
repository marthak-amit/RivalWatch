"""Login sessions: one row per login, replacing revoked_tokens.

A token is valid only while its session row is open (ended_at is null), not past expires_at and not idle
for longer than the role's idle timeout. Session length = last_seen_at - started_at.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("""
        create table sessions(
          jti text primary key,
          user_id int not null references users on delete cascade,
          role text not null check (role in ('user','admin')),
          started_at timestamptz not null default now(),
          last_seen_at timestamptz not null default now(),
          expires_at timestamptz not null,
          ended_at timestamptz,
          end_reason text check (end_reason in ('logout','expired','idle','disabled')),
          ip text,
          user_agent text
        );
        create index sessions_user on sessions (user_id, started_at desc);
        drop table revoked_tokens;
    """)


def downgrade() -> None:
    op.execute("""
        create table revoked_tokens(jti text primary key, expires_at timestamptz not null);
        drop table sessions;
    """)
