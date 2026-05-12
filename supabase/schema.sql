create table if not exists listings (
  id          text        primary key,
  title       text,
  price       text,
  year        text,
  km          text,
  hand        text,
  location    text,
  url         text,
  source      text        not null default 'yad2',
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now()
);

create index if not exists listings_first_seen_at_idx on listings (first_seen_at desc);
