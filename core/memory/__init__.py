# -*- coding: utf-8 -*-
"""记忆层：累积 → 固化（含图谱抽取）→ 双路召回。

* `embedder.py`   —— embedding provider 封装（可选能力，不可用则整体降级）
* `summarizer.py` —— LLM 把对话窗口固化成带元数据的记忆文档
* `store.py`      —— 文档路的 JSON 存储（向量 + 余弦 Top-K）
* `service.py`    —— 门面：累积 / 固化 / 双路召回
* `retrieval.py`  —— 文档路 + 图谱路按查询意图加权融合
"""

from .embedder import Embedder  # noqa: F401
from .retrieval import DualRouteRetriever, FusedHit, RouteHit  # noqa: F401
from .service import MemoryService, build_default  # noqa: F401
from .store import (MemoryAtom, MemoryStore, cosine,  # noqa: F401
                    decayed_score)
from .summarizer import (MemorySummarizer, fallback_consolidation,  # noqa: F401
                         parse_consolidation)

__all__ = ["MemoryAtom", "MemoryStore", "cosine", "decayed_score",
           "Embedder", "MemoryService", "build_default",
           "MemorySummarizer", "parse_consolidation", "fallback_consolidation",
           "DualRouteRetriever", "RouteHit", "FusedHit"]
