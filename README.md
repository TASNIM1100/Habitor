# Habitor

**Train your habits — together.**
Verify check-ins with a live photo, GPS, a QR or NFC checkpoint placed where the habit happens, or a focus
timer. Friends review photo proofs, nudge each other, compete in leagues and take on weekly challenges.

Python 3.11 · FastAPI · SQLModel · Jinja + HTMX · vanilla JS · installable PWA · no build step.

---

## Quick start

```bash
pip install -r requirements.txt
python main.py            # http://localhost:8000
python main.py --seed     # same, but first loads a lively demo world
```

First run creates `habitor.db` (SQLite, WAL mode), the `uploads/` folder and a secret key file
(`.habitor_secret`). Nothing else to configure.

### Demo logins (created by `--seed`)

| Username | Password    | What's going on |
|----------|-------------|-----------------|
| `alice`  | `arcade123` | Main demo player: LV5, 3 quests, taunted by bob 1h ago (clap back by checking in "Read 20 pages"), pending friend request + duel invite, active duel vs bob |
| `bob`    | `arcade123` | Has a **flagged** proof with a live re-prove countdown |
| `cara`   | `arcade123` | Has a pending photo proof you can vote on; did today's dare |
| `dan`    | `arcade123` | Missed a day → **tauntable**; wearing Caught Cappin' 🧢 |
| `eve`    | `arcade123` | Not alice's friend yet (sent her a request) |

The seed also creates the *Pixel Pushers* party (live boss battle, pet "Bitsy", stake pot "October Grind"),
captured map tiles around San Francisco, badges, notifications and a Wall of Fame/Shame.
`--seed` is idempotent; use `python main.py --seed-only` to seed without starting the server.
Because demo players share a public password, seeding only runs on a local SQLite database, with `DEBUG=1`,
or when you pass `--allow-demo` — never seed a production database.

### Tests

```bash
pytest tests.py -q
```

55 tests: every rule in `game.py` (points, levels, streaks, shields, votes, taunts, tiles, leagues, pots,
badges) plus HTTP flows (auth, CSRF, ownership, upload validation, idempotent scoring, GPS radius, QR,
voting → flag → rejection, 12h auto-verify, QR + NFC checkpoints, friend-code connect, themes, quick check-in sheet, scanner, security regressions
(open redirect, push SSRF, log redaction, Docker hygiene, demo-seed guard, timezone double check-ins), taunt limits, chill mode, clap-backs, leaderboards, media
privacy, security headers, and the Claude verification path with a stubbed client).

---

## Configuration

All settings are environment variables with safe defaults.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///habitor.db` | Any SQLAlchemy URL. `postgres://…` / `postgresql://…` are auto-routed to psycopg 3 |
| `SECRET_KEY` | auto-generated into `SECRET_KEY_FILE` | Signs sessions, CSRF and QR tokens. **Set it in production** (rotating it invalidates printed QR codes) |
| `SECRET_KEY_FILE` | `.habitor_secret` | Where the generated key is stored |
| `UPLOAD_DIR` | `uploads` | Proof photos (WebP, EXIF-stripped) |
| `BASE_URL` | `http://localhost:8000` | Used in invite links and QR codes |
| `DEBUG` | `0` | `1` = non-Secure cookies, `/docs`, verbose logs. Keep `0` in production |
| `ANTHROPIC_API_KEY` | — | Enables 🤖 AI photo verification and AI coach tips |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model for those calls |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | — | Enables Web Push (otherwise in-app notifications only) |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | For `python main.py` |
| `DISABLE_SCHEDULER` | `0` | Turn off background jobs (tests set this) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` (Docker) | Proxy IPs whose `X-Forwarded-For` is trusted. Set it to your load balancer's address; never `*` |

Generate VAPID keys once with the `vapid` CLI that ships with pywebpush's `py-vapid` dependency:
```bash
vapid --gen                      # writes private_key.pem + public_key.pem
vapid --applicationServerKey     # prints the value for VAPID_PUBLIC_KEY
```
Then set `VAPID_PUBLIC_KEY` to that printed key and `VAPID_PRIVATE_KEY` to the path of `private_key.pem`
(pywebpush also accepts the key's base64 DER string), plus `VAPID_SUBJECT=mailto:you@example.com`.

**Cookies & HTTPS.** With `DEBUG=0` session cookies are `Secure`. Browsers treat `http://localhost` as secure,
so local dev works. To test on a phone over your LAN, use HTTPS (camera and GPS require it anyway), e.g.
`cloudflared tunnel --url http://localhost:8000`, and set `BASE_URL` to the tunnel URL.

