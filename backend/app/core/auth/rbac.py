"""Role model.

Global role is a ceiling; per-dashboard grants are the floor.  The effective
role on a dashboard is ``min(ceiling(global_role), grant)``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.db.models.base import DashboardRole, UserRole

_USER_RANK = {UserRole.viewer: 0, UserRole.analyst: 1, UserRole.admin: 2}
_DASH_RANK = {DashboardRole.viewer: 0, DashboardRole.editor: 1, DashboardRole.owner: 2}


@dataclass(frozen=True, slots=True)
class Principal:
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: UserRole
    full_name: str | None = None
    scopes: dict[str, list] = field(default_factory=dict, hash=False, compare=False)

    def at_least(self, role: UserRole) -> bool:
        return _USER_RANK[self.role] >= _USER_RANK[role]


def user_role_at_least(actual: UserRole, minimum: UserRole) -> bool:
    return _USER_RANK[actual] >= _USER_RANK[minimum]


def dashboard_role_at_least(actual: DashboardRole, minimum: DashboardRole) -> bool:
    return _DASH_RANK[actual] >= _DASH_RANK[minimum]


def dashboard_ceiling(role: UserRole) -> DashboardRole:
    """A global viewer can never edit, even with an editor grant."""
    if role is UserRole.viewer:
        return DashboardRole.viewer
    return DashboardRole.owner


def effective_dashboard_role(global_role: UserRole, grant: DashboardRole | None) -> DashboardRole | None:
    if grant is None:
        return None
    ceiling = dashboard_ceiling(global_role)
    return grant if _DASH_RANK[grant] <= _DASH_RANK[ceiling] else ceiling
