from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ChatSession,
    Feedback,
    GuardEvent,
    GuardKind,
    GuardVerdict,
    Turn,
    TurnChart,
    TurnStatus,
    UsageCounter,
)


class ChatRepo:
    def __init__(self, session: AsyncSession):
        self.s = session

    # ── sessions ─────────────────────────────────────────────────────────────
    async def create_session(self, *, user_id: uuid.UUID, data_source_id: uuid.UUID, title: str | None) -> ChatSession:
        row = ChatSession(user_id=user_id, data_source_id=data_source_id, title=title)
        self.s.add(row)
        await self.s.flush()
        return row

    async def get_session(self, session_id: uuid.UUID, *, user_id: uuid.UUID) -> ChatSession | None:
        row = await self.s.get(ChatSession, session_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    async def list_sessions(self, user_id: uuid.UUID, limit: int = 50) -> list[ChatSession]:
        q = (
            select(ChatSession)
            .where(ChatSession.user_id == user_id)
            .order_by(ChatSession.last_active_at.desc())
            .limit(limit)
        )
        return list((await self.s.execute(q)).scalars())

    async def touch_session(self, session: ChatSession, *, title: str | None = None) -> None:
        session.last_active_at = datetime.now(UTC)
        if title and not session.title:
            session.title = title
        await self.s.flush()

    # ── turns ────────────────────────────────────────────────────────────────
    async def list_turns(self, session_id: uuid.UUID) -> list[Turn]:
        q = (
            select(Turn)
            .where(Turn.session_id == session_id)
            .options(selectinload(Turn.charts))
            .order_by(Turn.created_at)
        )
        return list((await self.s.execute(q)).scalars())

    async def recent_turns(self, session_id: uuid.UUID, limit: int = 6) -> list[Turn]:
        q = (
            select(Turn)
            .where(Turn.session_id == session_id, Turn.status == TurnStatus.ok)
            .order_by(Turn.created_at.desc())
            .limit(limit)
        )
        return list(reversed(list((await self.s.execute(q)).scalars())))

    async def get_turn(self, turn_id: uuid.UUID, *, user_id: uuid.UUID) -> Turn | None:
        row = await self.s.get(Turn, turn_id, options=[selectinload(Turn.charts)])
        if row is None or row.user_id != user_id:
            return None
        return row

    async def create_turn(self, **fields: Any) -> Turn:
        row = Turn(**fields)
        self.s.add(row)
        await self.s.flush()
        return row

    async def add_turn_chart(
        self,
        turn: Turn,
        *,
        position: int,
        title: str,
        chart_spec: dict,
        sql_text: str,
        sql_hash: str,
        render_code: str | None,
        dataset_name: str,
    ) -> TurnChart:
        row = TurnChart(
            turn_id=turn.id,
            position=position,
            title=title,
            chart_spec=chart_spec,
            sql_text=sql_text,
            sql_hash=sql_hash,
            render_code=render_code,
            dataset_name=dataset_name,
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def add_guard_event(
        self,
        *,
        turn_id: uuid.UUID | None,
        tile_refresh_id: uuid.UUID | None,
        kind: GuardKind,
        verdict: GuardVerdict,
        reason: str,
        offending_sql: str | None,
        shadow: str | None,
    ) -> GuardEvent:
        row = GuardEvent(
            turn_id=turn_id,
            tile_refresh_id=tile_refresh_id,
            kind=kind,
            verdict=verdict,
            reason=reason[:2000],
            offending_sql=offending_sql,
            shadow_parser_verdict=shadow,
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def guard_events_for_turn(self, turn_id: uuid.UUID) -> list[GuardEvent]:
        q = select(GuardEvent).where(GuardEvent.turn_id == turn_id).order_by(GuardEvent.created_at)
        return list((await self.s.execute(q)).scalars())

    async def add_feedback(
        self, *, turn_id: uuid.UUID, user_id: uuid.UUID, rating: int, comment: str | None
    ) -> Feedback:
        row = Feedback(turn_id=turn_id, user_id=user_id, rating=rating, comment=comment)
        self.s.add(row)
        await self.s.flush()
        return row

    # ── usage ────────────────────────────────────────────────────────────────
    async def usage_today(self, user_id: uuid.UUID) -> UsageCounter | None:
        return await self.s.get(UsageCounter, (user_id, date.today()))

    async def add_usage(
        self, user_id: uuid.UUID, *, turns: int, input_tokens: int, output_tokens: int, cost_usd: float
    ) -> None:
        stmt = pg_insert(UsageCounter).values(
            user_id=user_id,
            day=date.today(),
            turn_count=turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UsageCounter.user_id, UsageCounter.day],
            set_={
                "turn_count": UsageCounter.turn_count + turns,
                "input_tokens": UsageCounter.input_tokens + input_tokens,
                "output_tokens": UsageCounter.output_tokens + output_tokens,
                "cost_usd": UsageCounter.cost_usd + cost_usd,
            },
        )
        await self.s.execute(stmt)

    async def usage_rows(self, *, user_id: uuid.UUID | None, days: int) -> list[UsageCounter]:
        q = select(UsageCounter).where(UsageCounter.day >= func.current_date() - days)
        if user_id is not None:
            q = q.where(UsageCounter.user_id == user_id)
        return list((await self.s.execute(q.order_by(UsageCounter.day.desc()))).scalars())
