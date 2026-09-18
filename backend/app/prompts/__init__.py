"""Versioned prompt files.

Each ``*.md`` file under this package starts with a small header::

    ---
    id: agent_system
    version: 1
    ---

The loader computes a content hash at startup; ``prompt_version_id`` is
``"<id>@v<version>#<hash8>"`` and is recorded on every model call, so a
regression is attributable to an exact prompt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_HEADER = re.compile(r"^---\s*\n(?P<meta>.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    version: int
    content_hash: str
    text: str

    @property
    def version_id(self) -> str:
        return f"{self.id}@v{self.version}#{self.content_hash[:8]}"

    def render(self, **kwargs: str) -> str:
        """Substitute ``{{name}}`` placeholders.  Values are *data*: no templating logic runs on them."""
        out = self.text
        for key, value in kwargs.items():
            out = out.replace("{{" + key + "}}", value)
        return out


_registry: dict[str, Prompt] = {}


def load_prompts(directory: Path | None = None) -> dict[str, Prompt]:
    directory = directory or Path(__file__).parent
    _registry.clear()
    for path in sorted(directory.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        m = _HEADER.match(raw)
        if not m:
            raise ValueError(f"prompt {path.name} is missing its header")
        meta = dict(line.split(":", 1) for line in m.group("meta").splitlines() if ":" in line)
        pid = meta["id"].strip()
        version = int(meta.get("version", "1").strip())
        text = raw[m.end() :]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        _registry[pid] = Prompt(id=pid, version=version, content_hash=digest, text=text)
    return dict(_registry)


def get_prompt(prompt_id: str) -> Prompt:
    if not _registry:
        load_prompts()
    try:
        return _registry[prompt_id]
    except KeyError as exc:
        raise KeyError(f"unknown prompt id {prompt_id!r}") from exc


def all_prompts() -> dict[str, Prompt]:
    if not _registry:
        load_prompts()
    return dict(_registry)
