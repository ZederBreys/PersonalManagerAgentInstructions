"""Domain models.

Importing this package registers every model on ``app.db.Base.metadata``, which
is what Alembic autogenerate and ``create_all`` rely on.
"""

from app.models.event import Event, EventRecurrence
from app.models.expense import ExpensePeriod, RecurringExpense
from app.models.inbox import InboxMessage, InboxStatus
from app.models.job_run import JobRun, JobRunStatus
from app.models.reminder import Reminder

__all__ = [
    "Event",
    "EventRecurrence",
    "ExpensePeriod",
    "InboxMessage",
    "InboxStatus",
    "JobRun",
    "JobRunStatus",
    "RecurringExpense",
    "Reminder",
]
