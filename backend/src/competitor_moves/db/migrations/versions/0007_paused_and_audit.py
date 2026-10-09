"""Competitors can be paused by their owner (UI enable/disable switch); admin actions are audited.

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.execute("""
        alter table sites drop constraint sites_status_check;
        alter table sites add constraint sites_status_check check (status in ('active','paused','blocked','removed'));
        create table audit_log(
          id serial primary key,
          ts timestamptz not null default now(),
          actor_id int references users on delete set null,
          actor_email text not null,
          action text not null,
          target_id int references users on delete set null,
          target_email text,
          detail text not null default ''
        );
        create index audit_log_ts on audit_log (ts desc);
    """)


def downgrade() -> None:
    op.execute("""
        drop table audit_log;
        update sites set status='active' where status='paused';
        alter table sites drop constraint sites_status_check;
        alter table sites add constraint sites_status_check check (status in ('active','blocked','removed'));
    """)
