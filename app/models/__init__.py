"""Domain models.

Importing this package registers every model on ``app.db.Base.metadata``, which
is what Alembic autogenerate and ``create_all`` rely on.
"""

from app.models.allowed_sender import AllowedSender
from app.models.event import Event, EventRecurrence
from app.models.expense import ExpensePeriod, RecurringExpense
from app.models.inbox import InboxMessage, InboxStatus
from app.models.job_run import JobRun, JobRunStatus
from app.models.notification import Notification, NotificationStatus
from app.models.reminder import Reminder

__all__ = [
    "AllowedSender",
    "Event",
    "EventRecurrence",
    "ExpensePeriod",
    "InboxMessage",
    "InboxStatus",
    "JobRun",
    "JobRunStatus",
    "Notification",
    "NotificationStatus",
    "RecurringExpense",
    "Reminder",
]
