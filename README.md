# Nazzurath Discord Bot

Nazzurath is a Discord bot for the Tazzurath server and website. It keeps the
existing announcement, Avrae critical-roll, and read-only GitHub homebrew
features, and connects the website's DM scheduling system to Discord.

## What it does

- Checks `Adalbar3333/Tazzurath-Website` once per minute. A new website
  submission (a GitHub Issue with the `tazzurath-homebrew` label) is posted in
  Discord channel `1548461625865015466`.
- At 6:00 AM Central Time, posts every undecided proposal that members can
  currently vote on, including weighted approve/disapprove totals. It says
  nothing when the list is empty.
- At 12:00 PM Central Time, posts every Markdown page added under `content/` on
  the website's `main` branch since local midnight. It says nothing when no page
  was added.
- Uses `America/Chicago`, so the schedule follows CST/CDT daylight-saving
  changes automatically.
- If the process was briefly offline at the scheduled minute, it sends the
  missed report after reconnecting. Discord footer markers prevent duplicate
  daily reports after a restart.
- Keeps `/announce`, `/announce_quip`, automatic Avrae critical detection, and
  trusted-role reaction forwarding.
- Synchronizes current Discord members, display names, avatars, and roles to the
  website's PostgreSQL database every ten minutes. Members who leave are marked
  inactive instead of being deleted.
- Delivers durable scheduling invitations, session changes, cancellations, and
  24-hour/1-hour reminders by DM. Invitation and session messages have
  persistent response buttons that still work after a bot restart.
- Records Accept/Decline and Attending/Decline responses in the shared database
  and notifies the group's managers. A failed private message remains visible on
  the website and creates a manager-facing delivery warning.
- Exposes `/health` on `PORT` for host health checks.

The GitHub integration only needs read access to the website repository. The
scheduling integration uses the same Supabase PostgreSQL database as the
website; database credentials stay server-side.

## 1. Create/configure the Discord app

