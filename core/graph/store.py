# -*- coding: utf-8 -*-
"""图谱存储：SQLite 持久化 + 内存倒排索引。

与旧实现的差别
--------------
* 落盘从「整份 graph.json 重写」改成**按行 upsert**（`nodes` / `edges` /
  `entries` 三张表）。
* 向量列存 float32 BLOB：326 条带向量的条目从 42 MB 降到 5 MB。
* `next_id` 不再落盘，改为**装载时按现有最大 id 推导**——
  少一份需要保持一致的状态，就没有"id 发重"的可能。

保留的部分
----------
* 倒排索引仍在**装载时重建、不落盘**：多存一份索引就多一份漂移风险，
  而重建是 O(N) 线性扫描，几千条目是毫秒级。
* 中文分词仍是**字符二元组**（ASCII 按词、CJK 相邻两字成组），不引 `jieba`。
  这是有意的替换，不是等价实现。
"""
from __future__ import annotations

import json
import math
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from ..db import (connection, open_ready, pack_vec, to_f32, transaction,
                  unpack_vec)
from .model import GraphEdge, GraphEntry, GraphNode, canonicalize, now_iso

_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_key        TEXT PRIMARY KEY,
    node_type       TEXT    NOT NULL DEFAULT '',
    value           TEXT    NOT NULL DEFAULT '',
    canonical_value TEXT    NOT NULL DEFAULT '',
    metadata        TEXT    NOT NULL DEFAULT '{}',
    id              INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS edges (
    edge_key         TEXT PRIMARY KEY,
    source_key       TEXT    NOT NULL DEFAULT '',
    target_key       TEXT    NOT NULL DEFAULT '',
    relation_type    TEXT    NOT NULL DEFAULT '',
    source_memory_id INTEGER NOT NULL DEFAULT 0,
    weight           REAL    NOT NULL DEFAULT 1.0,
    confidence       REAL    NOT NULL DEFAULT 0.8,
    status           TEXT    NOT NULL DEFAULT 'active',
    metadata         TEXT    NOT NULL DEFAULT '{}',
    id               INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_key);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_key);
CREATE INDEX IF NOT EXISTS idx_edges_memory ON edges(source_memory_id);
CREATE TABLE IF NOT EXISTS entries (
    entry_key        TEXT PRIMARY KEY,
    entry_type       TEXT    NOT NULL DEFAULT '',
    content          TEXT    NOT NULL DEFAULT '',
    node_keys        TEXT    NOT NULL DEFAULT '[]',
    relation_type    TEXT,
    source_memory_id INTEGER NOT NULL DEFAULT 0,
    session_id       TEXT    NOT NULL DEFAULT '',
    persona_id       TEXT    NOT NULL DEFAULT '',
    metadata         TEXT    NOT NULL DEFAULT '{}',
    edge_id          INTEGER NOT NULL DEFAULT 0,
    vec              BLOB,
    id               INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id);
