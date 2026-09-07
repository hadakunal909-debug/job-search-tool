-- MIGRATION_wishlist.sql -- somewhere for a company we CANNOT scrape yet to land.
--
-- Both add-a-board paths dead-end today. The web page says "That isn't a readable job board, so
-- it can only be listed as apply-direct -- which needs a company name", and the extension's twin
-- falls through to "try the + Add board page". In both cases the user found an employer they want
-- followed, we could not read its board, and the intent is DISCARDED. Nobody learns which
-- employers people keep asking for, and the same site gets re-tried by hand every few weeks.
--
-- A wish is not a board. It is deliberately NOT in the `boards` table: custom_sources() filters on
-- `t in SCRAPERS` so a junk row there would be inert, but it would still show on /companies as
-- something we cover, and list_boards() would hand it to the careers-page coverage check. A wish
-- is a REQUEST, with a status, that a human reviews. Its own table says that plainly.
--
-- KEYED ON url, like every other table here, so db._upsert works unchanged (pk defaults to
-- "url") and a second person wishing for the same site MERGES rather than duplicating. `requests`
-- counts those repeats, which is the signal worth having: it ranks the backlog by how many people
-- actually wanted it, rather than by who happened to ask first.
--
-- NO FOREIGN KEY, and that is the point. `boards` and `jobs` are things we HAVE; a wish is a thing
-- we do not. There is nothing for it to reference.

create table if not exists public.wishlist (
  url          text primary key,
  company      text,
  -- WHY we could not take it, captured at the moment of failure rather than reconstructed
  -- later: "no readable board", "detected <ats> but read 0 postings", "tenant code only".
  -- This is what makes the list triageable instead of a pile of links.
  reason       text,
  source       text,                                   -- 'addboard' | 'extension'
  added_by     text,
  added_at     timestamptz not null default now(),
  requests     integer     not null default 1,
  -- open | adopted | rejected. Reviewed rows stay: a rejected wish is the record that stops
  -- the same site being investigated a third time, which is most of the value here.
  status       text        not null default 'open',
  note         text,
  reviewed_at  timestamptz
);

create index if not exists wishlist_open_idx
  on public.wishlist (status, requests desc, added_at desc);

-- db.py:262 marks this LAST -- without it PostgREST answers from its cached schema and every
-- column above reads as missing until it happens to reload.
notify pgrst, 'reload schema';
