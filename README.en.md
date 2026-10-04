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

## Filling in the Google Sheet

The `Events`, `Expenses` and `Email` sheets are for entering and editing data. New record: fill in a row and
**leave the `ID` column empty**; a few seconds after you stop typing the bot creates the record and
writes its number into `ID`. A row with an error is skipped and left as typed while the other rows are
imported.

**Feedback on the `ID` cell.** After every check the `ID` cell is coloured: green — the row was
accepted, red — it was not. Hover over it: the note shows **how the bot understood the row** (the
values as stored, including the computed reminder dates) or **why it was rejected** and what is
allowed. The bot overwrites your own colours and notes in the `ID` column.

**What you may change in the sheet.**

| Action | Allowed? |
|---|---|
| Rename a tab, reorder tabs | yes — the bot finds sheets by a hidden marker, not by name or position |
| Rename the header of any column | yes — columns are identified by order, not by name; the first row is always the header |
| Reorder columns or insert a column inside the table | no — the column order is fixed |
| Write your own notes to the right of the table | yes: from column `J` in Events, from `K` in Expenses (`H` and `I` in Events are filled by the bot) |
| Insert blank rows, sort | yes |
| Your own date or amount format (e.g. a long weekday date, `2 500 ₽`) | yes — the bot reads the stored value, not what is displayed |
| An amount whose cell format hides the value (e.g. `12.5` shown as a date) | the row is rejected with an explanation in the note; reset the format (Format → Number → Automatic) and retype the amount |
| A checkbox in the "Активно" column | yes — it stays a checkbox; the bot does not replace it with a word |
| A row holding only your own note (no ID, name, etc.) | ignored, not an error; rows that are not the bot's are never rewritten |
| Delete a row | does not delete the record: the row comes back. To delete a record write the word `удалить` (any letter case) in the "Активно" column — the record is deleted with its reminders and the row is cleared |

**Events** — `Событие` (name) and `Дата` (date) are required:

| Column | What to write |
|---|---|
| Дата | `24.12.2026` or `2026-12-24`; for yearly events the bot keeps the next occurrence |
| Повтор | once: `none`, `разово`, `один раз`; every year: `yearly`, `ежегодно`, `каждый год`, `через год`. Events have no other intervals (monthly payments belong on the Expenses sheet) |
| Напоминание | days before to remind: `3`, `за 15 дней`, `за день до события`, `за неделю`, `в день события`; several separated by a comma or «и»: `0, 15`, `за 15 дней и за 3 дня` |
| Что сделать | any text |
| Активно | `да` / `нет` (or a checkbox): with `нет` the event is paused — no reminders and it is not moved to next year; empty = `да`; `удалить` — delete the record |
| Когда будет напомнено | filled by the bot: the date of the next reminder |
| Когда событие случится | filled by the bot: the date of the next occurrence |

**Expenses** — `Название`, `Сумма`, `Валюта`, `День оплаты`, `Следующая оплата` are required:

| Column | What to write |
|---|---|
| Сумма | `1000`, `12,50`, `1 000,50` |
| Валюта | `RUB`, `USD`, `EUR` (or `₽`, `$`, `€`, `руб`) |
| Период | `monthly` / `ежемесячно`, `quarterly` / `ежеквартально`, `yearly` / `ежегодно` (empty = monthly) |
| День оплаты | a number from 1 to 31 |
| Следующая оплата | a date, as in Events |
| Активен | `да` / `нет` (or a checkbox); `удалить` — delete the record |
| Напомнить за (дн.) | days before to remind about the payment: `3` or `за 3 дня` (one value; empty = 3) |

An empty cell when editing an existing row means "leave unchanged".

**Email** — the addresses the bot reads mail from. `Отправитель` (sender) is required:

| Column | What to write |
|---|---|
| Отправитель | one address: `billing@example.com` (letter case does not matter; `mailto:…` and `Name <address>` are also accepted). Not allowed: a whole domain (`@example.com`, `example.com`), several addresses in one cell, an incomplete address |
| Активно | `да` / `нет` (or a checkbox); `нет` pauses the address, its mail is not read; empty = `да`; `удалить` — delete the address |

A green `ID` cell means the address was accepted (the note shows it as stored), red means it was not
(the note gives the reason). A change applies on the next mail import. With no active address the bot
does not read mail at all. The address is compared exactly; a whole domain is never allowed. The two
original addresses (`support@liteserver.nl`, `admin@ztv.su`) are added automatically on upgrade.

**Inbox** is view-only: the bot writes the newest 200 received letters here (Moscow time, sender,
subject, category, importance, summary, whether action is needed, status). Anything typed there by
hand is overwritten. **Reminders** is view-only too. The bot does not use a `Settings` tab: it can be
deleted without breaking anything and the bot will not re-create it.

**Speed.** The bot checks the sheet every 5 seconds (`SHEETS_POLL_SECONDS`) with one light request and
syncs once the edits stop changing — about 5–10 seconds after you stop typing. A full sync still runs
every 30 minutes as a safety net.

## External integrations

- **Telegram** — push notifications only (job failed/interrupted) via the Bot API (httpx).
- **Google Sheets** — sync via a service account; every row is validated on its own, invalid rows are skipped and never overwritten by export.
- **Gmail** — read-only import of messages from allowed senders (the list is the `Email` sheet, stored in the DB), exact address match.
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
