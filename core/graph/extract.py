# -*- coding: utf-8 -*-
"""图谱抽取：把一条记忆的元数据投影成节点 / 边 / 可检索条目。

**这一步不需要 LLM** —— 它是**确定性投影**。旧实现也是如此：
LLM 在上游负责把对话窗口总结成
`{summary, topics, participants, key_facts}`，抽取只是按固定规则铺开。
所以这一段是逐条对齐的（含 confidence 常数与 content 模板），
不是"我理解的等价物"。

产出的六类条目
--------------
| entry_type | 模板 | 关联节点 | confidence |
|---|---|---|---|
| `fact` | `Fact: {v}. Summary: {s}` | fact | 0.9 |
| `topic` | `Topic: {v}. Summary: {s}` | topic | 0.75 |
| `participant` | `Participant: {v}. Summary: {s}` | person | 0.7 |
| `edge` | `Topic {t} describes fact {f}. Summary: {s}` | topic+fact | 0.82 |
| `edge` | `Participant {p} is linked to fact {f}. Summary: {s}` | person+fact | 0.88 |
| `edge` | `Participant {a} co-occurs with participant {b}. Summary: {s}` | person+person | 0.7 |

边只有三种关系：`describes`（topic→fact）、`mentioned_in`（person→fact）、
`co_occurs_with`（person↔person）。

**条目正文里都带 Summary** —— 这是设计要点：单看 "Fact: A 画了图" 信息不足，
带上整段摘要才能在检索命中后给模型足够上下文。
"""
from __future__ import annotations

from typing import Any

from .model import (ExtractedGraph, GraphEdge, GraphEntry, GraphNode,
                    canonicalize, dedupe)

DEFAULTS = {
    "graph_max_topics": 6,
    "graph_max_participants": 8,
    "graph_max_facts": 8,
}


