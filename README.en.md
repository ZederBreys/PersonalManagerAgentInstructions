[🇷🇺 Русский](README.md) | 🇬🇧 English

# Personal Manager

A small single-user personal manager: events, reminders, recurring expenses,
incoming messages and notifications. SQLite is the source of truth; external
services are used only as auxiliary channels.

## Features

- **Events** — one-off and yearly (`yearly`), with correct February 29 handling.
- **Reminders** — derived from events: "on the day" and "N days before", unique per (event, date).
- **Recurring expenses** — monthly/quarterly/yearly; amounts stored as integer minor units (cents), next-payment-date calculation.
- **Job tracking** — a `job_runs` table (pending/running/success/failed), stale-job detection and startup recovery.
- **Telegram notifications** — only important events (job failed, job interrupted).
- **Google Sheets** — human-facing interface: two-way sync of events and expenses (a new row with an empty ID creates a record and receives its ID; deleting a row deletes nothing), reminders export.
- **Inbox + DeepSeek classification** — category, importance, summary, "action required" flag.
- **Gmail** — read-only import of messages from whitelisted senders (exact address match).

## Architecture

SQLite is the single source of truth; Python owns the business logic. APScheduler
only triggers jobs, while their state lives in the database. Google Sheets is the
human-facing interface, Telegram is the notification channel, Gmail is a read-only
source of messages, and DeepSeek only performs semantic classification. The LLM
never writes to the database directly and never performs deterministic calculations.

## Stack

- Python 3.13+
- SQLAlchemy 2.x (async) + aiosqlite
- Alembic (migrations)
- APScheduler 3.x
- httpx
- Pydantic + pydantic-settings
- google-api-python-client, google-auth, google-auth-oauthlib

## Requirements

- Python 3.13 or newer.
- For Telegram / Google Sheets / Gmail / DeepSeek — the corresponding credentials (see "Configuration").

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows PowerShell
pip install -e ".[dev]"
```

## Configuration

Copy `.env.example` to `.env` and fill in the values you need. `.env` is
git-ignored. Every variable is optional — without it the application still runs
and the corresponding integration is simply disabled.

| Variable | Purpose | Default |
| --- | --- | --- |
| `LOG_LEVEL` | Logging level | `INFO` |
| `DATABASE_URL` | SQLite URL (SQLAlchemy async) | `sqlite+aiosqlite:///./data/personal_manager.sqlite3` |
| `JOB_STALE_TIMEOUT_SECONDS` | After how many seconds a `running` job is considered stale | `3600` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (empty — Telegram disabled) | — |
| `TELEGRAM_CHAT_ID` | Chat ID for notifications (empty — notifications disabled) | — |
| `TELEGRAM_POLL_TIMEOUT` | Long-polling timeout | `30` |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the Sheets service-account JSON | — |
| `GOOGLE_SPREADSHEET_ID` | Google Sheets spreadsheet ID | — |
| `DEEPSEEK_API_KEY` | DeepSeek key (empty — classification disabled) | — |
| `DEEPSEEK_MODEL` | DeepSeek model | `deepseek-chat` |
| `DEEPSEEK_TIMEOUT_SECONDS` | DeepSeek request timeout | `30` |
| `GMAIL_CLIENT_SECRET_FILE` | Path to the OAuth client-secret JSON | — |
| `GMAIL_TOKEN_FILE` | Path to the OAuth token JSON | — |
| `GMAIL_MAX_MESSAGES` | Maximum number of messages imported per run | `100` |

`GOOGLE_SERVICE_ACCOUNT_FILE`, `GMAIL_CLIENT_SECRET_FILE` and `GMAIL_TOKEN_FILE`
point to external credential files; the files themselves must never be committed
to the repository.

## External integrations

- **Telegram** — push notifications only (job failed/interrupted) via the Bot API (httpx).
- **Google Sheets** — sync via a service account; every row is validated on its own, invalid rows are skipped and never overwritten by export.
- **Gmail** — read-only import of messages from whitelisted senders (`support@liteserver.nl`, `admin@ztv.su`), exact address match.
- **DeepSeek** — semantic classification of incoming messages.

## Database

SQLite via SQLAlchemy (async) and aiosqlite. The schema is managed with Alembic:

```bash
alembic upgrade head     # apply migrations
alembic check            # detect drift between models and migrations
alembic downgrade base   # roll back all migrations
```

## Running

```bash
python -m app
```

One long-lived process (for systemd): recovers stale jobs, starts the scheduler
(health check, daily reminders and — when configured — Google Sheets sync, Gmail
import, inbox classification) and Telegram long polling. Runs until
SIGTERM/SIGINT, then shuts down gracefully. Exit code 1 means it could not start
(e.g. migrations not applied), 2 means invalid configuration.

One-time Gmail authorization (opens a browser for the OAuth consent screen):

```bash
python -m app.gmail
```

## Testing

```bash
pytest
```

## Security

Secrets live only in `.env` and external credential files and must not be
committed. `.gitignore` excludes `.env`, credential `*.json` files and
`*.sqlite3`. Tokens, keys and message bodies are never logged.

## Project structure

```text
app/
    config.py               # settings (pydantic-settings)
    db.py                   # async engine, session, declarative base
    dates.py                # pure event date helpers
    expense_dates.py        # pure expense date helpers
    events.py               # event logic
    reminders.py            # reminder logic
    expenses.py             # expense logic
    job_runs.py             # job-run state
    scheduler.py            # APScheduler and job wrapper
    notifications.py        # Telegram notifications
    inbox.py                # inbox message service
    inbox_processing.py     # inbox processing (DeepSeek glue)
    models/                 # SQLAlchemy models
    telegram/               # Telegram Bot API client
    google_sheets/          # client, mappers, export/import
    gmail/                  # OAuth, client, parser, importer
    deepseek/               # DeepSeek client and classification schemas
alembic/                    # migrations
tests/                      # tests
```
