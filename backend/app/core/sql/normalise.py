"""SQL normalisation and hashing.

``sql_hash`` is the identity of a query: it answers "which tiles run this
statement?" in one indexed lookup and detects drift between a saved chart and
what a refresh actually executed.
"""

from __future__ import annotations

import hashlib
import re

import sqlglot

_WS = re.compile(r"\s+")

# data_source.dialect (what the catalogue calls it) → sqlglot's name for it
_SQLGLOT_DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "sqlite": "sqlite", "sqlite3": "sqlite"}


def sqlglot_dialect(name: str | None) -> str:
    return _SQLGLOT_DIALECTS.get((name or "").lower(), (name or "sqlite").lower())


def normalise_sql(sql: str, dialect: str = "sqlite") -> str:
    """Canonical form: parsed and re-rendered by sqlglot; falls back to whitespace folding."""
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
        return parsed.sql(dialect=dialect, normalize=True, pretty=False)
    except Exception:
        return _WS.sub(" ", sql).strip().rstrip(";").lower()


def sql_hash(sql: str, dialect: str = "sqlite") -> str:
    return hashlib.sha256(normalise_sql(sql, dialect).encode("utf-8")).hexdigest()