class GraphExtractor:
    def __init__(self, config: Any = None) -> None:
        self.config = config or {}

    def _cfg(self, key: str) -> int:
        try:
            v = self.config.get(key, DEFAULTS[key])
        except Exception:
            v = DEFAULTS[key]
        try:
            return int(v)
        except (TypeError, ValueError):
            return DEFAULTS[key]

    # ------------------------------------------------------------ 参与者
    def _participants(self, metadata: dict) -> list[tuple[str, str, dict]]:
        """返回 (显示名, canonical, 额外元数据)。

        优先用 `participant_identities`（带 QQ 号等稳定身份），
        退回 `participants`（纯名字列表）。
        """
        out: list[tuple[str, str, dict]] = []
        maxp = self._cfg("graph_max_participants")

        identities = metadata.get("participant_identities")
        if isinstance(identities, list):
            seen: set[str] = set()
            for item in identities:
                if not isinstance(item, dict):
                    continue
                ik = canonicalize(str(item.get("identity_key") or ""))
                name = str(item.get("display_name")
                           or item.get("sender_id") or "").strip()
                if not ik or not name or ik in seen:
                    continue
                seen.add(ik)
                aliases = dedupe([str(a) for a in (item.get("aliases") or []) if a])
                out.append((name, f"account:{ik}", {
                    "identity_key": ik,
                    "sender_id": str(item.get("sender_id") or ""),
                    "platform": str(item.get("platform") or "unknown"),
                    "aliases": aliases,
                    "is_bot": bool(item.get("is_bot", False)),
                }))
                if len(out) >= maxp:
                    break
        if out:
            return out

        names = dedupe([str(x) for x in (metadata.get("participants") or []) if x])
        return [(n, canonicalize(n), {}) for n in names[:maxp]]

    # ------------------------------------------------------------ 主入口
    def extract(self, source_memory_id: int, content: str,
                metadata: dict | None = None) -> ExtractedGraph:
        metadata = metadata or {}
        graph = ExtractedGraph()

        session_id = metadata.get("session_id")
        persona_id = metadata.get("persona_id")
        summary = str(metadata.get("canonical_summary") or content or "")

        topics = dedupe([str(x) for x in (metadata.get("topics") or []) if x]
                        )[: self._cfg("graph_max_topics")]
        participants = self._participants(metadata)
        facts = dedupe([str(x) for x in (metadata.get("key_facts") or []) if x]
                       )[: self._cfg("graph_max_facts")]
        if not facts and summary:
            facts = [summary]

        node_map: dict[str, GraphNode] = {}

        def add_node(node_type: str, value: str,
                     extra: dict | None = None) -> str:
            cv = canonicalize(value)
            if not cv:
                return ""
            n = GraphNode(node_type=node_type, value=str(value).strip(),
                          canonical_value=cv, metadata=extra or {})
            node_map[n.node_key] = n
            return n.node_key

        topic_keys = [k for k in (add_node("topic", t) for t in topics) if k]
        person_keys: list[str] = []
        for name, cv, extra in participants:
            n = GraphNode(node_type="person", value=name,
                          canonical_value=cv, metadata=extra)
            node_map[n.node_key] = n
            person_keys.append(n.node_key)
        fact_keys = [k for k in (add_node("fact", f, {"summary": summary})
                                 for f in facts) if k]

        graph.nodes.extend(node_map.values())

        def add_entry(entry_type: str, text: str, node_keys: list[str],
                      relation_type: str | None = None,
                      confidence: float = 0.8) -> None:
            graph.entries.append(GraphEntry(
                entry_type=entry_type,
                content=text,
                node_keys=list(node_keys),
                relation_type=relation_type,
                source_memory_id=source_memory_id,
                session_id=session_id or "",
                persona_id=persona_id or "",
                metadata={
                    "source_memory_id": source_memory_id,
                    "session_id": session_id,
                    "persona_id": persona_id,
                    "importance": metadata.get("importance", 0.5),
                    "create_time": metadata.get("create_time"),
                    "last_access_time": metadata.get("last_access_time"),
                    "canonical_summary": summary,
                    "graph_confidence": confidence,
                    "source_window": metadata.get("source_window"),
                },
            ))

        for fk in fact_keys:
            add_entry("fact", f"Fact: {node_map[fk].value}. Summary: {summary}",
                      [fk], "fact", 0.9)
        for tk in topic_keys:
            add_entry("topic", f"Topic: {node_map[tk].value}. Summary: {summary}",
                      [tk], "topic", 0.75)
        for pk in person_keys:
            add_entry("participant",
                      f"Participant: {node_map[pk].value}. Summary: {summary}",
                      [pk], "participant", 0.7)

        for tk in topic_keys:
            for fk in fact_keys:
                graph.edges.append(GraphEdge(
                    source_key=tk, target_key=fk, relation_type="describes",
                    source_memory_id=source_memory_id, confidence=0.82,
                    metadata={"summary": summary}))
                add_entry("edge",
                          f"Topic {node_map[tk].value} describes fact "
                          f"{node_map[fk].value}. Summary: {summary}",
                          [tk, fk], "describes", 0.82)

        for pk in person_keys:
            for fk in fact_keys:
                graph.edges.append(GraphEdge(
                    source_key=pk, target_key=fk, relation_type="mentioned_in",
                    source_memory_id=source_memory_id, confidence=0.88,
                    metadata={"summary": summary}))
                add_entry("edge",
                          f"Participant {node_map[pk].value} is linked to fact "
                          f"{node_map[fk].value}. Summary: {summary}",
                          [pk, fk], "mentioned_in", 0.88)

        for i, a in enumerate(person_keys):
            for b in person_keys[i + 1:]:
                graph.edges.append(GraphEdge(
                    source_key=a, target_key=b, relation_type="co_occurs_with",
                    source_memory_id=source_memory_id, confidence=0.7,
                    metadata={"summary": summary}))
                add_entry("edge",
                          f"Participant {node_map[a].value} co-occurs with "
                          f"participant {node_map[b].value}. Summary: {summary}",
                          [a, b], "co_occurs_with", 0.7)

        if not graph.entries and summary:
            sk = add_node("summary", summary)
            if sk:
                graph.nodes = list(node_map.values())
                add_entry("summary", f"Summary: {summary}", [sk], "summary", 0.6)

        return graph


def extract_into(store, source_memory_id: int, content: str,
                 metadata: dict | None = None,
                 config: Any = None) -> ExtractedGraph:
    """抽取并写入 store；返回抽取结果（便于测试与统计）。"""
    g = GraphExtractor(config).extract(source_memory_id, content, metadata)
    for n in g.nodes:
        store.upsert_node(n)
    for e in g.edges:
        store.upsert_edge(e)
    new_entries = []
    for en in g.entries:
        obj, created = store.upsert_entry(en)
        if created:
            new_entries.append(obj)
    g.entries = new_entries
    return g
