# Multi-user setup (one-time)

The app now supports **personal accounts**: each person logs in, keeps their **own
résumé**, gets their **own match scores**, and their **own** liked/hidden/applied jobs.
The scraped job pool is shared (everyone searches the same jobs). Accounts are
**admin-created** — there is no public sign-up.

## 1. Create the tables in Supabase (run once)

Supabase dashboard → your project → **SQL Editor** → **New query** → paste this → **Run**:

```sql
-- people who can log in (passwords are stored only as salted hashes)
create table if not exists users (
  username      text primary key,
  password_hash text not null,
  resume        text default '',
  created_at    timestamptz default now()
);

-- each person's own liked / hidden / applied jobs
create table if not exists user_jobs (
  username text not null,
  url      text not null,
  status   text,
  primary key (username, url)
);

-- store each job's description text so we can score it against ANY user's resume
alter table jobs add column if not exists jd text;
```

## 2. Create accounts (you, from your PC)

```powershell
python manage_users.py add kunal  <a-password>
python manage_users.py add friend <their-password>
python manage_users.py list
```

Give each person their username + password. They sign in at the app's URL.
Other commands: `passwd <user> <new>`, `remove <user>`, `resume <user> <file.txt>`.

## 3. Backfill job descriptions (so scoring works for everyone)

Run the scorer once — it now also saves each job's description text into the new `jd`
column, which is what lets the app score a job against each person's own résumé:

```powershell
python -m scraper.score_jobs
```

## 4. Each person sets their résumé

After logging in, everyone opens **📄 My résumé** in the sidebar, pastes their résumé,
and saves. Their match rings update to reflect *their* résumé.

---

**Note:** the old single `APP_PASSWORD` gate is replaced by username + password.
You can remove the `APP_PASSWORD` env var on Render once logins work (optional).
