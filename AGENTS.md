# Personal Manager — Agent Instructions

## Project

This is a small single-user personal manager written in Python.

The application manages:

* important dates and recurring events;
* recurring expenses;
* reminders;
* selected Gmail messages;
* Telegram notifications and read-only queries;
* Google Sheets as the human-facing management interface;
* SQLite as the persistent internal database;
* LLM-based interpretation of natural language and email classification.

The application is intentionally small. Prefer simple, explicit solutions over infrastructure-heavy abstractions.

---

## Core Architecture

```text
Google Sheets
      ↓
Python application
      ↓
SQLite
      ↑
Python business logic
      ↓
 ┌──────────────┬──────────────┬──────────────┐
 │    Gmail     │   DeepSeek   │   Telegram   │
 │     API      │  OpenRouter  │  Bot API     │
 └──────────────┴──────────────┴──────────────┘
```

### Responsibilities

**Google Sheets**

* Human-facing interface.
* Source of user-managed data and configuration.
* Used to create/edit events, expenses, email rules, etc.
* Not a replacement for the database.

**SQLite**

* Persistent source of truth for application state.
* Stores normalized entities, reminder state, processed emails, job history and logs.
* Must allow the application to continue working if Google Sheets is temporarily unavailable.

**Python**

* Owns business logic.
* Owns validation.
* Owns calculations.
* Owns date/time logic.
* Owns scheduling decisions.
* Owns database writes.
* Decides whether an LLM result is safe to persist.

**DeepSeek via OpenRouter**

* Used only where semantic understanding is useful:

  * natural-language parsing;
  * ambiguous user input;
  * email classification;
  * understanding read-only Telegram queries.
* Must not directly access SQLite.
* Must not directly modify application state.
* Must return structured data when used for application logic.

**Gmail**

* Read-only.
* Only process configured/whitelisted senders or sources.
* Do not scan the entire mailbox unnecessarily.

**Telegram**

* Push notifications for important events/errors.
* Read-only information queries.
* Do not use Telegram as an administration interface.
* Do not add/edit/delete application data through Telegram.
* Use Telegram Bot API over HTTP.
* Prefer `getUpdates` long polling; do not introduce webhooks unless there is a concrete requirement.

**Scheduler**

* Used to wake the application and execute scheduled jobs.
* APScheduler is sufficient.
* Scheduler state must not be the application's source of truth.
* Persistent job/reminder state belongs in SQLite.

---

# General Development Rules

## Keep the project small

Do not introduce infrastructure or abstractions without a concrete need.

Do NOT add:

* Celery;
* Redis;
* RabbitMQ;
* PostgreSQL;
* FastAPI;
* aiogram;
* LangChain;
* LangGraph;
* MCP;
* vector databases;
* message queues;
* microservices;
* Kubernetes;
* unnecessary background workers.

The project is a small single-user application.

Use simple Python code and existing libraries.

---

## Python

Target modern Python 3.13+.

Prefer:

* type hints;
* `async`/`await` for network I/O;
* Pydantic for external/input validation;
* SQLAlchemy 2.x for database access;
* `httpx` for HTTP;
* standard library where sufficient.

Avoid unnecessary frameworks.

Keep functions small and responsibilities explicit.

---

# Database

SQLite is the primary persistent database.

Use SQLAlchemy 2.x.

Use Alembic for schema migrations.

Never assume that deleting/recreating the database is an acceptable migration strategy.

Database changes must be represented by migrations.

### Important rule

The LLM must NEVER write directly to the database.

Correct flow:

```text
Natural language
      ↓
DeepSeek
      ↓
Structured output
      ↓
Pydantic validation
      ↓
Business validation
      ↓
Python application logic
      ↓
SQLAlchemy
      ↓
SQLite
```

The application must be able to reject an LLM result.

An LLM response is untrusted input.

---

# LLM

DeepSeek is accessed through OpenRouter.

Implement a small internal abstraction around the provider.

For example:

```python
class AIClient:
    async def parse_event(...): ...
    async def parse_expense(...): ...
    async def classify_email(...): ...
    async def understand_query(...): ...
```

Do not spread OpenRouter-specific API calls throughout the application.

The rest of the application should not care which specific model/provider is used.

### LLM principles

Do not use the LLM for deterministic work.

Do NOT ask the LLM to:

* calculate financial totals;
* determine whether a reminder is due;
* calculate dates when Python can do it;
* maintain application state;
* decide database writes;
* perform simple validation.

Use Python for those tasks.

