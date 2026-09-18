"""The allow-list and the schema context handed to the model.

Only enabled views reach the model; columns flagged ``sensitive`` are excluded
from the context entirely and the guard refuses references to them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DataSource, DataSourceView


@dataclass(frozen=True, slots=True)
class ViewMeta:
    name: str
    description: str
    business_rules: str | None
    scope_column: str | None
    columns: tuple[tuple[str, str, str], ...]  # (name, type, description) — non-sensitive only
    sensitive_columns: frozenset[str]
    allow_row_samples: bool = False


@dataclass(slots=True)
class AllowList:
    data_source_id: uuid.UUID
    dialect: str
    views: dict[str, ViewMeta] = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        return set(self.views)

    @property
    def scope_columns(self) -> dict[str, str]:
        return {n: v.scope_column for n, v in self.views.items() if v.scope_column}

    @property
    def sensitive_columns(self) -> dict[str, frozenset[str]]:
        return {n: v.sensitive_columns for n, v in self.views.items() if v.sensitive_columns}

    @property
    def scoped_view_names(self) -> list[str]:
        return sorted(self.scope_columns)


async def load_allow_list(session: AsyncSession, data_source_id: uuid.UUID) -> AllowList:
    source = await session.get(DataSource, data_source_id)
    if source is None or not source.is_active:
        raise ValueError("data source unavailable")
    rows = (
        await session.execute(
            select(DataSourceView).where(
                DataSourceView.data_source_id == data_source_id, DataSourceView.is_enabled.is_(True)
            )
        )
    ).scalars()
    allow = AllowList(data_source_id=data_source_id, dialect=source.dialect)
    for row in rows:
        cols = []
        sensitive = set()
        for c in row.column_metadata or []:
            if c.get("sensitive"):
                sensitive.add(str(c["name"]).lower())
            else:
                cols.append((str(c["name"]), str(c.get("type", "")), str(c.get("description", ""))))
        allow.views[row.view_name.lower()] = ViewMeta(
            name=row.view_name,
            description=row.description,
            business_rules=row.business_rules,
            scope_column=row.scope_column,
            columns=tuple(cols),
            sensitive_columns=frozenset(sensitive),
            allow_row_samples=row.allow_row_samples,
        )
    return allow


def render_schema_context(allow: AllowList) -> str:
    """Plain-text schema block for the prompt.  Everything here is *data* from the allow-list."""
    parts = []
    for name in sorted(allow.views):
        v = allow.views[name]
        parts.append(f"VIEW {v.name}")
        parts.append(f"  purpose: {v.description}")
        if v.scope_column:
            parts.append(f"  scope column: {v.scope_column}")
        if v.sensitive_columns:
            parts.append("  note: SELECT * is not permitted on this view; list the columns you need explicitly.")
        parts.append("  columns:")
        for cname, ctype, cdesc in v.columns:
            parts.append(f"    - {cname} ({ctype}): {cdesc}")
    return "\n".join(parts)


def render_business_rules(allow: AllowList) -> str:
    rules = [f"- {v.name}: {v.business_rules}" for v in allow.views.values() if v.business_rules]
    return "\n".join(rules) if rules else "- (none)"
