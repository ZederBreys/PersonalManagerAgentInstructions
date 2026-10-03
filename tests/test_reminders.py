"""Domain operation tests for Reminders."""

from __future__ import annotations

import asyncio
from datetime import date

import app.db as db
from app.events import advance_event, create_event, get_event, update_event
from app.models import EventRecurrence
from app.reminders import (
    generate_reminders,
    get_due_reminders,
    get_pending_reminders,
    get_reminders_for_event,
    mark_done,
    mark_sent,
    regenerate_reminders,
)


def _run(coro) -> None:
    asyncio.run(coro)


def test_generate_reminders_day_of_and_before(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev, offsets=(0, 15))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            assert [r.remind_at for r in reminders] == [
                date(2026, 11, 3),
                date(2026, 11, 18),
            ]

    _run(_r())


def test_generate_reminders_idempotent(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev)
            await generate_reminders(s, ev)
            await generate_reminders(s, ev)
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            assert len(reminders) == 1
            assert reminders[0].remind_at == date(2026, 11, 18)

    _run(_r())


def test_pending_done_sent(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev)
            await s.commit()
            eid = ev.id

        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            r = reminders[0]
            pending = await get_pending_reminders(s)
            assert [x.id for x in pending] == [r.id]
            await mark_done(s, r)
            await s.commit()

        async with db.get_session() as s:
            assert await get_pending_reminders(s) == []
            r = (await get_reminders_for_event(s, eid))[0]
            assert r.is_done is True
            await mark_sent(s, r)
            await s.commit()

        async with db.get_session() as s:
            r = (await get_reminders_for_event(s, eid))[0]
            assert r.is_sent is True
            assert r.sent_at is not None

    _run(_r())


def test_get_due_reminders(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            due_ev = await create_event(s, name="due", next_date=date(2026, 1, 1))
            future_ev = await create_event(s, name="future", next_date=date(2030, 1, 1))
            await generate_reminders(s, due_ev)
            await generate_reminders(s, future_ev)
            await s.commit()

        async with db.get_session() as s:
            due = await get_due_reminders(s, today=date(2026, 9, 27))
            assert [r.remind_at for r in due] == [date(2026, 1, 1)]

    _run(_r())


def test_regenerate_reminders_replaces_only_pending(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev, offsets=(0, 15))
            await s.commit()
            eid = ev.id

        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            sent = next(r for r in reminders if r.remind_at == date(2026, 11, 18))
            await mark_sent(s, sent)
            await s.commit()

        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await regenerate_reminders(s, ev)
            await s.commit()

        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            by_date = {r.remind_at: r for r in reminders}
            # sent reminder preserved; pending offset-15 reminder recreated
            # because the event's offsets are now persisted
            assert date(2026, 11, 18) in by_date
            assert by_date[date(2026, 11, 18)].is_sent is True
            assert date(2026, 11, 3) in by_date
            assert by_date[date(2026, 11, 3)].is_sent is False
            assert by_date[date(2026, 11, 3)].is_done is False
            assert len(reminders) == 2

    _run(_r())


def test_update_event_regenerates_pending_preserves_sent(schema) -> None:
    async def _r() -> None:
        # Event date A -> generate reminders -> mark one sent
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev, offsets=(0, 15))
            await s.commit()
            eid = ev.id

        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            day_of = next(r for r in reminders if r.remind_at == date(2026, 11, 18))
            await mark_sent(s, day_of)
            await s.commit()

        # Update Event date A -> B
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await update_event(s, ev, next_date=date(2026, 12, 20))
            await s.commit()

        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            by_date = {r.remind_at: r for r in reminders}

            # sent reminder is history and must survive
            assert date(2026, 11, 18) in by_date
            assert by_date[date(2026, 11, 18)].is_sent is True

            # pending reminders now match B with the persisted offsets (0, 15)
            assert date(2026, 12, 20) in by_date
            assert date(2026, 12, 5) in by_date
            for d in (date(2026, 12, 5), date(2026, 12, 20)):
                assert by_date[d].is_sent is False
                assert by_date[d].is_done is False

            # no duplicates
            assert len(reminders) == 3

    _run(_r())


def test_generate_persists_offsets(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev, offsets=(15, 0, 15))
            assert ev.reminder_offsets == [0, 15]
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.reminder_offsets == [0, 15]

    _run(_r())


def test_offsets_persist_through_regenerate(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 11, 18))
            await generate_reminders(s, ev, offsets=(0, 15))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await regenerate_reminders(s, ev)
            await s.commit()
        async with db.get_session() as s:
            reminders = await get_reminders_for_event(s, eid)
            assert sorted(r.remind_at for r in reminders) == [
                date(2026, 11, 3),
                date(2026, 11, 18),
            ]

    _run(_r())


def test_offsets_persist_through_advance(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(
                s,
                name="x",
                next_date=date(2026, 11, 18),
                recurrence=EventRecurrence.YEARLY,
            )
            await generate_reminders(s, ev, offsets=(0, 15))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await advance_event(s, ev, today=date(2027, 1, 1))
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.next_date == date(2027, 11, 18)
            reminders = await get_reminders_for_event(s, eid)
            assert sorted(r.remind_at for r in reminders) == [
                date(2027, 11, 3),
                date(2027, 11, 18),
            ]

    _run(_r())