Use the LLM for semantic interpretation.

---

# Pydantic

Use Pydantic models at external boundaries.

Examples:

* Google Sheets parsed data;
* Gmail message classification;
* LLM structured output;
* Telegram queries;
* application configuration.

Validate data before it enters the domain/database layer.

Never assume an LLM response is valid merely because it matches the expected JSON shape.

---

# Google Sheets

Google Sheets is a human-facing interface.

Do not make the Sheets structure unnecessarily similar to the SQL schema.

The user should be able to understand and edit the sheets without knowing the database design.

Likely sheets include:

* `Events`
* `Expenses`
* `Email`
* `Inbox`
* `Reminders`
* `Settings`
* optionally logs/history

Do not create additional sheets unless there is a concrete reason.

Google Sheets may be temporarily unavailable.

The application must not lose important state because Sheets is unavailable.

---

# Events

Events may include:

* birthdays;
* anniversaries;
* meetings;
* other important dates.

Events can be recurring.

Reminder timing is user-configurable.

Examples:

```text
Birthday → on the day
Anniversary → 15 days before
Meeting → 1 day before
```

Date calculations belong to Python.

---

# Expenses

Recurring expenses may be:

* monthly;
* quarterly;
* yearly;
* other explicitly supported periods.

Store structured information such as:

* amount;
* currency;
* period;
* payment day;
* category;
* reminder lead time;
* active/inactive state.

Financial calculations must be deterministic Python code.

The application should distinguish between:

1. actual cash outflow during a period;
2. normalized average monthly cost.

Do not use an LLM for arithmetic.

---

# Gmail

Use the official Gmail API.

Prefer read-only access.

Only inspect messages that match configured rules/whitelisted senders whenever possible.

Track processed Gmail message IDs in SQLite.

A previously processed email should not normally be sent to the LLM again.

Minimize data sent to the LLM:

* sender;
* subject;
* date;
* relevant body content.

Do not send unnecessary private mailbox contents.

---

# Telegram

Telegram is primarily a notification channel.

Use the Telegram Bot API through HTTP with `httpx`.

No aiogram is required unless a concrete future requirement makes it useful.

Use long polling (`getUpdates`) rather than webhooks.

Telegram interactions are read-only.

Examples:

```text
"Сколько я трачу на подписки?"
"Сколько обязательных платежей будет в следующем месяце?"
"Какие платежи предстоят на этой неделе?"
```

Queries must never mutate application state.

---

# Notifications

Telegram is the **important alert channel**.

Do not send every log message to Telegram.

### Send to Telegram

Examples:

* critical job failure;
* job appears to have crashed;
* important integration failure;
* important reminder;
* action requiring the user's attention;
* important email requiring action;
* inability to safely process something important.

Example:

```text
⚠️ Ежедневная проверка не завершилась.

Запуск: 10:00
Статус: running
Причина: вероятно, процесс был остановлен.

Проверь работу менеджера.
```

### Do not send to Telegram

Examples:

* ordinary successful job execution;
* debug messages;
* normal API calls;
* minor recoverable errors;
* routine synchronization information.

These belong in persistent logs/history.

---

# Job Execution and Reliability

A scheduled task being started is NOT the same as successfully completing it.

Every important scheduled job must have persistent execution state.

Use a concept such as:

```text
job_runs
```

with fields similar to:

```text
id
job_name
scheduled_at
started_at
finished_at
status
error
result
```

Possible statuses:

```text
pending
running
success
failed
```

The database is the source of truth.

APScheduler only triggers execution.

### Crash detection

If a job remains `running` longer than a reasonable timeout, treat it as suspicious/failed.

Example:

```text
started_at = 10:00
current time = 10:45
status = running
```

The next health check can mark it as failed:

```text
status = failed
error = "Job probably crashed or timed out"
```

and send an important Telegram alert.

Do not silently ignore failed jobs.

---

# Task-level execution

For complex jobs, individual steps may also be tracked.

Example:

```text
daily_agent
├── sync_google_sheets
├── check_gmail
├── process_emails
├── calculate_reminders
└── send_notifications
```

A job can therefore provide a useful execution history:

```text
Google Sheets sync      success
Gmail check             failed
Email processing        skipped
Reminder calculation    success
Telegram notifications  success
```

Do not over-engineer this.

Add task-level tracking when it provides real diagnostic value.

---

# Logging

Use normal Python logging for application logs.

Persistent important execution history belongs in SQLite.

