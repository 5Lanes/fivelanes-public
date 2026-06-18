# Fivelanes

Fivelanes pulls email, text threads, and calendar events into `timeline.db`, resolves conversation threads across connected OAuth accounts, and runs segmentation and summaries for the dashboard.

Licensed under the [MIT License](LICENSE).

## Input sources

Fivelanes accepts data through four channels. The scheduled pipeline (`fivelanes.main` / dashboard scheduler) pulls email and calendar automatically; text threads are file-based and must be selected in the dashboard before they appear on Threads.

| Channel | How data arrives | Where it lands | Processing |
|---------|------------------|----------------|------------|
| **Email** | Mail to `SOURCE_ACCOUNT` (forward, Cc/Bcc, or direct To) | Gmail API → `thread_tracking`, `timeline_entries` | Segmentation + LLM summary (same as inbox pipeline) |
| **Text** | JSON files in `conversations/` (iMessage export shape) | `thread_tracking` (`text:` prefix) when tracked | Summary only (no email-style segmentation) |
| **Calendar** | Google Calendar OAuth (connected accounts) | `out/availability_calendar_latest.json`, `meetings` table | Availability export; context for summaries and meeting prep |
| **Dashboard** | HTTP POST to `/api/*` (tracking, plans, lanes, pipeline run, drafts) | `timeline.db` | User actions and on-demand LLM calls |

### Email

The primary input is a dedicated Fivelanes inbox (`SOURCE_ACCOUNT`). Connected Gmail OAuth accounts (`credentials/credentials.json`, `credentials/tokens.json`) supply both the inbox pull and thread resolution across mailboxes.

Mail is pulled with `(to:inbox OR cc:inbox OR bcc:inbox)` plus recipient checks. Four delivery routes are handled in [`services/email/inbox_process.py`](services/email/inbox_process.py) — see [Inbox delivery scenarios](#inbox-delivery-scenarios) below.

Image-only captures (direct To with screenshots) run OCR (Tesseract) first, then a vision model fallback.

Run manually:

```bash
python -c "from services.email import populate_timeline; populate_timeline(lookback_days=14)"
```

### Text threads

On-disk JSON under `conversations/` (override with `TEXTS_CONVERSATIONS_DIR`) holds iMessage/SMS exports — one file per thread, filename stem = conversation key (e.g. `+15551234567.json`). Each file is a list of message objects with fields like `text`, `date`, `handle`, `is_from_me`, and `guid`.

Fivelanes does not sync texts from a phone automatically. Export conversations externally, drop the JSON files into `conversations/`, then open **Texts setup** (`/texts-setup`) to choose which threads to track. Tracked threads are registered in `thread_tracking` with `inbox_thread_id` `text:<key>` and merged into the Threads view. Summaries are generated via `/api/texts/summarize` or as part of `fivelanes.main`.

### Calendar

Google Calendar is read through the same OAuth tokens. After each pipeline run (unless `CALENDAR_AVAILABILITY_DISABLE=1`), events are exported to `out/availability_calendar_latest.json` and synced into the `meetings` table. Optional scheduling rules in `credentials/calendar_scheduling_rules.json` filter which calendars count and set buffers/timezone.

Run manually:

```bash
python scripts/pull_calendar_availability.py
```

### Dashboard and scheduler

`dashboard_server.py` serves the UI and JSON API. Besides text-thread selection, the dashboard accepts snooze/remove on threads, lane and plan edits, meeting-prep and email-reply prompts (user intent → LLM), and manual pipeline runs (`POST /api/pipeline/run`).

The background scheduler (`utils/run_fivelanes_scheduler.py`, also started with the dashboard) runs the full cycle every `FIVELANES_INTERVAL_SEC` (default 15 minutes), skipping quiet hours (`FIVELANES_QUIET_START_HOUR`–`FIVELANES_QUIET_END_HOUR` in `FIVELANES_SCHEDULER_TZ`).

**Security note:** The dashboard has no authentication and exposes `/timeline.db`. It is intended for trusted LAN use only. See [SECURITY.md](SECURITY.md).

## Setup

1. Copy [`.env.example`](.env.example) to `.env`.
2. Copy [`services/prompts.example.json`](services/prompts.example.json) to `services/prompts.json` and fill in your LLM prompts.
3. Configure required values in `.env`:
   - `SOURCE_ACCOUNT` — Fivelanes inbox address (e.g. `you+fivelanes@example.com`)
   - `SOURCE_OAUTH_ACCOUNT_ID` — label for the Gmail OAuth account that owns that inbox
   - `OWNER_NAME` — your display name (used in summary assembly and UI heuristics)
4. Gmail OAuth:
   - Copy [`credentials/credentials.example.json`](credentials/credentials.example.json) to `credentials/credentials.json`
   - Run `python utils/add_account.py you@example.com` to create `credentials/tokens.json`
   - Optional: copy [`credentials/calendar_scheduling_rules.example.json`](credentials/calendar_scheduling_rules.example.json) to `credentials/calendar_scheduling_rules.json`
5. Install dependencies:
   - Python: `pip install -r requirements-linux.txt` (system package `tesseract-ocr` for image OCR)
   - Frontend: `cd frontend && npm install && npm run build`
6. Optional: `FIVELANES_BACKEND` (`llama` or `claude`), Ollama host/models, or `CLAUDE_API_KEY` — see `.env.example`.

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
| **To** `SOURCE_ACCOUNT` with subject `todo:` | Creates a **Plan** from the remainder of the subject. No `thread_tracking` or `timeline_entries`. |
| **Forward** to `SOURCE_ACCOUNT` | Tracks via inner RFC `Message-ID`, resolves the real thread in connected mailboxes, **drops** the forward-to-inbox shell from the timeline. |
| **Cc/Bcc** `SOURCE_ACCOUNT` (inbox not in To) | Tracks via envelope RFC id, resolves the source mailbox thread, **keeps** the Cc/Bcc copy in the Fivelanes inbox in the timeline. |
| **To** `SOURCE_ACCOUNT` directly (e.g. screenshot) | Pulls the inbox Gmail thread as the capture; OCR/vision on images; **subject line included** in prompts. |

Gmail inbox search uses `(to:inbox OR cc:inbox OR bcc:inbox)` plus recipient checks on each message.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development notes.

## Publishing a public release

The infrastructure ships without `services/prompts.json`. To publish a fresh public repo with no git history, export the cleaned tree and push to a new remote:

```bash
git archive HEAD | tar -x -C /tmp/fivelanes-public
cd /tmp/fivelanes-public
git init && git add -A && git commit -m "Initial public release: Fivelanes infrastructure"
git remote add origin git@github.com:YOUR_ORG/fivelanes.git
git push -u origin main
```

Keep your private fork (with real prompts, credentials, and data) separate.
