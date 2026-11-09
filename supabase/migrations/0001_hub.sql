-- Smitty hub: the always-on middle between the Mac and the iPhone / Watch.
--
-- The Mac (smitty/hub.py) mirrors its SQLite activity store up here and drains the
-- `commands` queue; the phone only ever talks to this project. When the Mac is offline
-- commands wait as `queued` and the app shows the last known state.
--
-- Apply with the Supabase SQL editor or `supabase db push`. Afterwards, in Authentication:
-- enable the Email provider, add your one user (email + password), then turn OFF
-- "Allow new users to sign up" so only that account exists.
-- Row-level security below grants every signed-in user full access: this hub is single-user.

create extension if not exists pgcrypto;

-- Latest snapshots, same keys as the Mac's kv table:
--   state, status, sessions, approvals, chat, attention, brain, systems
-- plus `mac` = {"online": bool, "seen": iso, "pid", "mode", "version"} written by the sync agent.
create table if not exists hub_state (
  key     text primary key,
  value   jsonb not null,
  updated timestamptz not null default now()
);

-- The inbox. `id` is the Mac's notice id so re-syncs are idempotent.
create table if not exists notices (
  id      bigint primary key,
  ts      timestamptz not null,
  kind    text not null,
  title   text not null,
  body    text not null default '',
  session text,
  urgent  boolean not null default false,
  read    timestamptz,
  detail  jsonb not null default '{}'::jsonb,
  image   text                      -- path in the `artifacts` bucket when a screenshot is attached
);
create index if not exists notices_read on notices (read, id desc);

-- What was heard and said (typed or spoken). `id` is the Mac's event id.
create table if not exists chat_messages (
  id     bigint primary key,
  ts     timestamptz not null,
  role   text not null check (role in ('heard', 'said')),
  text   text not null,
  detail jsonb not null default '{}'::jsonb
);

-- What each session did, step by step (`session` is the worker key shown in `sessions`).
create table if not exists steps (
  id      bigint primary key,
  ts      timestamptz not null,
  session text not null,
  kind    text not null,
  text    text not null,
  detail  jsonb not null default '{}'::jsonb
);
create index if not exists steps_session on steps (session, id);

-- Things the phone/watch asked Smitty to do. The Mac picks them up in order.
--   chat          {text}                       talk to Smitty
--   approval      {id, answer: yes|no|all}     answer a pending approval (id from hub_state.approvals)
--   notice_reply  {id, text}                   reply to an inbox item (continues that session)
--   session       {key, action: stop|close|send, text?}
--   schedule      {id, action: remove|pause|resume|run}
--   mode          {mode: active|passive}
--   mark_read     {ids: [..]} or {}            mark inbox items read (all unread when no ids)
create table if not exists commands (
  id       uuid primary key default gen_random_uuid(),
  created  timestamptz not null default now(),
  device   text,                    -- iphone | watch
  kind     text not null check (kind in ('chat', 'approval', 'notice_reply', 'session', 'schedule', 'mode', 'mark_read')),
  body     jsonb not null default '{}'::jsonb,
  status   text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed')),
  result   jsonb,
  started  timestamptz,
  finished timestamptz
);
create index if not exists commands_queue on commands (status, created);

-- Screenshots and other files a session wants reviewed.
insert into storage.buckets (id, name, public) values ('artifacts', 'artifacts', false)
on conflict (id) do nothing;

-- ---------------------------------------------------------------- access
alter table hub_state     enable row level security;
alter table notices       enable row level security;
alter table chat_messages enable row level security;
alter table steps         enable row level security;
alter table commands      enable row level security;

do $$
declare t text;
begin
  foreach t in array array['hub_state', 'notices', 'chat_messages', 'steps', 'commands'] loop
    execute format('drop policy if exists "owner reads" on %I', t);
    execute format('drop policy if exists "owner writes" on %I', t);
    execute format('create policy "owner reads" on %I for select to authenticated using (true)', t);
    execute format('create policy "owner writes" on %I for all to authenticated using (true) with check (true)', t);
  end loop;
end $$;

drop policy if exists "owner reads artifacts" on storage.objects;
create policy "owner reads artifacts" on storage.objects
  for select to authenticated using (bucket_id = 'artifacts');

-- The Mac uses the service role key, which bypasses RLS.

-- ---------------------------------------------------------------- live updates
-- The app can subscribe instead of polling once Realtime is on for these tables.
do $$
begin
  alter publication supabase_realtime add table hub_state, notices, chat_messages, commands;
exception when duplicate_object or undefined_object then null;
end $$;

-- ---------------------------------------------------------------- housekeeping
-- Called by the Mac once a day (POST /rest/v1/rpc/hub_prune).
create or replace function hub_prune() returns void language sql security definer as $$
  delete from chat_messages where ts < now() - interval '60 days';
  delete from steps where ts < now() - interval '21 days';
  delete from notices where read is not null and ts < now() - interval '60 days';
  delete from commands where status in ('done', 'failed') and created < now() - interval '14 days';
$$;
