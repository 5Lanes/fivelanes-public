# Fivelanes

Fivelanes pulls email, Meet recording notes, text threads, and calendar events into a private SQLite database (`timeline.db`), resolves conversation threads across connected OAuth accounts, and runs segmentation and summaries for the dashboard.

Licensed under the [MIT License](LICENSE).

## Code vs data

This repository ships **application code only**. Your private runtime data lives in a separate directory (conventionally `fivelanes-data/` next to the clone or anywhere on disk). Point the app at it with `FIVELANES_DATA_ROOT` in a gitignored bootstrap `.env` in the repo root.

Typical data-directory layout:

```
/path/to/your/fivelanes-data/
  .env                 # SOURCE_ACCOUNT, API keys, etc.
  prompts.json         # LLM prompts (from services/prompts.example.json)
  timeline.db          # SQLite database (created on first run)
  credentials/
    credentials.json   # Google OAuth desktop client
    tokens.json        # OAuth tokens (created by utils/add_account.py)
    calendar_scheduling_rules.json
  conversations/       # iMessage/SMS JSON exports
  slack-dms/           # Slack DM JSON (premium)
  linkedin-messages/   # LinkedIn data export CSV (premium)
  meet-recordings/     # Meet notes catalog + imported summary tabs
  out/                 # calendar availability JSON
  logs/
```

Paths resolve through [`utils/runtime_paths.py`](utils/runtime_paths.py). Override individual paths in your data `.env` (`FIVELANES_PROMPTS_PATH`, `TEXTS_CONVERSATIONS_DIR`, `DATABASE_NAME`, etc.).

## Features

The dashboard is split into a base open-source layer and optional premium capabilities (see [`utils/features.py`](utils/features.py)).

| Tier | Capabilities |
|------|----------------|
| **Base** | Threads, dashboard, meetings, plans, lanes, meeting prep, email-reply drafting, pipeline |
| **Premium** | Text threads (iMessage/SMS exports), Slack DMs, LinkedIn DMs, Meet recording notes (Google Docs), calendar availability export and open-slots UI |

Premium features are disabled in the public repo unless unlocked (e.g. via a premium add-on or `FIVELANES_PREMIUM=1` for local development). The README documents the full product; runtime gating lives in code.

## Input sources

Fivelanes accepts data through seven channels. The scheduled pipeline (`fivelanes.main` / dashboard scheduler) pulls email and calendar automatically; Meet recording notes (premium), text, Slack, and LinkedIn threads are cataloged and must be selected in the dashboard before they appear on Threads.

| Channel | How data arrives | Where it lands | Processing |
|---------|------------------|----------------|------------|
| **Email** | Mail to `SOURCE_ACCOUNT` (forward, Cc/Bcc, or direct To) | Gmail API → `thread_tracking`, `timeline_entries` | Segmentation + LLM summary (same as inbox pipeline) |
| **Meet recordings** | Pull Doc names/dates from Drive, then select which to import | `thread_tracking` (`meet:` prefix), `timeline_entries` (`type=meeting`, `source_id` `docs:…`) | Conversation-summary tab only (not the full transcript tab); summary on track / pipeline; **premium** |
| **Text** | JSON files in your data directory's `conversations/` (iMessage export shape) | `thread_tracking` (`text:` prefix) when tracked | Summary only (no email-style segmentation); **premium** |
| **Slack** | Pull DMs via `SLACK_USER_TOKEN`, stored as JSON under `slack-dms/` | `thread_tracking` (`slack:` prefix) when tracked | Summary only; **premium** |
| **LinkedIn** | CSV from LinkedIn data export under `linkedin-messages/messages.csv` | `thread_tracking` (`linkedin:` prefix) when tracked | Summary only; **premium** |
| **Calendar** | Google Calendar OAuth (connected accounts) | `$DATA_ROOT/out/availability_calendar_latest.json`, `meetings` table | Availability export, open-slots UI, scheduling context in summaries; **premium** |
| **Dashboard** | HTTP POST to `/api/*` (tracking, plans, lanes, pipeline run, drafts, meeting prep, email reply) | `$DATA_ROOT/timeline.db` | User actions and on-demand LLM calls |

### Email

The primary input is a dedicated Fivelanes inbox (`SOURCE_ACCOUNT`). Connected Gmail OAuth accounts (`$FIVELANES_DATA_ROOT/credentials/credentials.json`, `tokens.json`) supply both the inbox pull and thread resolution across mailboxes.

