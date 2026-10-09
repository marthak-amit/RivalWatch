-- One schema per website: site_<id>. {schema} and {site_id} are filled in by db/site_schema.py.
-- Every table carries site_id, a foreign key to public.sites with a check that only this site's id is stored.
create schema if not exists {schema};

create table if not exists {schema}.crawl_runs(
  id serial primary key,
  site_id int not null default {site_id} references public.sites(id) on delete cascade check (site_id = {site_id}),
  run_type text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',
  pages_fetched int not null default 0,
  pages_failed int not null default 0,
  products_seen int not null default 0,
  est_total_products bigint,
  limit_hit boolean not null default false,
  full_coverage boolean not null default false,
  stop_reason text,
  changes jsonb not null default '{{}}',
  summary text,
  home jsonb,
  trace jsonb not null default '[]',
  prompt_version text,
  models jsonb,
  error text
);

create table if not exists {schema}.products(
  id serial primary key,
  site_id int not null default {site_id} references public.sites(id) on delete cascade check (site_id = {site_id}),
  url text not null unique,
  sku text, sku_norm text, gtin text,
  title text, brand text, category text, image text,
  price numeric, compare_at_price numeric, cost numeric, currency text, in_stock boolean,
  attributes jsonb not null default '{{}}',
  source_date timestamptz,
  extracted_by text,
  hash text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  last_checked_at timestamptz,
  missing_count int not null default 0,
  removed_at timestamptz
);
create index if not exists products_sku_norm on {schema}.products (sku_norm);
create index if not exists products_category on {schema}.products (category);

create table if not exists {schema}.price_history(
  id serial primary key,
  site_id int not null default {site_id} references public.sites(id) on delete cascade check (site_id = {site_id}),
  product_id int not null references {schema}.products on delete cascade,
  run_id int references {schema}.crawl_runs on delete set null,
  price numeric, compare_at_price numeric, in_stock boolean,
  hash text not null,
  seen_at timestamptz not null default now()
);
create index if not exists price_history_product on {schema}.price_history (product_id, id desc);

create table if not exists {schema}.pages(
  url text primary key,
  site_id int not null default {site_id} references public.sites(id) on delete cascade check (site_id = {site_id}),
  kind text not null default 'page',
  content_hash text not null,
  first_seen_at timestamptz not null default now(),
  last_fetched_at timestamptz not null default now()
);

-- Every change ever detected on this website (or made to it by a user), oldest to newest. Never rewritten.
create table if not exists {schema}.history(
  id serial primary key,
  site_id int not null default {site_id} references public.sites(id) on delete cascade check (site_id = {site_id}),
  run_id int references {schema}.crawl_runs on delete set null,       -- the crawl that detected it
  product_id int references {schema}.products on delete set null,     -- null for page/menu/promotion changes
  user_id int references public.users(id) on delete set null,         -- who made it, for changes made in the app
  type text not null,
  category text,
  before jsonb,
  after jsonb,
  occurred_at timestamptz not null,
  detected_at timestamptz not null default now()
);
create index if not exists history_occurred on {schema}.history (occurred_at desc);
create index if not exists history_product on {schema}.history (product_id, occurred_at desc);
create index if not exists history_type on {schema}.history (type, occurred_at desc);

-- Later template changes go below as idempotent ALTERs; `python -m competitor_moves.db.site_schema` applies them
-- to every site (the migrate service runs it after alembic).
-- 2026-10-09: where each value came from, as plain columns (not only inside the before/after JSON)
alter table {schema}.products add column if not exists path text;               -- URL path of the product page
alter table {schema}.products add column if not exists source_url text;         -- the page/feed the data was read from
alter table {schema}.history add column if not exists title text;               -- product or page title at the time
alter table {schema}.history add column if not exists url text;                 -- the product/page the change is about
alter table {schema}.history add column if not exists path text;
alter table {schema}.history add column if not exists source_url text;          -- the page/feed where it was seen
alter table {schema}.history add column if not exists currency text;
alter table {schema}.history add column if not exists price_before numeric;
alter table {schema}.history add column if not exists price_after numeric;
create index if not exists history_path on {schema}.history (path);
-- 2026-10-09: the product page's own SEO/social tags, exactly as published
alter table {schema}.products add column if not exists meta_title text;          -- <title>
alter table {schema}.products add column if not exists meta_description text;    -- <meta name="description">
alter table {schema}.products add column if not exists meta jsonb;               -- every og:/twitter:/product: tag
alter table {schema}.products add column if not exists meta_fetched_at timestamptz;
