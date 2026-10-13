"""Load settings.yaml and sources.yaml."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SourceSpec:
    id: str
    name: str
    kind: str
    roles: list[str]
    lineage: str
    trust: str = "B"
    agency: str = ""
    url: str | None = None
    geometry: str | None = None
    where: str | None = None
    fields: dict[str, list[str]] = field(default_factory=dict)
    datetime_tz: str = "UTC"
    date_only: bool = False
    calendar_dates: bool = False
    enabled: bool = True
    consensus: bool = True
    path_key: str | None = None
    layer: str | None = None
    filter: dict[str, Any] = field(default_factory=dict)
    lineage_from_field: dict[str, Any] | None = None
    cause_codes: str | None = None
    ignition_kind: str | None = None
    coverage: str | None = None
    notes: str | None = None
    verified: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path

    def path(self, *keys: str) -> Path | None:
        node: Any = self.raw.get("paths", {})
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return None
            node = node[k]
        if not node:
            return None
        p = Path(str(node))
        return p if p.is_absolute() else self.root / p

    def local_path(self, key: str) -> Path | None:
        return self.path("local", key)

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.raw.get(section, {}).get(key, default)

    @property
    def cache_dir(self) -> Path:
        return self.path("cache_dir") or self.root / "cache"

    @property
    def out_dir(self) -> Path:
        return self.path("out_dir") or self.root / "out"


_SPEC_KEYS = set(SourceSpec.__dataclass_fields__) - {"id", "raw"}


def load_sources(path: Path | None = None) -> tuple[dict[str, SourceSpec], dict[str, Any]]:
    path = path or PROJECT_ROOT / "config" / "sources.yaml"
    doc = yaml.safe_load(path.read_text())
    defaults = doc.get("defaults", {})
    specs: dict[str, SourceSpec] = {}
    for sid, entry in (doc.get("sources") or {}).items():
        kwargs = {k: copy.deepcopy(v) for k, v in entry.items() if k in _SPEC_KEYS}
        kwargs.setdefault("roles", [])
        kwargs.setdefault("lineage", sid.upper())
        kwargs.setdefault("name", sid)
        specs[sid] = SourceSpec(id=sid, raw=entry, **kwargs)
    return specs, defaults


def load_settings(path: Path | None = None, root: Path | None = None) -> Settings:
    path = path or PROJECT_ROOT / "config" / "settings.yaml"
    return Settings(raw=yaml.safe_load(path.read_text()) or {}, root=root or PROJECT_ROOT)