---

## How it's built

```
Habitor/
├── main.py           FastAPI app: config, models, auth, routes, orchestration, scheduler, seed
├── game.py           pure game rules (no I/O) — unit-tested
├── templates/
│   ├── base.html     app shell (sidebar, top bar, tab bar), icon set, dialogs, toasts
│   ├── pages.html    every page body as a macro
│   └── partials.html HTMX fragments (quest card, feed items, votes, taunt modal, HUD…)
├── static/
│   ├── style.css     the design system (tokens, components, responsive layout)
│   ├── app.js        camera, GPS, QR scanning, Web NFC read/write, timer, maps, recap export, push, PWA
│   ├── sw.js         service worker: offline shell + push
│   └── manifest.json PWA manifest
├── tests.py · requirements.txt · Dockerfile · README.md
```

* **Scoring is transactional and idempotent.** Every point change is a row in a ledger (`point_events`) with a
  unique `ref` (`checkin:42`, `verify:42`, `clapback:7`…), so nothing can double-count. Check-ins are unique per
  habit per slot at the database level; a double tap gets `409 QUEST ALREADY COMPLETE`.
* **Game rules** (`game.py`): `points = (10 + min(streak, 20) + 5 if verified) × difficulty` (×2 for Prove It Now),
  `XP = points`, `level = floor(sqrt(XP/50)) + 1`, rejected proof −15, clap-back +20 / taunter −5.
* **Scheduler** (APScheduler, every 1 and 5 min, all timezone-aware and re-runnable): reminders, Prove It Now
  triggers, missed-day detection (Chill Mode → shield → streak break), 12h vote resolution, re-prove deadlines,
  weekly boss spawn/fail, pet health, daily dares, duel endings, stake-pot settlement, monthly league rollover,
  badge expiry. With several workers, a DB lease (`job_locks`) elects one leader.
* **Security:** argon2 hashes, signed HttpOnly SameSite=Lax cookies (Secure outside DEBUG), CSRF token on every
  POST (form field or `X-CSRF-Token` header, which HTMX sends), session rotation on login, in-memory rate limits
  (login, sign-up, check-ins, taunts, votes, reactions, friend requests), ownership/visibility checks on every
  object (photos are served only to owner, friends and party-mates), upload validation (type sniffing, 5 MB,
  decompression-bomb guard, resize to 1280 px, EXIF stripped, re-encoded WebP), Jinja autoescaping, strict CSP
  (only the CDNs used, no inline scripts, no eval), `X-Frame-Options`, `nosniff`, HSTS on HTTPS.
* **Front-end libraries** load from CDNs: htmx, html5-qrcode, Leaflet (+ OpenStreetMap tiles), canvas-confetti,
  and qrcode-generator (to draw checkpoint and friend-code QR codes), plus Google Fonts (Barlow, Barlow Condensed).

### Themes and mobile features

Three matte themes: **Trail** (brown/off-white with mustard, default), **Blaze** (brick red + yellow) and
**Court** (white + charcoal with yellow). Switch them in *Settings ▸ Theme* (or the sidebar on desktop). The choice is
stored in a `habitor_theme` cookie, so pages render in the right theme with no flash, and it works signed out too.

Built for phones and an Android APK wrapper:
* Bottom tab bar with a centre **check-in** button that opens a quick bottom sheet (today's quests, Scan QR, Tap
  NFC, New quest, Add friend).
* **Scan** page (`/scan`, also an app shortcut) that understands every Habitor code: quest QR/NFC checkpoints and
  friend codes.
* Dialogs become **bottom sheets** on phones; the Android back button and a tap on the backdrop close them.
* Pull-to-refresh on Today, Feed and Leaderboards; ripple and press feedback; **haptic vibration** (toggle in
  Settings); the **screen stays awake** during focus sessions; an offline banner; a loading bar for every action;
  an install prompt card; swipe toasts away; 48px minimum touch targets; safe-area insets for notched phones.

### QR and NFC checkpoints

Every quest has two signed checkpoint links: `/qr/<token>` and `/nfc/<token>`. Put them where the habit happens:

* **QR:** quest ▸ *Edit* ▸ *Verification checkpoints* ▸ **Print checkpoint**, and stick it up at the gym, on the
  bookshelf or guitar case. Scan it from the quest card (*QR checkpoint* tab) or with any phone camera.
