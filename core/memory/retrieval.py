# -*- coding: utf-8 -*-
"""双路检索：文档路 + 图谱路，按查询意图加权融合。

旧实现的 `DualRouteRetriever`：

    doc_signal   = 文档路得分 / 文档路最高分
    graph_signal = 图谱路得分 / 图谱路最高分
    final = min(1, w_doc·doc_signal + w_graph·graph_signal + cross_bonus)

    w_doc = 0.65, w_graph = 0.35, cross_bonus = 0.08（两路都命中才给）

**查询意图调权**（这是图谱真正发挥价值的地方）：

| 查询里出现 | 调整 | 例子 |
|---|---|---|
| 关系词 | graph +0.2 / doc −0.2 | 「谁和谁」「认识」「朋友」「老师」 |
| 时间词 | graph +0.1 / doc −0.1 | 「上次」「昨天」「什么时候」 |
| 事实词 | doc +0.15 / graph −0.15 | 「是什么」「解释」「怎么」 |

原因：图谱条目的正文形如
`Participant A is linked to fact B. Summary: ...`，
对"谁和谁什么关系"这类问题天生更贴；而"某概念是什么"更适合文档路。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RELATION_TERMS = ("谁", "和谁", "关系", "认识", "朋友", "同事", "家人", "父母",
                  "妈妈", "爸爸", "老师", "同学", "partner", "friend",
                  "relationship", "with whom")
TEMPORAL_TERMS = ("上次", "昨天", "前天", "刚才", "之前", "什么时候", "哪天",
                  "最近", "last time", "yesterday", "recently", "when")
FACTUAL_TERMS = ("是什么", "什么是", "解释", "定义", "怎么", "如何",
                 "why", "what is", "explain", "define", "how to")

# 「问过去」的词。取代降权只在**问当前状态**时生效，命中这些词就照常召回旧记忆。
PAST_TERMS = ("以前", "之前", "当时", "那时候", "那时", "原来", "曾经",
              "过去", "从前", "当年", "上一次", "原来的")

# 去冗余（MMR）的默认参数：0.7 = 相关性占七成、多样性占三成。
MMR_LAMBDA = 0.7

DEFAULTS = {
    "document_route_weight": 0.65,
    "graph_route_weight": 0.35,
    "cross_route_bonus": 0.08,
}


@dataclass
class RouteHit:
    key: str
    score: float
    content: str
    source_memory_id: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FusedHit:
    key: str
    final_score: float
    content: str
    doc_score: float
    graph_score: float
    intent: str
    breakdown: dict[str, float] = field(default_factory=dict)


def route_weights(query: str, *, doc_w: float, graph_w: float,
                  dynamic: bool = True) -> tuple[float, float, str]:
    """按查询意图调整两路权重，返回归一化后的 (doc, graph, intent)。"""
    if not dynamic:
        return doc_w, graph_w, "fixed"
    q = (query or "").casefold()
    intent = "default"
    d, g = doc_w, graph_w
    rel = any(t in q for t in RELATION_TERMS)
    tmp = any(t in q for t in TEMPORAL_TERMS)
    fac = any(t in q for t in FACTUAL_TERMS)
    if rel:
        g += 0.2
        d -= 0.2
        intent = "relationship"
    if tmp:
        g += 0.1
        d -= 0.1
        intent = "temporal" if intent == "default" else f"{intent}+temporal"
    if fac and not rel:
        d += 0.15
        g -= 0.15
        intent = "factual" if intent == "default" else f"{intent}+factual"
    d = max(0.15, min(0.9, d))
    g = max(0.10, min(0.85, g))
    total = d + g
    if total <= 0:
        return doc_w, graph_w, "fixed"
    return d / total, g / total, intent


def asks_about_past(query: str) -> bool:
    """查询是不是在问「过去/当时」的状态。

    取代降权（新记忆取代旧记忆）只在问**当前状态**时生效 —— 用户问
    「他以前住哪」的时候，被取代的旧记忆恰恰是正确答案，不能压。
    """
    q = (query or "").casefold()
    return any(t in q for t in PAST_TERMS)


# ---------------------------------------------------------------- 去冗余

def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text or "")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def text_similarity(a: str, b: str) -> float:
    """两段正文的 CJK 二元组 Jaccard 相似度（0~1）。

    为什么不用向量：图谱条目的正文往往没有向量，而且我们要的是"两条记忆
    是不是在讲同一件事"。同一段对话固化出来的近义记忆（实测同一个"蛋包饭"
    话题产出了 5 条几乎一样的记忆）文字重合度极高，二元组 Jaccard 足够，
    还省一次 embedding。
    """
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def mmr_diversify(hits: list[FusedHit], k: int, *,
                  lam: float = MMR_LAMBDA) -> list[FusedHit]:
    """MMR 去冗余：同主题的记忆只留 1~2 条，别让 top-k 被近义条目占满。

    排序分高但和已选项太像的，会被"扣掉相似度惩罚"往后排；
    `lam` 越大越看重相关性。传进来的 `hits` 必须已按分数降序。
    """
    if k <= 0:
        return []
    if len(hits) <= 1:
        return list(hits[:k])
    pool = list(hits)
    picked = [pool.pop(0)]
    while pool and len(picked) < k:
        best_i, best_v = 0, None
        for i, h in enumerate(pool):
            red = max((text_similarity(h.content, s.content) for s in picked),
                      default=0.0)
            v = lam * h.final_score - (1.0 - lam) * red
            if best_v is None or v > best_v:
                best_i, best_v = i, v
        picked.append(pool.pop(best_i))
    return picked


class DualRouteRetriever:
    """融合两路结果。两路都命中时给 cross_route_bonus（交叉验证信号）。"""

    def __init__(self, config: Any = None) -> None:
        self.config = config or {}

    def _cfg(self, key: str) -> float:
        try:
            v = self.config.get(key, DEFAULTS[key])
        except Exception:
            v = DEFAULTS[key]
        try:
            return float(v)
        except (TypeError, ValueError):
            return DEFAULTS[key]

    def fuse(self, query: str, doc_hits: list[RouteHit],
             graph_hits: list[RouteHit], *, k: int = 10,
             dynamic: bool = True, diversify: bool = True) -> list[FusedHit]:
        if not doc_hits and not graph_hits:
            return []
        doc_w, graph_w, intent = route_weights(
            query, doc_w=self._cfg("document_route_weight"),
            graph_w=self._cfg("graph_route_weight"), dynamic=dynamic)
        bonus = self._cfg("cross_route_bonus")

        doc_max = max((h.score for h in doc_hits), default=1.0) or 1.0
        graph_max = max((h.score for h in graph_hits), default=1.0) or 1.0
        doc_map = {h.key: h for h in doc_hits}
        graph_map = {h.key: h for h in graph_hits}

        out: list[FusedHit] = []
        for key in set(doc_map) | set(graph_map):
            dh, gh = doc_map.get(key), graph_map.get(key)
            ds = (dh.score / doc_max) if dh is not None else 0.0
            gs = (gh.score / graph_max) if gh is not None else 0.0
            cb = bonus if (dh is not None and gh is not None) else 0.0
            final = min(1.0, doc_w * ds + graph_w * gs + cb)
            content = (dh.content if dh is not None
                       else (gh.content if gh is not None else ""))
            out.append(FusedHit(
                key=key, final_score=final, content=content,
                doc_score=ds, graph_score=gs, intent=intent,
                breakdown={
                    "document_route_score": round(ds, 4),
                    "graph_route_score": round(gs, 4),
                    "document_route_weight": round(doc_w, 4),
                    "graph_route_weight": round(graph_w, 4),
                    "cross_route_bonus": round(cb, 4),
                    "dual_route_final_score": round(final, 4),
                }))
        out.sort(key=lambda h: -h.final_score)
        # 去冗余放在最后：融合分只回答"哪条最相关"，回答不了"这几条是不是
        # 在讲同一件事"。同一段对话固化出的近义记忆会把 top-k 占满，
        # 注入进上下文就是 5 遍同样的话（实测 5 条"蛋包饭"记忆全进了 top-5）。
        if diversify:
            return mmr_diversify(out, k)
        return out[:k]

    def report(self) -> dict:
        return {"document_route_weight": self._cfg("document_route_weight"),
                "graph_route_weight": self._cfg("graph_route_weight"),
                "cross_route_bonus": self._cfg("cross_route_bonus")}