Do not treat Google Sheets as the primary logging system.

Google Sheets may contain a human-readable history/log view.

SQLite remains the authoritative source.

---

# Error Handling

Do not silently swallow exceptions.

Bad:

```python
try:
    ...
except Exception:
    pass
```

If an error is recoverable:

1. log it;
2. retry when appropriate;
3. record the outcome;
4. continue safely if possible.

If an error affects an important operation:

1. record failure;
2. make the job state explicit;
3. send a Telegram alert if user action is required.

Do not send Telegram alerts for every transient error.

---

# Retries

Retries must be deliberate.

Good candidates:

* temporary HTTP failures;
* rate limits;
* temporary Gmail/API failures;
* temporary OpenRouter failures;
* Telegram API temporary failures.

Do not retry indefinitely.

Use bounded retries with sensible delays.

A retry must not accidentally duplicate an important side effect.

---

# Idempotency

Scheduled jobs may run more than once.

Design important operations to be idempotent where practical.

Examples:

* processing the same Gmail message twice should not create duplicate records;
* sending a reminder should not create duplicate reminders;
* synchronizing Sheets should not duplicate entities;
* restarting the application should not corrupt state.

Use stable IDs and database constraints where appropriate.

---

# Configuration and Secrets

Secrets must never be committed.

Use environment variables / `.env` locally.

Examples:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
OPENROUTER_API_KEY
GOOGLE credentials
DATABASE_URL
```

Never put tokens, API keys or OAuth secrets into:

* source code;
* `AGENTS.md`;
* Google Sheets;
* logs;
* test fixtures committed to git.

---

# Testing

Use pytest.

Prioritize tests for business-critical deterministic logic:

* recurring date calculation;
* reminder calculation;
* expense calculations;
* idempotency;
* job state transitions;
* failure handling;
* parsing/validation.

Do not waste time testing trivial wrappers around third-party APIs.

LLM-dependent functionality should have deterministic mocked tests.

Do not require a real OpenRouter request for normal test execution.

---

# Code Changes

Before changing architecture, inspect the existing code.

Prefer modifying the smallest number of files necessary.

Do not create new abstractions merely because they look architecturally clean.

Do not rewrite working code without a concrete reason.

When implementing a feature:

1. understand existing behavior;
2. identify the smallest appropriate change;
3. implement it;
4. add/update tests;
5. verify the result.

Keep changes focused.

---

# Documentation

Do not create additional Markdown documentation unless it provides real value.

`AGENTS.md` is the primary AI-agent project documentation.

If architecture becomes substantially more complex, a separate document may be introduced, but do not create documentation merely for completeness.

---

# Definition of Done

A feature is not complete merely because the code runs.

For important features verify:

* validation exists;
* errors are handled;
* persistent state is correct;
* restart behavior is safe;
* duplicate execution is safe where applicable;
* tests cover important deterministic behavior;
* secrets are not exposed;
* Telegram alerts are used only when appropriate.

When unsure, prefer the simpler implementation that preserves correctness and observability.

## Google Credentials Security

The project uses local Google credentials for Gmail and Google Sheets integration.

Credential files are stored outside the source code and must be accessed only by the running Python application using their configured absolute paths:

* Gmail OAuth client secret:
  `C:\Users\ZederBreys\Desktop\testManager\folderenv\client_secret_186027981043-g6m9lieq14bg9f0spbfpq8h649an2od2.apps.googleusercontent.com.json`
* Google Sheets Service Account:
  `C:\Users\ZederBreys\Desktop\testManager\folderenv\glim-330513-92e5c456b0d5.json`
* Gmail OAuth token:
  `C:\Users\ZederBreys\Desktop\testManager\folderenv\token.json`

### Strict rules

* **NEVER open, read, inspect, parse, print, or otherwise access the contents of these credential files directly.**
* Do not output credentials, tokens, private keys, client secrets, or their contents anywhere.
* Do not copy these files into the project, `.env`, Git, Docker images, logs, tests, reports, or generated files.
* Do not attempt to discover or infer values stored inside these files.
* Python application code may pass these file paths to the appropriate Google authentication libraries so the application itself can load the credentials at runtime.
* Tests should use mocks/fake credentials whenever real credentials are not required.
* If an OpenCode/tool permission request would grant access to the contents of these credential files, **do not request, approve, or proceed with that access automatically**. The user will review such permission requests manually.
* The fact that the paths are present in this document does **not** grant permission to read the files.
* Treat these files as secrets even though their paths are explicitly provided.