* **NFC:** on the same page, **Write to NFC tag** writes the link to a blank NTAG213/215 sticker (Chrome on
  Android). On other devices, copy the link into any NFC writer app. Tapping the tag opens Habitor on iPhone and
  Android; on Chrome for Android you can also tap it from the quest card (*NFC checkpoint* tab).

Both are instant-verified (+5 bonus), only work for the quest's owner, and can be re-issued with
*Issue new codes* if a tag or printout is compromised. They also count as re-verification for flagged proofs.

### Friend codes

*Friends* shows your **friend code** as a QR code. A friend scans it (in the app or with their camera), confirms, and
you're connected. You can also write the code to an NFC sticker so friends connect with a tap. The scanner and
NFC reader on the same page do the reverse.

### Rule details worth knowing

* **Proof status:** GPS, QR, NFC and AI-verified proofs are verified instantly (+5 bonus). A completed focus timer is
  also verified (server-timed, with "still here?" pings) but doesn't earn the +5. Photo-only proofs are pending
  until 2 friends say LEGIT (+5 bonus) or 12 hours pass without a Sus majority.
* **Flagging:** ≥3 Sus votes, or Sus > 50% of votes cast. The owner then has 12h to re-verify with live photo + GPS
  or the habit's QR/NFC checkpoint. Otherwise the check-in is rejected: its points are revoked, −15, the streak day is lost and
  "Caught Cappin' 🧢" shows for 7 days.
* **Streaks:** today is always "open". A missed scheduled day uses a Streak Shield if you have one (earn 1 per
  7-day streak, max 3); Chill Mode days neither break nor extend streaks (max 14 days/month).
* **Taunts** unlock when a friend missed/broke a streak in the last 48h, or it's after 20:00 their time and a quest
  is still undone. 3/day per sender, 1 per target per day, friends only, never during Chill Mode.
* **Leaderboards:** Friends / Party / Challenge / Global (opt-in) × Today / Week (resets Monday 00:00 in your
  timezone) / All-time × Points or Consistency % (completed ÷ scheduled). Arrows compare against the same board
  as of yesterday.
* **Leagues:** each party has a tier; monthly seasons rank parties in a tier by average points per member; top
  20% promote, bottom 20% relegate.
* **Coin stake pots** use virtual coins only. Finishers split the pot; if nobody finishes, stakes are refunded.

---

## Switching to PostgreSQL

```bash
pip install "psycopg[binary]>=3.2"
export DATABASE_URL=postgresql://user:pass@host:5432/habitor
python main.py
```
No code changes: tables and indexes are created on startup. (`postgres://` URLs from Heroku/Render work too.)

## Deploy

**Docker (any host)**
```bash
docker build -t habitor .
docker run -p 8000:8000 -v habitor-data:/data -e SECRET_KEY=change-me -e BASE_URL=https://your.domain habitor
```
The image runs as a non-root user with 2 uvicorn workers; SQLite and uploads live in the `/data` volume.
Only the app files are copied in (no local database, uploads or secret key). Behind a proxy, pass
`-e FORWARDED_ALLOW_IPS=<proxy ip>` so client IPs (and rate limits) can't be spoofed.
(If you bind-mount a host folder, make it writable by UID 10001.)

**Render** — New ▸ Web Service ▸ your repo ▸ *Docker* runtime. Add a Disk mounted at `/data` (for SQLite/uploads)
or a Render PostgreSQL and set `DATABASE_URL`. Set `SECRET_KEY` and `BASE_URL`. Health check path: `/health`.

**Railway** — New Project ▸ Deploy from repo (the Dockerfile is detected). Add a Volume at `/data`, or add the
PostgreSQL plugin and reference `${{Postgres.DATABASE_URL}}`. Set `SECRET_KEY`, `BASE_URL`.

**Fly.io**
```bash
fly launch --no-deploy            # detects the Dockerfile
fly volumes create habitor_data --size 1
# in fly.toml:  [mounts] source="habitor_data" destination="/data"   and   internal_port = 8000
fly secrets set SECRET_KEY=change-me BASE_URL=https://<app>.fly.dev
fly deploy
```

For uploads at scale, point `UPLOAD_DIR` at a mounted volume shared by all instances (or run one instance).

## Install on your phone (PWA)