Mail is pulled with `(to:inbox OR cc:inbox OR bcc:inbox)` plus recipient checks. Four delivery routes are handled in [`services/email/inbox_process.py`](services/email/inbox_process.py) — see [Inbox delivery scenarios](#inbox-delivery-scenarios) below.

Image-only captures (direct To with screenshots) run OCR (Tesseract) first, then a vision model fallback.

Run manually:

```bash
python -c "from services.email import populate_timeline; populate_timeline(lookback_days=14)"
```

### Text threads

**Premium.** On-disk JSON under your data directory's `conversations/` folder (override with `TEXTS_CONVERSATIONS_DIR`) holds iMessage/SMS exports — one file per thread, filename stem = conversation key (e.g. `+15551234567.json`). Each file is a list of message objects with fields like `text`, `date`, `handle`, `is_from_me`, and `guid`.

Fivelanes does not sync texts from a phone automatically. Export conversations externally, drop the JSON files into `$FIVELANES_DATA_ROOT/conversations/`, then open **Texts setup** (`/texts-setup`) to choose which threads to track. Tracked threads are registered in `thread_tracking` with `inbox_thread_id` `text:<key>` and merged into the Threads view. Summaries are generated via `/api/texts/summarize` or as part of `fivelanes.main`.

### Slack DMs

**Premium.** Set `SLACK_USER_TOKEN` in your data `.env`, then open **Slack setup** (`/slack-setup`) to pull DMs into `$FIVELANES_DATA_ROOT/slack-dms/` and choose which conversations to track. Tracked threads use `inbox_thread_id` `slack:<conversation_id>` and appear on Threads. Once a conversation is tracked, the background scheduler also pulls fresh DMs and re-summarizes it on every scheduled inbox pull; you can still trigger it manually via `/api/slack/pull` + `/api/slack/summarize` or `fivelanes.main`.

### LinkedIn DMs

**Premium.** Export your LinkedIn messages (data export format) and place `messages.csv` under `$FIVELANES_DATA_ROOT/linkedin-messages/` (override with `LINKEDIN_MESSAGES_DIR`). Open **LinkedIn setup** (`/linkedin-setup`) to choose which threads to track. Tracked threads use `inbox_thread_id` `linkedin:<conversation_key>` and appear on Threads. Summaries run via `/api/linkedin/summarize` or `fivelanes.main`.

### Meet recording notes

**Premium.** Google Meet / Gemini notes Docs are handled like Slack: open **Meet notes** (`/meet-recordings-setup`), pull Doc **names and dates** from Drive into `$FIVELANES_DATA_ROOT/meet-recordings/index.json`, then choose which recordings to import. Import fetches only the **conversation-summary** tab (never the full transcript tab) and registers `thread_tracking` rows with `inbox_thread_id` `meet:<drive_file_id>`. Summaries run via **Generate summaries** or `fivelanes.main`.

Uses the same OAuth tokens (`drive.readonly`, `documents.readonly`). Enable **Drive** and **Docs** APIs in the Cloud project (a “Found 0 docs” pull with no error usually means Drive API is disabled). Re-run `python utils/add_account.py …` after upgrading so tokens include the new scopes.

```bash
python -c "from services.meet_recordings import pull_meet_recording_catalog; print(pull_meet_recording_catalog())"
```

### Calendar and availability

**Premium.** Google Calendar is read through the same OAuth tokens. After each pipeline run (unless `CALENDAR_AVAILABILITY_DISABLE=1`), events are exported to `$FIVELANES_DATA_ROOT/out/availability_calendar_latest.json` and synced into the `meetings` table. The Threads page shows open slots from that export; thread summaries can use your calendar as scheduling context. Optional scheduling rules in `$FIVELANES_DATA_ROOT/credentials/calendar_scheduling_rules.json` filter which calendars count and set buffers/timezone.

Run manually:

```bash
python scripts/pull_calendar_availability.py
```

### Dashboard and scheduler

`dashboard_server.py` serves the UI and JSON API. Besides Meet notes, text, Slack, and LinkedIn thread selection (all premium), the dashboard accepts snooze/remove on threads, lane and plan edits, meeting-prep and email-reply prompts (user intent → LLM), and manual pipeline runs (`POST /api/pipeline/run`).

The background scheduler (`utils/run_fivelanes_scheduler.py`, also started with the dashboard) runs the full cycle every `FIVELANES_INTERVAL_SEC` (default 15 minutes) during the active window (default 06:00–19:00 local; quiet hours 19:00–06:00 via `FIVELANES_QUIET_START_HOUR` / `FIVELANES_QUIET_END_HOUR` in `FIVELANES_SCHEDULER_TZ`).

**Security note:** The dashboard has no authentication and exposes `/timeline.db`. It is intended for trusted LAN use only. See [SECURITY.md](SECURITY.md).

## Setup

1. **Bootstrap the repo** — copy [`.env.example`](.env.example) to `.env` in the repo root and set `FIVELANES_DATA_ROOT` to your private data directory (any path; e.g. `~/fivelanes-data` or `./fivelanes-data` beside the clone).

2. **Create your data directory:**

   ```bash
   mkdir -p "$FIVELANES_DATA_ROOT"/{credentials,conversations,out,logs}
   ```

3. **Configure the data directory** — copy [`data.env.example`](data.env.example) to `$FIVELANES_DATA_ROOT/.env` and set:
   - `SOURCE_ACCOUNT` — Fivelanes inbox address (e.g. `you+fivelanes@example.com`)
   - `SOURCE_OAUTH_ACCOUNT_ID` — label for the Gmail OAuth account that owns that inbox
   - `OWNER_NAME` — your display name (used in summary assembly and UI heuristics)

4. **Prompts** — copy [`services/prompts.example.json`](services/prompts.example.json) to `$FIVELANES_DATA_ROOT/prompts.json`. Optional: set `FIVELANES_PROMPTS_PATH` if you use a different location.

5. **Google OAuth** (Gmail, Calendar, Meet recording Docs):
   - Copy [`credentials/credentials.example.json`](credentials/credentials.example.json) to `$FIVELANES_DATA_ROOT/credentials/credentials.json`
   - In Google Cloud Console (same project as that OAuth client), enable:
     - [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
     - [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
     - [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) (required for Meet notes catalog)
     - [Google Docs API](https://console.cloud.google.com/apis/library/docs.googleapis.com) (required to import summary tabs)
   - From the repo root: `python utils/add_account.py you@example.com` to create `$FIVELANES_DATA_ROOT/credentials/tokens.json`
   - Optional: copy [`credentials/calendar_scheduling_rules.example.json`](credentials/calendar_scheduling_rules.example.json) to `$FIVELANES_DATA_ROOT/credentials/calendar_scheduling_rules.json`

6. **Install dependencies:**
   - Python: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-linux.txt` (system package `tesseract-ocr` for image OCR)
   - Frontend: `cd frontend && npm install && npm run build`

7. **Optional:** `FIVELANES_BACKEND` (`llama` or `claude`), Ollama host/models, or `CLAUDE_API_KEY` — see [`data.env.example`](data.env.example).

## Running

From the project root (with your virtualenv active):

```bash
# Pull inbox mail → thread_tracking + timeline_entries
python -c "from services.email import populate_timeline; populate_timeline(lookback_days=14)"

# Segment and summarize timeline messages
python -c "from fivelanes import run_llm_pipeline; run_llm_pipeline(lookback_days=14)"
```

Or use [`fivelanes.py`](fivelanes.py): `run_email_pipeline()` and `run_llm_pipeline()`.

The dashboard (`dashboard_server.py`) serves the thread UI when `DASHBOARD_HOST` / `DASHBOARD_PORT` are set.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Inbox delivery scenarios

Mail to/cc/bcc `SOURCE_ACCOUNT` is routed in [`services/email/inbox_process.py`](services/email/inbox_process.py) (`InboxRoute`). Gmail fetch only lives in [`services/email/inbox_pull.py`](services/email/inbox_pull.py); LLM body prep lives in [`services/email/inbox_delivery.py`](services/email/inbox_delivery.py).

### Code layout

| Module | Role |
|--------|------|
| [`services/email/inbox_process.py`](services/email/inbox_process.py) | Route, todo plans, tracking rows, thread expansion |
| [`services/email/inbox_pull.py`](services/email/inbox_pull.py) | Gmail fetch for to/cc/bcc inbox |
| [`services/email/thread_resolve.py`](services/email/thread_resolve.py) | Gmail expansion primitives; `populate_timeline` delegates to `inbox_process` |
| [`services/email/inbox_delivery.py`](services/email/inbox_delivery.py) | `timeline_row_process_body` (pre-LLM) |
| [`services/email/forwarding.py`](services/email/forwarding.py) | Forward unwrap, inner RFC `Message-ID` |
| [`services/email/gmail_message.py`](services/email/gmail_message.py) | Build timeline rows from Gmail API responses |
| [`services/email/subject.py`](services/email/subject.py) | Subject-prefix and `todo` helpers |
| [`services/email/recipients.py`](services/email/recipients.py) | To/Cc/Bcc parsing |

| How you send it | What Fivelanes does |
|-----------------|---------------------|
| **To** `SOURCE_ACCOUNT` with subject `todo:` | Creates a standalone **Plan** (synthetic `todo:` id, not linked to a tracked thread). Marks the inbox Gmail thread removed; deleting the plan does not affect other threads. |
| **Forward** to `SOURCE_ACCOUNT` | Tracks via inner RFC `Message-ID`, resolves the real thread in connected mailboxes, **drops** the forward-to-inbox shell from the timeline. |
| **Cc/Bcc** `SOURCE_ACCOUNT` (inbox not in To) | Tracks via envelope RFC id, resolves the source mailbox thread for timeline content; inbox Cc/Bcc shell copies are not stored as separate timeline messages. |
| **To** `SOURCE_ACCOUNT` directly (e.g. screenshot) | Pulls the inbox Gmail thread as the capture; OCR/vision on images; **subject line included** in prompts. |

Gmail inbox search uses `(to:inbox OR cc:inbox OR bcc:inbox)` plus recipient checks on each message.

### Thread identity: inbox tracking vs timeline messages

Email threads use **two related ids**. Do not conflate them when changing ingestion, snooze, or the dashboard.

| Layer | Table / field | What it is | Used for |
|-------|----------------|------------|----------|
| **Inbox tracking** | `thread_tracking.inbox_thread_id` | How this conversation was registered from the Fivelanes inbox (Gmail `threadId` on `SOURCE_ACCOUNT`, or `rfc:…` for Cc/Bcc) | Snooze, remove, plans, lanes, dashboard thread list |
| **Inbox Gmail thread** | `thread_tracking.gmail_inbox_thread_id` | For Cc/Bcc only: the real Gmail `threadId` on the Fivelanes inbox account when `inbox_thread_id` is an RFC key | Peeking inbox deliveries, pruning inbox shell rows |
| **Timeline grouping** | `timeline_entries.thread_id` | Same value as `inbox_thread_id` after expansion (`bind_timeline_rows_to_inbox_thread`) | Grouping messages in the UI and LLM pipeline per tracked thread |
| **Timeline message key** | `timeline_entries.source_id` | Gmail **message** id from the mailbox thread where the conversation **lives** (resolved via RFC `Message-ID` on the forwarder's OAuth account) | Deduping messages, segmentation cache, image fetches |

**Snooze and removal are unchanged by source-thread resolution.** They always target `thread_tracking.inbox_thread_id` (and the matching `timeline_entries.thread_id` / `claude_message_outputs.thread_id`). `thread_tracking.snoozed` is `0` active, `1` snoozed, `2` removed. The Fivelanes inbox delivery remains tracked even when message bodies are pulled from Personal, LHC, or another connected account.

**Why `source_id` is not the inbox copy's message id:** The same physical email in Gmail has a different message id in each mailbox. A forward or Cc/Bcc to `SOURCE_ACCOUNT` gets one id in the Fivelanes inbox and another in the forwarder's sent/received thread. Fivelanes stores ids from the **source mailbox thread** (see `resolve_source_mailbox_thread` in [`services/email/thread_resolve.py`](services/email/thread_resolve.py) and `_try_expand_from_source_mailbox_thread` in [`services/email/inbox_process.py`](services/email/inbox_process.py)). Inbox forward/cc shell ids are irrelevant for `timeline_entries` and must not be used for deduplication.

**End-to-end flow:**

1. Inbox pull creates a `thread_tracking` row from the seed message (`build_tracking_row`).
2. `expand_thread` resolves the conversation on the forwarder's connected account(s) and pulls bodies from that Gmail thread.
3. `bind_timeline_rows_to_inbox_thread` sets `timeline_entries.thread_id` to the inbox tracking key so dashboard state stays aligned.
4. `upsert_timeline_entries` dedupes on `source_id` (canonical message ids from the source thread).

Implementation reference: [`utils/database.py`](utils/database.py) (schema module doc), [`services/thread_snooze.py`](services/thread_snooze.py).

**One-time cleanup** after upgrading to source-thread ingestion:

```bash
python3 scripts/reconcile_timeline_inbox_duplicates.py --all
```

Use `--dry-run` to preview counts. `--refresh-inbox` needs Gmail OAuth; `--prune-content-dupes` is offline.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development notes.

## Publishing a public release

The infrastructure ships without private data. To publish a fresh public repo with no git history, export the cleaned tree and push to a new remote:

```bash
git archive HEAD | tar -x -C /tmp/fivelanes-public
cd /tmp/fivelanes-public
git init && git add -A && git commit -m "Initial public release: Fivelanes infrastructure"
git remote add origin git@github.com:YOUR_ORG/fivelanes.git
git push -u origin main
```

Keep your private data directory (and any local clone bootstrap `.env`) separate — never commit `FIVELANES_DATA_ROOT` contents.