CREATE INDEX IF NOT EXISTS idx_entries_memory ON entries(source_memory_id);
"""

_NODE_SQL = (
    "INSERT OR REPLACE INTO nodes (node_key, node_type, value, canonical_value,"
    " metadata, id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)")
_EDGE_SQL = (
    "INSERT OR REPLACE INTO edges (edge_key, source_key, target_key,"
    " relation_type, source_memory_id, weight, confidence, status, metadata,"
    " id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
_ENTRY_SQL = (
    "INSERT OR REPLACE INTO entries (entry_key, entry_type, content, node_keys,"
    " relation_type, source_memory_id, session_id, persona_id, metadata,"
    " edge_id, vec, id, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _dumps(value: Any, default: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(default, ensure_ascii=False)


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        got = json.loads(raw)
    except Exception:
        return default
    return got if isinstance(got, type(default)) else default


def tokenize(text: str) -> list[str]:
    """中英混排的轻量切词（ASCII 按词，CJK 按字符二元组）。"""
    if not text:
        return []
    low = text.lower()
    out: list[str] = _ASCII_WORD.findall(low)
    for run in _CJK.findall(low):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


class GraphStore:
    """图谱持久化与检索用的底层结构。

    三张表按行 upsert，但**装载仍然是把全图读进内存**——
    BM25 倒排索引与图遍历都要全图，逐条查库只会更慢。
    """

    def __init__(self, path: Path) -> None:
        p = Path(path)
        # 兼容旧调用点传进来的 `graph.json`：库里落盘名统一是 `.db`
        self.path = p if p.suffix == ".db" else p.with_suffix(".db")
        self._lock = threading.RLock()
        self.nodes: dict[str, GraphNode] = {}        # node_key -> node
        self.edges: dict[str, GraphEdge] = {}        # edge_key -> edge
        self.entries: dict[str, GraphEntry] = {}     # entry_key -> entry
        self.entry_nodes: dict[str, list[str]] = {}  # entry_key -> [node_key]
        self._next_id = 1
        # 加载时重建的派生结构
        self._inv: dict[str, dict[str, int]] = {}    # token -> {entry_key: tf}
        self._doclen: dict[str, int] = {}            # entry_key -> token 数
        self._avg_len = 0.0
        self._ready = open_ready(self.path, _SCHEMA)
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        with self._lock:
            self.nodes = {}
            self.edges = {}
            self.entries = {}
            self.entry_nodes = {}
            self._next_id = 1
            if self._ready:
                try:
                    with connection(self.path) as con:
                        nrows = con.execute("SELECT * FROM nodes").fetchall()
                        erows = con.execute("SELECT * FROM edges").fetchall()
                        trows = con.execute("SELECT * FROM entries").fetchall()
                except Exception:
                    nrows = erows = trows = []
                self._load_nodes(nrows)
                self._load_edges(erows)
                self._load_entries(trows)
            self._rebuild_index()

    def _load_nodes(self, rows) -> None:
        top = 0
        for d in rows:
            n = GraphNode(node_type=d["node_type"], value=d["value"],
                          canonical_value=d["canonical_value"],
                          metadata=_loads(d["metadata"], {}),
                          id=int(d["id"] or 0), created_at=d["created_at"],
                          updated_at=d["updated_at"])
            if n.canonical_value:
                self.nodes[n.node_key] = n
                top = max(top, n.id)
        self._next_id = max(self._next_id, top + 1)

    def _load_edges(self, rows) -> None:
        top = 0
        for d in rows:
            e = GraphEdge(source_key=d["source_key"],
                          target_key=d["target_key"],
                          relation_type=d["relation_type"],
                          source_memory_id=int(d["source_memory_id"] or 0),
                          weight=float(d["weight"] or 1.0),
                          confidence=float(d["confidence"] or 0.8),
                          status=d["status"] or "active",
                          metadata=_loads(d["metadata"], {}),
                          id=int(d["id"] or 0), created_at=d["created_at"],
                          updated_at=d["updated_at"])
            if e.source_key and e.target_key:
                self.edges[e.edge_key] = e
                top = max(top, e.id)
        self._next_id = max(self._next_id, top + 1)

    def _load_entries(self, rows) -> None:
        top = 0
        for d in rows:
            en = GraphEntry(entry_type=d["entry_type"], content=d["content"],
                            node_keys=_loads(d["node_keys"], []),
                            relation_type=d["relation_type"],
                            source_memory_id=int(d["source_memory_id"] or 0),
                            session_id=d["session_id"] or "",
                            persona_id=d["persona_id"] or "",
                            metadata=_loads(d["metadata"], {}),
                            edge_id=int(d["edge_id"] or 0),
                            vec=unpack_vec(d["vec"]),
                            id=int(d["id"] or 0), created_at=d["created_at"],
                            updated_at=d["updated_at"])
            if en.content:
                self.entries[en.entry_key] = en
                self.entry_nodes[en.entry_key] = list(en.node_keys)
                top = max(top, en.id)
        self._next_id = max(self._next_id, top + 1)

    def save(self, *, force: bool = False) -> None:
        """兼容调用点。写入已是即时的，这里不再需要"把整份图重写一遍"。"""

    # ------------------------------------------------------------ 单行写入
    def _write_node(self, n: GraphNode) -> None:
        if not self._ready:
            return
        try:
            with connection(self.path) as con:
                con.execute(_NODE_SQL, (
                    n.node_key, n.node_type, n.value, n.canonical_value,
                    _dumps(n.metadata, {}), n.id, n.created_at, n.updated_at))
        except Exception:
            pass

    def _write_edge(self, e: GraphEdge) -> None:
        if not self._ready:
            return
        try:
            with connection(self.path) as con:
                con.execute(_EDGE_SQL, (
                    e.edge_key, e.source_key, e.target_key, e.relation_type,
                    e.source_memory_id, float(e.weight), float(e.confidence),
                    e.status, _dumps(e.metadata, {}), e.id, e.created_at,
                    e.updated_at))
        except Exception:
            pass

    def _write_entry(self, en: GraphEntry) -> None:
        if not self._ready:
            return
        try:
            with connection(self.path) as con:
                con.execute(_ENTRY_SQL, (
                    en.entry_key, en.entry_type, en.content,
                    _dumps(list(en.node_keys), []), en.relation_type,
                    en.source_memory_id, en.session_id, en.persona_id,
                    _dumps(en.metadata, {}), en.edge_id, pack_vec(en.vec),
                    en.id, en.created_at, en.updated_at))
        except Exception:
            pass

    def save_entries(self, entries: Iterable[GraphEntry]) -> None:
        """回写若干条目（用于"upsert 之后才拿到向量"的补写）。

        写入是即时的，所以任何在 `upsert_entry` 之后才改到条目上的字段
        都必须显式回写一次，否则只活在内存里。
        """
        todo = [e for e in entries if e.entry_key in self.entries]
        if not todo or not self._ready:
            return
        try:
            with transaction(self.path) as con:
                con.executemany(_ENTRY_SQL, [(
                    en.entry_key, en.entry_type, en.content,
                    _dumps(list(en.node_keys), []), en.relation_type,
                    en.source_memory_id, en.session_id, en.persona_id,
                    _dumps(en.metadata, {}), en.edge_id, pack_vec(en.vec),
                    en.id, en.created_at, en.updated_at) for en in todo])
        except Exception:
            pass

    # ------------------------------------------------------------ 倒排索引
    def _rebuild_index(self) -> None:
        inv: dict[str, dict[str, int]] = defaultdict(dict)
        doclen: dict[str, int] = {}
        for key, entry in self.entries.items():
            toks = tokenize(entry.content)
            doclen[key] = len(toks)
            tf: dict[str, int] = defaultdict(int)
            for t in toks:
                tf[t] += 1
            for t, c in tf.items():
                inv[t][key] = c
        self._inv = dict(inv)
        self._doclen = doclen
        self._avg_len = (sum(doclen.values()) / len(doclen)) if doclen else 0.0

    # ------------------------------------------------------------ 写入
    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def upsert_node(self, node: GraphNode) -> GraphNode:
        with self._lock:
            key = node.node_key
            cur = self.nodes.get(key)
            if cur is not None:
                # 已有节点：合并 metadata、刷新时间（不新增）
                if node.metadata:
                    merged = dict(cur.metadata)
                    merged.update(node.metadata)
                    cur.metadata = merged
                cur.updated_at = now_iso()
                # 必须回写：合并后的 metadata 只活在内存里就等于丢了
                self._write_node(cur)
                return cur
            node.id = node.id or self._new_id()
            # 迁移会带外部 id 进来，`_next_id` 必须跟上，否则后发的 id 会撞车
            self._next_id = max(self._next_id, node.id + 1)
            self.nodes[key] = node
            self._write_node(node)
            return node

    def upsert_edge(self, edge: GraphEdge) -> GraphEdge:
        with self._lock:
            key = edge.edge_key
            cur = self.edges.get(key)
            if cur is not None:
                cur.weight = max(cur.weight, edge.weight)
                cur.confidence = max(cur.confidence, edge.confidence)
                cur.updated_at = now_iso()
                self._write_edge(cur)
                return cur
            edge.id = edge.id or self._new_id()
            self._next_id = max(self._next_id, edge.id + 1)
            self.edges[key] = edge
            self._write_edge(edge)
            return edge

    def upsert_entry(self, entry: GraphEntry) -> tuple[GraphEntry, bool]:
        """写入条目。返回 (条目, 是否新建)。"""
        with self._lock:
            key = entry.entry_key
            cur = self.entries.get(key)
            if cur is not None:
                if len(entry.vec or ()) and not len(cur.vec or ()):
                    cur.vec = to_f32(entry.vec)
                cur.metadata.setdefault("hits", 0)
                cur.updated_at = now_iso()
                self._write_entry(cur)
                return cur, False
            entry.id = entry.id or self._new_id()
            self._next_id = max(self._next_id, entry.id + 1)
            for nk in entry.node_keys:
                if nk not in self.nodes:
                    ntype, _, cval = nk.partition(":")
                    self.nodes[nk] = GraphNode(node_type=ntype,
                                               value=cval or nk,
                                               canonical_value=cval or nk,
                                               id=self._new_id())
                    self._write_node(self.nodes[nk])
            self.entries[key] = entry
            self.entry_nodes[key] = list(entry.node_keys)
            self._write_entry(entry)
            # 索引增量更新
            for t, c in _tf(tokenize(entry.content)).items():
                self._inv.setdefault(t, {})[key] = c
            self._doclen[key] = len(tokenize(entry.content))
            n = len(self._doclen)
            self._avg_len = ((self._avg_len * (n - 1)) + self._doclen[key]) / n if n else 0.0
            return entry, True

    def forget_memory(self, source_memory_id: int) -> int:
        """删除某条记忆派生的全部条目/边（记忆被遗忘时用）。"""
        with self._lock:
            gone = [k for k, e in self.entries.items()
                    if e.source_memory_id == source_memory_id]
            for k in gone:
                self.entries.pop(k, None)
                self.entry_nodes.pop(k, None)
                self._doclen.pop(k, None)
            for t in list(self._inv):
                for k in gone:
                    self._inv[t].pop(k, None)
                if not self._inv[t]:
                    self._inv.pop(t, None)
            gone_edges = [k for k, e in self.edges.items()
                          if e.source_memory_id == source_memory_id]
            for k in gone_edges:
                self.edges.pop(k, None)
            # 清理不再被任何条目引用的节点
            used = {nk for nks in self.entry_nodes.values() for nk in nks}
            orphans = [k for k in self.nodes if k not in used]
            for k in orphans:
                self.nodes.pop(k, None)
            self._avg_len = (sum(self._doclen.values()) / len(self._doclen)
                             ) if self._doclen else 0.0
            if (gone or gone_edges or orphans) and self._ready:
                try:
                    with transaction(self.path) as con:
                        con.executemany("DELETE FROM entries WHERE entry_key=?",
                                        [(k,) for k in gone])
                        con.executemany("DELETE FROM edges WHERE edge_key=?",
                                        [(k,) for k in gone_edges])
                        con.executemany("DELETE FROM nodes WHERE node_key=?",
                                        [(k,) for k in orphans])
                except Exception:
                    pass
            return len(gone)

    def forget_session(self, session_id: str) -> int:
        """删除某个记忆键派生出的全部条目/边/节点（记忆被遗忘时用）。

        为什么需要它 —— 按 `source_memory_id` 联动删除的兜底：
        历史数据里，图谱条目与其派生原子的 `source_memory_id` 并不总是对得上
        （旧实现两路脱节：某会话的 55 条图条目挂 `10005`，而它的 462 条
        原子的 mid 集合里根本没有这个值）。这时 `forget_memory(mid)` 永远
        匹配不到 —— 记忆删了、图条目残留，图谱路照样召回，
        用户会看到"删了记忆 AI 还记得"。

        固化时是用同一个 key 同时写原子和条目的 `session_id`，
        所以 `session_id` 是历史数据上也成立、更可靠的关联。
        mid 是全局唯一的一次话编号，顺带一起删（跨会话复用/去重都覆盖到）。
        """
        with self._lock:
            keys = [k for k, e in self.entries.items()
                    if e.session_id == session_id]
            if not keys:
                return 0
            mids = {e.source_memory_id for k, e in self.entries.items()
                    if k in keys and e.source_memory_id}
            total = sum(self.forget_memory(m) for m in mids)
            # 兜底：mid 为 0 / 无 mid 的条目（forget_memory 匹配不到的那批）
            left = [k for k, e in self.entries.items()
                    if e.session_id == session_id]
            if left:
                for k in left:
                    self.entries.pop(k, None)
                    self.entry_nodes.pop(k, None)
                    self._doclen.pop(k, None)
                for t in list(self._inv):
                    for k in left:
                        self._inv[t].pop(k, None)
                    if not self._inv[t]:
                        self._inv.pop(t, None)
                used = {nk for nks in self.entry_nodes.values() for nk in nks}
                orphans = [k for k in self.nodes if k not in used]
                for k in orphans:
                    self.nodes.pop(k, None)
                self._avg_len = (
                    sum(self._doclen.values()) / len(self._doclen)
                ) if self._doclen else 0.0
                if self._ready:
                    try:
                        with transaction(self.path) as con:
                            con.executemany("DELETE FROM entries WHERE entry_key=?",
                                            [(k,) for k in left])
                            con.executemany("DELETE FROM nodes WHERE node_key=?",
                                            [(k,) for k in orphans])
                    except Exception:
                        pass
                total += len(left)
            return total

    # ------------------------------------------------------------ BM25
    def bm25_search(self, query: str, k: int = 10, *,
                    session_id: str | None = None,
                    persona_id: str | None = None,
                    strict_persona_id: str | None = None,
                    k1: float = 1.5, b: float = 0.75) -> list[tuple[GraphEntry, float]]:
        """对图谱条目做 BM25 关键词检索。

        `persona_id`：旧语义硬过滤（只丢弃「明确是别人格」的，空 persona 保留）。
        `strict_persona_id`：严格隔离，只保留 `persona_id == 该值` 的条目。
        """
        toks = tokenize(query)
        if not toks or not self.entries:
            return []
        n_docs = len(self.entries)
        avg = self._avg_len or 1.0
        scores: dict[str, float] = defaultdict(float)
        for t in set(toks):
            posting = self._inv.get(t)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            for key, tf in posting.items():
                dl = self._doclen.get(key, 1) or 1
                denom = tf + k1 * (1 - b + b * dl / avg)
                scores[key] += idf * (tf * (k1 + 1)) / denom
        out: list[tuple[GraphEntry, float]] = []
        for key, s in scores.items():
            e = self.entries.get(key)
            if e is None:
                continue
            if session_id and e.session_id and e.session_id != session_id:
                continue
            if strict_persona_id:
                if e.persona_id != strict_persona_id:
                    continue
            elif persona_id and e.persona_id and e.persona_id != persona_id:
                continue
            out.append((e, s))
        out.sort(key=lambda kv: -kv[1])
        return out[:k]

    # ------------------------------------------------------------ 图遍历
    def neighbors(self, node_key: str, *, relation: str | None = None,
                  limit: int = 20) -> list[tuple[str, str, float]]:
        """返回 (邻居 node_key, relation_type, confidence)。"""
        out: list[tuple[str, str, float]] = []
        for e in self.edges.values():
            if e.status != "active":
                continue
            if relation and e.relation_type != relation:
                continue
            if e.source_key == node_key:
                out.append((e.target_key, e.relation_type, e.confidence))
            elif e.target_key == node_key:
                out.append((e.source_key, e.relation_type, e.confidence))
            if len(out) >= limit:
                break
        return out

    def entries_for_node(self, node_key: str, limit: int = 20) -> list[GraphEntry]:
        out = []
        for key, nks in self.entry_nodes.items():
            if node_key in nks:
                e = self.entries.get(key)
                if e is not None:
                    out.append(e)
                if len(out) >= limit:
                    break
        return out

    def expand(self, node_keys: Iterable[str], *, hops: int = 1,
               limit: int = 30) -> set[str]:
        """从若干节点出发做 N 跳扩展，返回邻域内所有节点 key。

        用于回答"谁和谁什么关系"这类问题 —— 单点命中的条目不够，
        需要沿边把相关实体一起带出来。
        """
        frontier = set(node_keys)
        seen = set(frontier)
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for nk in frontier:
                for nb, _rel, _c in self.neighbors(nk, limit=limit):
                    if nb not in seen:
                        nxt.add(nb)
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        return seen

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            by_type[n.node_type] += 1
        rel: dict[str, int] = defaultdict(int)
        for e in self.edges.values():
            rel[e.relation_type] += 1
        size = 0
        for extra in ("", "-wal"):
            try:
                size += Path(str(self.path) + extra).stat().st_size
            except OSError:
                pass
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "entries": len(self.entries),
            "entry_links": sum(len(v) for v in self.entry_nodes.values()),
            "tokens": len(self._inv),
            "node_types": dict(by_type),
            "relation_types": dict(rel),
            "bytes": size,
        }


def _tf(tokens: list[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for t in tokens:
        out[t] += 1
    return dict(out)