Open the site over HTTPS, log in, then:
* **iPhone/iPad (Safari):** Share ▸ *Add to Home Screen*.
* **Android (Chrome):** ⋮ ▸ *Install app* (or the 📲 INSTALL button in Settings).

It launches full-screen with the bottom tab bar, works offline for the app shell, and receives push
notifications when VAPID keys are configured (iOS 16.4+ supports push for installed web apps).

## Optional: wrap for the App Store / Play Store with Capacitor

Habitor is a server-rendered app, so the simplest wrapper points a native shell at your deployed URL:

```bash
npm init -y && npm i @capacitor/core @capacitor/cli @capacitor/android @capacitor/ios
npx cap init Habitor com.example.habitor --web-dir www
mkdir www && echo '<meta http-equiv="refresh" content="0;url=https://your.domain/">' > www/index.html
```
In `capacitor.config.json` add `"server": { "url": "https://your.domain", "cleartext": false }`, then
`npx cap add android && npx cap add ios && npx cap open android` (or `ios`). Add camera and location usage
strings (`NSCameraUsageDescription`, `NSLocationWhenInUseUsageDescription`; Android `CAMERA` and
`ACCESS_FINE_LOCATION`) so `getUserMedia` and GPS work inside the WebView. Store review usually expects some native
value (push via `@capacitor/push-notifications`, haptics) beyond a plain website.

---

## Testing every feature by hand (≈5 minutes)

Run `python main.py --seed`, open http://localhost:8000 and sign in as **alice / arcade123**. Use a private window
for **bob** where noted. The camera, GPS and NFC need `localhost` or HTTPS; Web NFC needs Chrome on Android.

1. **Accounts & friends.** Create an account in a private window, then sign out and in again. On *Friends*, scan
   another player's friend code (or open `/invite/<code>`), search "da", and accept eve's request. In *Settings*,
   change your avatar colour and frame (higher frames are locked) and toggle **Chill Mode**.
2. **Quests.** *My quests* ▸ add the "Gym 3x/Week" plan, then create a custom quest with specific days, a reminder,
   a two-minute version and *stack after* Guitar. After 18:00 the two-minute hint appears on undone quests.
   Completing Guitar shows "Next in your stack: Read 20 pages".
3. **Verification.** Guitar ▸ *Focus timer* (a 1-minute demo) ▸ *Record verified check-in*. Read ▸ *Live photo* ▸
   *Open camera* ▸ *Take photo* ▸ *Submit* (this also claps back at bob: +20). Gym ▸ *Edit* ▸ *Verification
   checkpoints*: print the QR and write the NFC tag, then scan or tap them. You can also open the checkpoint link
   directly to see the confirm page. Set a GPS pin and use *Check in here*; anything over 150 m is refused.
4. **Activity & voting.** React to posts and vote *Legit* on cara's pending photo (2 legit votes = verified). As bob,
   *Needs re-verification* on Today shows the countdown. Voting on your own proof is refused.
5. **Taunts.** *Friends* or *Activity* ▸ *Taunt* dan ▸ pick a preset. As dan you see the taunt dialog, and the
   taunt shows on his profile. A second taunt to dan, or a fourth taunt in one day, is refused.
6. **Leaderboards.** Switch scope × period × ranking. Check the medals, the up/down movement, your highlighted row,
   the walls of fame and shame, and the league table.
7. **Game modes.** *Challenges*: accept dan's duel, follow alice vs bob, assign a dare on the duel finished vs cara,
   and create a stake challenge. *Parties ▸ Pixel Pushers*: pet, daily dare (live photo, +25 coins), invite link,
   league. *Boss battle*: HP, hits, top damage. *Territory*: captured tiles. *Profile*: badges. *Weekly recap*:
   swipe through, then *Download as image*. To trigger Prove It Now:
   `sqlite3 habitor.db "update users set prove_it_until=datetime('now','+2 minutes') where username='alice'"`
   and reload.
8. **Insights.** Heatmap, completion, best time of day, mood trend, longest streaks and the coach tip (written by
   AI when `ANTHROPIC_API_KEY` is set).
9. **Notifications.** The bell count in the top bar and the *Notifications* page. With VAPID keys set:
   *Settings* ▸ *Enable push notifications*.
10. **Look & feel.** Resize to phone width (bottom tab bar, no horizontal scroll, 44px+ touch targets), toggle
    sound effects (muted by default), and open `/nope` for the 404 page.
