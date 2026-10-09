"""Workspace ownership and site details needed by the crawl agent.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.execute("""
        alter table projects
          add column owner_user_id int references users on delete cascade,
          add column industry text not null default 'jewellery';
        create unique index projects_owner on projects (owner_user_id) where owner_user_id is not null;
        alter table sites
          add column name text,
          add column url text,
          add column last_error text;
        update sites set url = 'https://' || domain where url is null;
        alter table sites alter column url set not null;
        create index moves_project_window on moves (project_id, window_to desc);
    """)


def downgrade() -> None:
    op.execute("""
        drop index if exists moves_project_window;
        alter table sites drop column name, drop column url, drop column last_error;
        drop index if exists projects_owner;
        alter table projects drop column owner_user_id, drop column industry;
    """)
