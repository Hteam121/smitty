# Smitty on the go: the cloud hub, the iPhone app and the Watch

The phone never talks to the Mac. Both talk to a small Supabase project (the *hub*):

```
iPhone / Watch  ──►  Supabase hub  ◄──  Mac: smitty/hub.py (inside `smitty run`)
                     hub_state      latest snapshots + Mac heartbeat
                     notices        the inbox (screenshots live in the `artifacts` bucket)
                     chat_messages  what was heard and said
                     steps          what each session did
                     commands       what you asked for from the phone, queued until the Mac runs it
```

If the Mac is asleep or offline, commands wait as `queued` and the app shows
"Mac offline since …" from the heartbeat. Nothing is lost; it runs when Smitty is back.

## 1. Create the hub (once, ~10 minutes)

1. Create a Supabase project (free tier is plenty). Any region.
2. SQL editor → paste and run `supabase/migrations/0001_hub.sql`.
   (Or `supabase link` + `supabase db push` if you use the CLI.)
3. Authentication → Providers: enable **Email**. That is the only sign-in method (Sign in with
   Apple would need an entitlement and provider setup for no gain on a single-user hub).
4. Authentication → Users → *Add user*: your email + a password. This is the only account.
   Then Authentication → Settings → turn **off** "Allow new users to sign up". Do not skip this:
   row-level security gives every signed-in user full access, so the hub is only private while
   yours is the only account.
5. Project Settings → API Keys: copy the **Project URL**, the **publishable** key
   (`sb_publishable_…`, the old *anon* key; goes in the app) and the **secret** key
   (`sb_secret_…`, the old *service_role*; for the Mac only, never in the app).

## 2. Connect the Mac

Write `~/.smitty/hub.json` (readable only by you):

```bash
printf '{"url": "https://<project>.supabase.co", "key": "<secret key>"}' > ~/.smitty/hub.json && chmod 600 ~/.smitty/hub.json
```

(or export `SMITTY_HUB_URL` and `SMITTY_HUB_KEY`). Then:

```bash
uv run smitty hub --check
```

prints `hub OK` and the number of rows pushed. From then on `smitty run`, `serve` and `chat`
sync automatically every 3 s (`SMITTY_HUB_INTERVAL_S`); `smitty hub` runs the sync on its own.
Sync state (the cursor) is in `~/.smitty/hub.cursor.json`; delete it to re-push recent history.

What is mirrored: kv snapshots except the `live:` typing buffers, the last 100 chat lines then
everything new, the last 50 inbox items then everything new (and read marks either way), the
last 300 session steps then everything new (text capped at 2000 chars). `hub_prune()` runs daily
from the Mac (chat 60 days, steps 21, read notices 60, finished commands 14).

## 3. The apps

`apps/SmittyMobile/` — see its README. Sign-in screen takes the project URL and the publishable
key, then the email + password from step 4. The Watch receives the sign-in from the phone over
WatchConnectivity.

Approvals: an **Allow** tap on the phone or Watch counts as your "yes" for a destructive action,
the same as a spoken yes or a dashboard click. It is always one tap
per approval; nothing approves on a swipe, and "Allow all" still only covers that one session.

## 4. Later, with a paid Apple developer account

- Signing: your team id goes in `apps/SmittyMobile/Local.xcconfig`; TestFlight for installs on your own phone.
  (If Xcode reports "PLA Update available", accept the new agreement at
  developer.apple.com/account first.)
- Push (APNs): a Supabase database webhook on `notices`/`hub_state.approvals` → Edge Function →
  APNs, with actionable notifications (Allow / Deny) on the Watch. Not built yet.
- Live Activity for the running session and the Watch complication. Not built yet.

## 5. Screenshot reviews

A session that changes something visible can ask you to look before it finishes:

- Smitty's own workers have a `request_ui_review(target, question)` tool (their system prompt
  tells them to use it for UI changes). `target` is a URL of the running app, rendered at phone
  size by your own Chrome in headless mode (no Playwright install), or a PNG/JPG path such as a
  simulator screenshot.
- Any other Claude Code session can do the same from the shell:
  `uv run smitty review http://localhost:3000 "Does the new header look right?"` — it prints
  your reply. `uv run smitty shot <url>` just takes the screenshot.

The image lands in `~/.smitty/artifacts/<session key>/`, the inbox gets a **Review** item with
it (dashboard: **Looks good** / **Request changes**; phone and Watch: Approve / your words), the
hub uploads the file to the `artifacts` bucket, and your reply text is what the session receives.
No reply within `SMITTY_REVIEW_WAIT_S` (30 min) tells the session to keep its version and say so.

## 6. Not yet built

- Realtime subscriptions: the app polls every 3 s in the foreground; the tables are already in
  the `supabase_realtime` publication for when that changes.