Use the [Discord Developer Portal](https://discord.com/developers/applications).

1. Create an application (or open the existing Nazzurath application).
2. On **Bot**, reset/copy the bot token. Treat this like a password.
3. On **Bot > Privileged Gateway Intents**, enable **Server Members Intent** and
   **Message Content Intent**. Message content is needed to inspect Avrae embeds.
4. On **Installation**, use a Discord-provided install link. For Guild Install,
   select the `bot` and `applications.commands` scopes.
5. Give the bot these permissions: View Channels, Send Messages, Embed Links,
   Read Message History, Add Reactions, and Use External Emoji.
6. Install it in the Tazzurath server. Make sure channel-specific overrides let
   it view and send in channel `1548461625865015466` and the configured critical
   forwarding channel.

Discord's official setup guide covers the same token, install-scope, and install
steps: <https://docs.discord.com/developers/quick-start/getting-started>.

## 2. Create the read-only GitHub token

The website repository is private, so create a
[fine-grained personal access token](https://github.com/settings/personal-access-tokens/new):

1. Set **Resource owner** to `Adalbar3333`.
2. Choose **Only select repositories** and select `Tazzurath-Website`.
3. Under repository permissions, grant **Contents: Read-only** and
   **Issues: Read-only**. Metadata read access is included automatically.
4. Generate the token and save it immediately.

Do not reuse the website's write-enabled GitHub token. The bot cannot edit the
site and does not need permission to do so.

## 3. Configure and run locally

Python 3.11 or later is recommended.

```bash
git clone https://github.com/Adalbar3333/NazzurathBot.git
cd NazzurathBot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and supply at least:

```dotenv
DISCORD_TOKEN=the_discord_bot_token
GITHUB_TOKEN=the_read_only_fine_grained_github_token
DATABASE_URL=the_supabase_pooled_postgresql_connection_string
DISCORD_GUILD_ID=the_tazzurath_server_id
DISCORD_DM_ROLE_ID=the_global_dm_role_id
```

The channel and repository values are already set to the requested defaults.
`HOMEBREW_SIGNING_SECRET` is optional; leave it empty for the simplest setup.
Advanced deployments can give it the website's `NEXTAUTH_SECRET` to verify the
site's signed metadata, but this is not required and does place that sensitive
website secret on a second server.

`DATABASE_URL` is optional at process level: when it is absent, scheduling is
disabled and every existing bot feature continues to run. To use scheduling,
first apply the migrations from the Tazzurath website repository, then use a
least-privilege runtime database credential. `WEBSITE_URL` controls links in
Discord messages. `SCHEDULING_POLL_SECONDS` defaults to 15 seconds.

Then verify and start it:

```bash
python -m unittest -v
python nazzurath.py
```

A successful start logs `Logged in as ...`. On its first run, the bot records
existing registry items without flooding the channel, then checks for new
submissions every minute. Set `ANNOUNCE_EXISTING_SUBMISSIONS=true` if you want
the first run to post all existing items. Global slash commands can take a
little time to appear after the first start.

Never commit `.env`; it is intentionally ignored by Git and Docker.

## 4. Easiest free hosting: Render

Use [Render](https://render.com/). The included `Dockerfile`, `render.yaml`, and
`/health` endpoint make the repository deployable as a free web service without
leaving your computer on.

First commit and push this finished project to the `main` branch of
`Adalbar3333/NazzurathBot`. Then:

1. Sign in to the [Render Dashboard](https://dashboard.render.com/) with GitHub.
2. Choose **New > Blueprint** and connect `Adalbar3333/NazzurathBot`.
3. Render will read `render.yaml`. Confirm the **Free** service.
4. Supply these secret values when prompted:
   - `DISCORD_TOKEN`: the token from the Discord Developer Portal.
   - `GITHUB_TOKEN`: the read-only fine-grained GitHub token from step 2.
   - `DATABASE_URL`: the pooled Supabase PostgreSQL connection string used by
     the scheduling website.
   - `DISCORD_GUILD_ID`: the Tazzurath server ID.
   - `DISCORD_DM_ROLE_ID`: the global Dungeon Master role ID.
5. Click **Apply**. The deploy is ready when the logs show `Logged in as ...`
   and the service's `/health` page shows `"discordReady": true`.

Render's free services can sleep after 15 minutes without incoming traffic. To
keep the Discord connection and exact-time reports awake, create a free monitor
at [UptimeRobot](https://uptimerobot.com/):

1. Copy the Render URL, such as
   `https://nazzurath-discord-bot.onrender.com/health`.
2. In UptimeRobot choose **Add New Monitor > HTTP(s)**.
3. Paste the `/health` URL and use the free 5-minute interval.

That interval is shorter than Render's 15-minute idle window. The bot does not
need a persistent disk. Homebrew restart deduplication uses hidden Discord
message markers, while scheduling delivery and button state are durable in
PostgreSQL.

Render's free tier is suitable for a hobby bot but does not provide a production
uptime guarantee and can restart services. The bot reconnects automatically and
sends a scheduled report after reconnecting if it missed the scheduled minute.

## Alternative: Oracle Cloud Always Free

If you prefer a VM you fully control, Oracle Cloud's Always Free compute is the
stronger but more technical option. Create an Always Free Ubuntu instance,
clone this repository, install `requirements.txt` in a virtual environment, and
run `nazzurath.py` as a `systemd` service. Oracle documents that idle Always Free
instances can be reclaimed, so it is not automatically more reliable for this
small, low-CPU bot.

## Troubleshooting

- **4014 / disallowed intents:** enable Server Members and Message Content on the
  Discord Developer Portal, then restart.
- **GitHub 404:** the fine-grained token does not have access to the private
  `Tazzurath-Website` repository, or owner/repository is misspelled.
- **Channel not found / 403:** invite the bot to the correct server and check the
  channel permissions and ID.
- **No new-page links:** verify `GITHUB_BRANCH=main`; only newly added
  `content/**/*.md` files count, not edits to existing pages.
- **No 6 AM message:** this is expected when every proposal is already approved
  or disapproved.
- **Scheduling is disabled:** set `DATABASE_URL` and `DISCORD_GUILD_ID`; the
  `/health` response reports configuration, database health, worker state, and
  last successful worker timestamps without exposing schedule data.
- **Member list is empty:** enable **Server Members Intent**, verify
  `DISCORD_GUILD_ID`, and restart the bot.
- **Scheduling DMs fail:** members must share the server and allow private
  messages from server members. The website alert is retained and group
  managers receive a delivery warning.
