# -*- coding: utf-8 -*-
"""图谱数据模型（对齐旧 livingmemory 的四表结构）。

旧库的表（实测自干净副本 `lm_probe`）：

| 表 | 行数 | 含义 |
|---|---|---|
| `graph_nodes` | 15,698 | 实体节点：`node_key = "<type>:<canonical_value>"` |
| `graph_edges` | 98,591 | 有类型的关系：`edge_key = "<src>|<rel>|<dst>|<memory_id>"` |
| `graph_entries` | 134,299 | 可检索的图谱条目（fact / topic），带 session/persona |
| `graph_entry_nodes` | 245,031 | 条目 ↔ 节点的多对多 |

节点类型实测有 `topic` / `person` / `fact`；关系类型实测有 `describes`
（topic describes fact）等。这里保持同一套键格式，语义才好对齐。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------- 规范化

_EDGE_PUNCT = re.compile(
    r"^[\s,.;:!?'\"，。；：！？、（）()\[\]{}<>《》]+"
    r"|[\s,.;:!?'\"，。；：！？、（）()\[\]{}<>《》]+$")
_WS = re.compile(r"\s+")


def canonicalize(value: str) -> str:
    """实体名归一：去首尾标点、折叠空白、纯 ASCII 转小写。

    与旧 `EntityResolver.canonicalize` 逐字一致 —— 归一是去重的前提，
    改了它同一实体会被存成两个节点。
    """
    if not value:
        return ""
    v = _EDGE_PUNCT.sub("", str(value).strip())
    v = _WS.sub(" ", v)
    if v.isascii():
        v = v.lower()
    return v


def dedupe(values: list[str]) -> list[str]:
    """按归一化结果去重，保留原顺序与原写法。"""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        c = canonicalize(v)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(str(v).strip())
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 节点

@dataclass
class GraphNode:
    node_type: str
    value: str
    canonical_value: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.canonical_value:
            self.canonical_value = canonicalize(self.value)
        if not self.created_at:
            self.created_at = now_iso()
        self.updated_at = self.updated_at or self.created_at

    @property
    def node_key(self) -> str:
        return f"{self.node_type}:{self.canonical_value}"


# ---------------------------------------------------------------- 边

@dataclass
class GraphEdge:
    source_key: str
    target_key: str
    relation_type: str
    source_memory_id: int = 0
    weight: float = 1.0
    confidence: float = 0.8
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now_iso()
        self.updated_at = self.updated_at or self.created_at

    @property
    def edge_key(self) -> str:
        return (f"{self.source_key}|{self.relation_type}|"
                f"{self.target_key}|{self.source_memory_id}")


# ---------------------------------------------------------------- 条目

@dataclass
class GraphEntry:
    entry_type: str
    content: str
    node_keys: list[str] = field(default_factory=list)
    relation_type: str | None = None
    source_memory_id: int = 0
    session_id: str = ""
    persona_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    edge_id: int = 0
    vec: list[float] = field(default_factory=list)
    id: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now_iso()
        self.updated_at = self.updated_at or self.created_at

    @property
    def entry_key(self) -> str:
        payload = (f"{self.entry_type}|{self.source_memory_id}|"
                   f"{self.relation_type or ''}|"
                   f"{'|'.join(self.node_keys)}|{self.content}")
        return sha1(payload)

    @property
    def importance(self) -> float:
        try:
            return float(self.metadata.get("importance", 0.5))
        except (TypeError, ValueError):
            return 0.5

    @property
    def graph_confidence(self) -> float:
        try:
            return float(self.metadata.get("graph_confidence", 0.7))
        except (TypeError, ValueError):
            return 0.7

    @property
    def created_ts(self) -> float:
        for key in ("create_time", "last_access_time"):
            v = self.metadata.get(key)
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
        try:
            return datetime.fromisoformat(self.created_at).timestamp()
        except Exception:
            return time.time()


# ---------------------------------------------------------------- 抽取结果

@dataclass
class ExtractedGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    entries: list[GraphEntry] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.nodes and not self.entries
