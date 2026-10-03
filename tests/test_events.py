"""Domain operation tests for Events."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

import app.db as db
from app.events import (
    advance_due_events,
    advance_event,
    create_event,
    delete_event,
    get_event,
    list_events,
    update_event,
)
from app.models import EventRecurrence


def _run(coro) -> None:
    asyncio.run(coro)


def test_create_and_get(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(
                s,
                name="  День рождения Маши  ",
                next_date=date(2026, 11, 18),
                recurrence=EventRecurrence.YEARLY,
            )
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            got = await get_event(s, eid)
            assert got is not None
            assert got.name == "День рождения Маши"
            assert got.next_date == date(2026, 11, 18)
            assert got.recurrence is EventRecurrence.YEARLY
            assert got.anchor_date == date(2026, 11, 18)
            assert got.is_active is True

    _run(_r())


def test_create_none_recurrence_has_no_anchor(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 1, 1))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            got = await get_event(s, eid)
            assert got.recurrence is EventRecurrence.NONE
            assert got.anchor_date is None

    _run(_r())


def test_create_rejects_empty_name(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            for bad in ("", "   "):
                with pytest.raises(ValueError):
                    await create_event(s, name=bad, next_date=date(2026, 1, 1))

    _run(_r())


def test_create_rejects_invalid_recurrence(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            with pytest.raises(ValueError):
                await create_event(
                    s, name="x", next_date=date(2026, 1, 1), recurrence="weekly"
                )

    _run(_r())


def test_get_missing_returns_none(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            assert await get_event(s, 123_456) is None

    _run(_r())


def test_list_active_sorted(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            await create_event(s, name="B", next_date=date(2026, 5, 1))
            await create_event(s, name="A", next_date=date(2026, 3, 1))
            await create_event(
                s, name="C", next_date=date(2026, 7, 1), is_active=False
            )
            await s.commit()

            active = await list_events(s)
            assert [e.name for e in active] == ["A", "B"]

            all_events = await list_events(s, active_only=False)
            assert [e.name for e in all_events] == ["A", "B", "C"]

    _run(_r())


def test_update_fields_and_anchor(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="Old", next_date=date(2026, 11, 18))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await update_event(
                s,
                ev,
                name="New",
                next_date=date(2026, 12, 20),
                recurrence=EventRecurrence.YEARLY,
                action_text="hello",
                is_active=False,
            )
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.name == "New"
            assert ev.next_date == date(2026, 12, 20)
            assert ev.recurrence is EventRecurrence.YEARLY
            assert ev.anchor_date == date(2026, 12, 20)
            assert ev.action_text == "hello"
            assert ev.is_active is False

    _run(_r())


def test_update_to_none_clears_anchor(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(
                s, name="x", next_date=date(2026, 1, 1), recurrence=EventRecurrence.YEARLY
            )
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await update_event(s, ev, recurrence=EventRecurrence.NONE)
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.recurrence is EventRecurrence.NONE
            assert ev.anchor_date is None

    _run(_r())


def test_update_next_date_reanchors_yearly(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(
                s,
                name="x",
                next_date=date(2026, 11, 18),
                recurrence=EventRecurrence.YEARLY,
            )
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await update_event(s, ev, next_date=date(2026, 12, 25))
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.next_date == date(2026, 12, 25)
            assert ev.anchor_date == date(2026, 12, 25)
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await advance_event(s, ev, today=date(2027, 1, 1))
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.next_date == date(2027, 12, 25)
            assert ev.anchor_date == date(2026, 12, 25)

    _run(_r())


def test_delete_event(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2026, 1, 1))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await delete_event(s, ev)
            await s.commit()
        async with db.get_session() as s:
            assert await get_event(s, eid) is None

    _run(_r())


def test_advance_event_overdue_yearly(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(
                s, name="x", next_date=date(2024, 11, 18), recurrence=EventRecurrence.YEARLY
            )
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await advance_event(s, ev, today=date(2026, 9, 27))
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.next_date == date(2026, 11, 18)
            assert ev.anchor_date == date(2024, 11, 18)

    _run(_r())


def test_advance_event_none_untouched(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(s, name="x", next_date=date(2020, 1, 1))
            await s.commit()
            eid = ev.id
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            await advance_event(s, ev, today=date(2026, 9, 27))
            await s.commit()
        async with db.get_session() as s:
            ev = await get_event(s, eid)
            assert ev.next_date == date(2020, 1, 1)

    _run(_r())


def test_advance_due_events_bulk(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            await create_event(
                s, name="yearly", next_date=date(2024, 11, 18),
                recurrence=EventRecurrence.YEARLY,
            )
            await create_event(
                s, name="future", next_date=date(2030, 1, 1),
                recurrence=EventRecurrence.YEARLY,
            )
            await create_event(s, name="none", next_date=date(2020, 1, 1))
            await s.commit()
        async with db.get_session() as s:
            advanced = await advance_due_events(s, today=date(2026, 9, 27))
            await s.commit()
            assert sorted(e.name for e in advanced) == ["yearly"]
        async with db.get_session() as s:
            all_events = {e.name: e for e in await list_events(s, active_only=False)}
            assert all_events["yearly"].next_date == date(2026, 11, 18)
            assert all_events["future"].next_date == date(2030, 1, 1)
            assert all_events["none"].next_date == date(2020, 1, 1)

    _run(_r())


def test_leap_year_event_returns_to_feb29(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            ev = await create_event(
                s, name="leap", next_date=date(2024, 2, 29),
                recurrence=EventRecurrence.YEARLY,
            )
            await s.commit()
            eid = ev.id

        for today, expected in [
            (date(2024, 3, 1), date(2025, 2, 28)),
            (date(2025, 3, 1), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2027, 2, 28)),
            (date(2027, 3, 1), date(2028, 2, 29)),
        ]:
            async with db.get_session() as s:
                ev = await get_event(s, eid)
                await advance_event(s, ev, today=today)
                await s.commit()
            async with db.get_session() as s:
                ev = await get_event(s, eid)
                assert ev.next_date == expected
                assert ev.anchor_date == date(2024, 2, 29)

    _run(_r())
