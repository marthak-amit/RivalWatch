create schema if not exists {schema};

create table if not exists {schema}.products(
  id serial primary key,
  url text not null unique,
  sku text, sku_norm text, gtin text,
  title text, brand text, category text, image text,
  price numeric, compare_at_price numeric, cost numeric, currency text, in_stock boolean,
  source_date timestamptz,
  extracted_by text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);
create index if not exists products_sku_norm on {schema}.products (sku_norm);

create table if not exists {schema}.price_history(
  id serial primary key,
  product_id int not null references {schema}.products on delete cascade,
  price numeric, compare_at_price numeric, in_stock boolean,
  hash text not null,
  seen_at timestamptz not null default now()
);
create index if not exists price_history_product on {schema}.price_history (product_id, id desc);

create table if not exists {schema}.pages(
  url text primary key,
  content_hash text not null,
  last_fetched_at timestamptz not null default now()
);

create table if not exists {schema}.crawl_runs(
  id serial primary key,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',
  pages_fetched int not null default 0,
  pages_failed int not null default 0,
  est_total_products bigint,
  changes jsonb not null default '{{}}',
  summary text,
  trace jsonb not null default '[]',
  error text
);

create table if not exists {schema}.events(
  id serial primary key,
  run_id int references {schema}.crawl_runs on delete set null,
  product_id int references {schema}.products on delete set null,
  type text not null, category text,
  before jsonb, after jsonb,
  occurred_at timestamptz not null,
  detected_at timestamptz not null default now()
);
