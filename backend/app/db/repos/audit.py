from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.observability.request_context import request_id_var

audit_log = logging.getLogger("lens.audit")


class AuditRepo:
    """Writes an audit row *and* a line on the audit log channel."""

    def __init__(self, session: AsyncSession):
        self.s = session

    async def write(
        self,
        *,
        action: str,
        tenant_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        object_type: str | None = None,
        object_id: uuid.UUID | str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        row = AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id is not None else None,
            request_id=request_id_var.get(),
            ip=ip,
            user_agent=(user_agent or "")[:300] or None,
            metadata_=metadata or {},
        )
        self.s.add(row)
        await self.s.flush()
        audit_log.info(
            action,
            extra={
                "object_type": object_type,
                "object_id": row.object_id,
                "actor": str(actor_user_id),
                **(metadata or {}),
            },
        )
