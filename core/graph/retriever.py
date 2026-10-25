# -*- coding: utf-8 -*-
"""图谱路检索：BM25 关键词 + 向量，用 RRF 融合后加权排序。

两处与旧实现**逐参数对齐**（不是"我理解的等价物"）：

1. **RRF 融合**：`score = Σ 1/(k + rank)`，k=60。
2. **加权公式**：

       final = (α·rrf_norm + β·importance + γ·recency + δ·graph_confidence) × temporal

   α=0.55 β=0.20 γ=0.15 δ=0.10，`recency = exp(-decay_rate · days_old)`，
   `decay_rate=0.01`。

时间衰减用条目 metadata 里的 `create_time` / `last_access_time`；
两者都没有才退回 ISO 时间戳。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from .model import GraphEntry

RRF_K = 60

DEFAULTS = {
    "graph_score_alpha": 0.55,
    "graph_score_beta": 0.20,
    "graph_score_gamma": 0.15,
    "graph_score_delta": 0.10,
    "decay_rate": 0.01,
}


@dataclass
class GraphResult:
    entry_key: str
    final_score: float
    rrf_score: float
    keyword_score: float | None
    vector_score: float | None
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score_breakdown: dict[str, float] = field(default_factory=dict)

    @property
    def doc_id(self) -> int:
        """图谱条目挂在哪条记忆上（与文档路对齐用）。"""
        try:
            return int(self.metadata.get("source_memory_id") or 0)
        except (TypeError, ValueError):
            return 0


def rrf_fuse(lists: list[list[str]], *, k: int = RRF_K) -> dict[str, float]:
    """对多个"已排序的 id 列表"做 Reciprocal Rank Fusion。"""
    scores: dict[str, float] = {}
    for ids in lists:
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def _clamp(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


class GraphRetriever:
    def __init__(self, store: Any, embedder: Any = None,
                 config: Any = None) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config or {}
        self.stats = {"searches": 0, "keyword_hits": 0, "vector_hits": 0,
                      "fused": 0, "last_error": ""}

    def _cfg(self, key: str) -> float:
        try:
            v = self.config.get(key, DEFAULTS[key])
        except Exception:
            v = DEFAULTS[key]
        try:
            return float(v)
        except (TypeError, ValueError):
            return DEFAULTS[key]

    # ------------------------------------------------------------ 向量路
    def _vector_search(self, query_vec: list[float], k: int, *,
                       session_id: str | None, persona_id: str | None,
                       strict_persona_id: str | None = None
                       ) -> list[tuple[GraphEntry, float]]:
        if not query_vec:
            return []
        from ..memory.store import cosine
        out: list[tuple[GraphEntry, float]] = []
        for e in self.store.entries.values():
            if not e.vec:
                continue
            if session_id and e.session_id and e.session_id != session_id:
                continue
            if strict_persona_id:
                if e.persona_id != strict_persona_id:
                    continue
            elif persona_id and e.persona_id and e.persona_id != persona_id:
                continue
            s = cosine(query_vec, e.vec)
            if s > 0:
                out.append((e, s))
        out.sort(key=lambda kv: -kv[1])
        return out[:k]

    # ------------------------------------------------------------ 主接口
    async def search(self, query: str, k: int = 10, *,
                     session_id: str | None = None,
                     persona_id: str | None = None,
                     prefer_persona: str | None = None,
                     other_factor: float = 1.0,
                     strict_persona: bool = False) -> list[GraphResult]:
        """图谱路检索。

        人格策略（默认不改变旧行为）：
          * `persona_id`：旧语义硬过滤；
          * `prefer_persona`：当前人格（用于降权/严格隔离的参照）；
          * `strict_persona=True`：只保留 `persona_id == prefer_persona` 的条目；
          * `other_factor`：别人格条目的 final_score 乘以此系数（本命优先降权）。
        """
        if not (query or "").strip():
            return []
        self.stats["searches"] += 1

        ref = prefer_persona or persona_id
        strict_id = ref if (strict_persona and ref) else None
        kw = self.store.bm25_search(query, max(k * 2, k),
                                    session_id=session_id,
                                    persona_id=persona_id,
                                    strict_persona_id=strict_id)
        vec: list[tuple[GraphEntry, float]] = []
        if self.embedder is not None and getattr(self.embedder, "available", False):
            try:
                # embed_dedup：与文档路并发时共享同一次 embedding 请求
                # （recall_texts 两路都 embed 同一条 query）。
                qv = await self.embedder.embed_dedup(query)
                vec = self._vector_search(qv, max(k * 2, k),
                                          session_id=session_id,
                                          persona_id=persona_id,
                                          strict_persona_id=strict_id)
            except Exception as e:
                self.stats["last_error"] = repr(e)

        self.stats["keyword_hits"] += len(kw)
        self.stats["vector_hits"] += len(vec)
        if not kw and not vec:
            return []

        kw_ids = [e.entry_key for e, _ in kw]
        vec_ids = [e.entry_key for e, _ in vec]
        rrf = rrf_fuse([kw_ids, vec_ids])
        if not rrf:
            return []
        self.stats["fused"] += len(rrf)

        kw_map = {e.entry_key: s for e, s in kw}
        vec_map = {e.entry_key: s for e, s in vec}
        max_rrf = max(rrf.values()) or 1.0
        now = time.time()

        alpha = self._cfg("graph_score_alpha")
        beta = self._cfg("graph_score_beta")
        gamma = self._cfg("graph_score_gamma")
        delta = self._cfg("graph_score_delta")
        decay_rate = self._cfg("decay_rate")

        results: list[GraphResult] = []
        for key, rrf_score in rrf.items():
            e = self.store.entries.get(key)
            if e is None:
                continue
            meta = e.metadata if isinstance(e.metadata, dict) else {}
            importance = _clamp(meta.get("importance"), 0.5)
            confidence = _clamp(meta.get("graph_confidence"), 0.7)
            created = e.created_ts
            last_access = now
            try:
                la = float(meta.get("last_access_time") or 0)
                if la > 0:
                    last_access = la
            except (TypeError, ValueError):
                pass
            reference = max(created, last_access)
            days_old = max(0.0, (now - reference) / 86400.0)
            recency = math.exp(-decay_rate * days_old)

            # 条目级时间衰减（有 ttl 时按半衰期式衰减，否则不衰减）
            temporal = 1.0
            try:
                ttl = float(meta.get("ttl_days") or 0)
            except (TypeError, ValueError):
                ttl = 0.0
            if ttl > 0:
                days_since = max(0.0, (now - last_access) / 86400.0)
                temporal = 0.5 ** (days_since / ttl)

            rrf_norm = rrf_score / max_rrf
            final = (alpha * rrf_norm + beta * importance
                     + gamma * recency + delta * confidence) * temporal

            # 本命优先降权：别人格条目在融合前把 final 打折
            if other_factor != 1.0 and ref:
                _pid = getattr(e, "persona_id", "") or ""
                if _pid and _pid != ref:
                    final *= other_factor

            results.append(GraphResult(
                entry_key=key, final_score=final, rrf_score=rrf_score,
                keyword_score=kw_map.get(key), vector_score=vec_map.get(key),
                content=e.content, metadata=meta,
                score_breakdown={
                    "graph_rrf_normalized": round(rrf_norm, 4),
                    "graph_importance": round(importance, 4),
                    "graph_recency_weight": round(recency, 4),
                    "graph_confidence": round(confidence, 4),
                    "graph_temporal_factor": round(temporal, 4),
                    "graph_final_score": round(final, 4),
                }))

        results.sort(key=lambda r: -r.final_score)
        return results[:k]

    # ------------------------------------------------------------ 图遍历
    def related_entries(self, seed_keys: list[str], *, hops: int = 1,
                        limit: int = 10) -> list[GraphEntry]:
        """从种子节点沿边扩展，取邻域条目的正文（供"关系类"问题用）。"""
        if not seed_keys:
            return []
        keys = self.store.expand(seed_keys, hops=hops)
        out: list[GraphEntry] = []
        seen: set[str] = set()
        for nk in keys:
            for e in self.store.entries_for_node(nk, limit=limit):
                if e.entry_key in seen:
                    continue
                seen.add(e.entry_key)
                out.append(e)
                if len(out) >= limit:
                    return out
        return out

    def report(self) -> dict:
        return {**self.stats, "graph": self.store.stats()}
