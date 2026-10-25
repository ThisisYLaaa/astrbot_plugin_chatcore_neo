# -*- coding: utf-8 -*-
"""记忆服务：累积对话 → 固化（含图谱抽取）→ 双路召回。

完整链路（对齐旧 livingmemory）
------------------------------
写入侧::

    对话轮次 → 累积到阈值 → LLM 固化 → 记忆文档（summary/topics/key_facts/participants）
                                    ├→ 文档路：文档 embed → MemoryStore
                                    └→ 图谱路：确定性抽取 → 节点/边/条目 → 条目 embed

读取侧::

    query ─┬→ 文档路：MemoryStore 余弦 Top-K
           └→ 图谱路：BM25 + 条目向量 → RRF → 加权
                    ↓
             DualRoute 按查询意图加权融合 → 注入文本

**为什么不再"每轮存一条原子"**
旧实现存的是固化后的文档（1495 条 documents），不是原始轮次。
原始轮次信息密度低、噪音大，直接做向量召回质量差；
固化后每条都带总结与结构化字段，召回才准，图谱也才有输入。

**未固化的轮次会持久化**（`memory/_pending.json`），
因此重启不会丢掉还没攒够一个窗口的对话。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from ..db import to_f32
from ..graph import GraphExtractor, GraphRetriever, GraphStore, extract_into
from ..paths import MEMORY_DIR, inst_memory_dir
from .embedder import Embedder
from .retrieval import DualRouteRetriever, RouteHit, asks_about_past
from .store import MemoryStore, cosine
from .summarizer import MemorySummarizer

PENDING_PATH = "memory/_pending.json"

# 取代降权：被更新记忆取代过的旧条目，在「问当前状态」的查询里乘以此系数。
# 不删、只压 —— 用户问「他以前住哪」时它还是正确答案。
SUPERSEDED_FACTOR = 0.35

# 取代判据：向量相似度下限（再叠加"至少共享一个 topic"）。
SUPERSEDED_SIM = 0.8
# 一次固化最多标记几条旧记忆被取代（防止一条新记忆把半库都压下去）。
SUPERSEDED_MAX = 3

# ---------------------------------------------------------------- 召回闸门
# 「嗯」「草」「哈哈」「？？？」这类消息**没有可召回的语义**：与任何记忆的
# 相似度都低，勉强命中的也基本是噪音，而 embedding 调用是既有钱又有延迟的
# （线上实测单次可拖 200ms~40s）。所以在 embedding 之前就把它们拦掉。
_GATE_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)   # 空白/标点/符号/emoji
_GATE_WORDS = frozenset({
    "嗯", "哦", "啊", "额", "呃", "唔", "哈", "嘿", "唉", "哎", "咦", "喂",
    "草", "艹", "靠", "6", "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嗯嗯", "哦哦",
    "在吗", "在么", "在不在", "好的", "好啊", "笑死", "谢谢", "谢谢啦",
})
_GATE_CHARS = frozenset("嗯哦啊额呃唔哈嘿唉哎咦喂草艹靠6")


def recall_gate(query: str) -> bool:
    """返回 True = 这轮**跳过召回**（无实词的短消息）。

    判定刻意保守 —— **只要出现任意一个正常词就放行**，宁可多召回一次，
    也不误拦。拦的只有三种：
      * 剔掉空白/标点/符号/emoji 后什么都不剩（"？？？"、"……"、"😂"）；
      * 剩下的整体是个语气词（"嗯"、"草"、"在吗"、"好的"）；
      * 剩下的全是同一个语气字（"嗯嗯嗯嗯"、"哈哈哈哈哈"）。
    """
    text = (query or "").strip()
    if not text:
        return True
    core = _GATE_PUNCT.sub("", text)
    if not core:
        return True
    if core.casefold() in _GATE_WORDS:
        return True
    return all(ch in _GATE_CHARS for ch in core)

# 记忆人格策略（配置项 `memory_persona_policy`）：
#   0 关闭     —— 跨人格共享，仅按来源人格加前缀标注（默认，保持旧行为）
#   1 本命优先 —— 别人格记忆的排序分乘 PERSONA_OTHER_FACTOR 后仍保留
#   2 严格隔离 —— 只召回当前人格的记忆
PERSONA_POLICY_OFF = 0
PERSONA_POLICY_PREFER = 1
PERSONA_POLICY_STRICT = 2
PERSONA_OTHER_FACTOR = 0.5


class MemoryService:
    def __init__(self, embedder: Embedder, store: MemoryStore | None = None,
                 *, config: Any = None, context: Any = None,
                 graph_store: GraphStore | None = None,
                 summarizer: MemorySummarizer | None = None) -> None:
        self.embedder = embedder
        self.store = store or MemoryStore()
        self.config = config or {}
        self.context = context
        # 图谱与待固化缓冲**跟随 store 的根目录**。
        # 之前这里硬编码 MEMORY_DIR，导致测试注入临时 store 时，
        # 图谱仍写到真实 plugin_data 里 —— 一个根目录只能有一个来源。
        base = Path(getattr(self.store, "root", None) or MEMORY_DIR)
        self.graph = graph_store or GraphStore(base / "graph.db")
        self.summarizer = summarizer or MemorySummarizer(context, self.config)
        self.graph_retriever = GraphRetriever(self.graph, embedder, self.config)
        self.dual = DualRouteRetriever(self.config)
        self._pending: dict[str, list[dict]] = {}
        self._pending_path = base / "_pending.json"
        self._pending_dirty = False
        # 维度迁移：`_reembed_dim` 记录已处理过的向量维度（同一维度只扫一次），
        # `_reembed_task` 是后台重编码任务（同时在跑的只允许一个）。
        self._reembed_dim = 0
        self._reembed_task: Any = None
        self.stats_counters = {
            "turns": 0, "consolidated": 0, "degraded": 0,
            "graph_entries": 0, "graph_entry_vectors": 0, "recalls": 0,
            "recall_gated": 0, "reembedded": 0,
            "reembed_failed": 0, "superseded": 0, "manual_notes": 0,
            "edited": 0,
            "last_error": ""}
        self._load_pending()

    # ------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default) or 0)
        except (TypeError, ValueError):
            return default

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enable_memory", True))

    @property
    def available(self) -> bool:
        return self.enabled and self.embedder.available

    # ------------------------------------------------------------ 待固化缓冲
    def _load_pending(self) -> None:
        try:
            raw = self._pending_path.read_text(encoding="utf-8").strip()
            d = json.loads(raw) if raw else {}
            if isinstance(d, dict):
                self._pending = {k: list(v) for k, v in d.items()
                                 if isinstance(v, list)}
        except FileNotFoundError:
            pass
        except Exception:
            self._pending = {}

    def _save_pending(self) -> None:
        if not self._pending_dirty:
            return
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pending_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._pending, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self._pending_path)
            self._pending_dirty = False
        except Exception:
            pass

    # ------------------------------------------------------------ 写入
    def note_turn(self, key: str, user_msg: str, reply: str,
                  *, persona: str = "", uid: str = "",
                  user_name: str = "") -> int:
        """把一轮对话记进待固化缓冲，返回缓冲轮数。

        **不落向量、不建图** —— 固化时才做，避免为闲聊里的每句话付费。
        """
        if not self.enabled:
            return 0
        buf = self._pending.setdefault(key, [])
        buf.append({
            "ts": time.time(),
            "user": (user_msg or "").strip()[:800],
            "reply": (reply or "").strip()[:800],
            "uid": str(uid or ""),
            "user_name": str(user_name or ""),
            "persona": str(persona or ""),
        })
        self.stats_counters["turns"] += 1
        self._pending_dirty = True
        self._save_pending()
        return len(buf)

    def pending_size(self, key: str, persona: str = "") -> int:
        """某个记忆键里待固化的条数；传 persona 时只数该人格的缓冲。"""
        n = 0
        for t in self._pending.get(key, []):
            if not persona or str(t.get("persona") or "") == persona:
                n += 1
        return n

    def should_consolidate(self, key: str, persona: str = "") -> bool:
        n = self.pending_size(key, persona)
        if n <= 0:
            return False
        turns = max(1, self._int_cfg("memory_consolidate_turns", 10))
        return n >= turns

    def _window_text(self, turns: list[dict]) -> str:
        lines = []
        for t in turns:
            who = t.get("user_name") or t.get("uid") or "用户"
            if t.get("user"):
                lines.append(f"{who}：{t['user']}")
            if t.get("reply"):
                lines.append(f"bot：{t['reply']}")
        return "\n".join(lines)

    def _participants(self, turns: list[dict]) -> list[dict]:
        """从窗口里收集参与者（拿不到稳定 QQ 号时退回昵称）。"""
        out: list[dict] = []
        seen: set[str] = set()
        for t in turns:
            uid = str(t.get("uid") or "")
            name = str(t.get("user_name") or uid or "").strip()
            if not name:
                continue
            ik = uid or name
            if ik in seen:
                continue
            seen.add(ik)
            entry = {"display_name": name}
            if uid:
                entry["identity_key"] = uid
                entry["sender_id"] = uid
            out.append(entry)
        return out[:8]

    async def consolidate(self, key: str, *, session_id: str = "",
                          persona_id: str = "",
                          umo: str = "", persona_desc: str = "") -> dict | None:
        """把缓冲里的一窗对话固化成一条记忆 + 一拍图谱。

        `persona_desc`：角色身份描述，透传给总结代理，
        让记忆以所扮演角色的第一人称落笔；为空则用通用口吻。
        """
        turns = self._pending.get(key) or []
        if not turns:
            return None
        # 缓冲按人格隔离固化：只固化当前 persona 的 turns。人格切换后同一
        # 记忆组里会混着 A/B 的对话，若整批固化成一条记忆，来源 persona
        # 只能标成最后一次的人格，召回时的"来源标注"就会失真。
        # 只有缓冲里**确实带 persona**时才严格隔离；全是旧格式（无 persona）
        # 数据时不过滤，避免阻断历史数据固化。
        if persona_id and any(str(t.get("persona") or "") for t in turns):
            turns = [t for t in turns
                     if str(t.get("persona") or "") == persona_id]
        if not turns:
            return None
        if not self.available:
            # embedding 不可用：保留缓冲，不清空（下次可用时再固化）
            return None

        try:
            text = self._window_text(turns)
            kw = {}
            if persona_desc:
                kw["persona"] = persona_desc
            result = await self.summarizer.consolidate(
                text, self._participants(turns), umo=umo or key, **kw)
            if result.get("degraded"):
                self.stats_counters["degraded"] += 1

            summary = str(result.get("summary") or text[:300])
            # 文档正文：summary + 关键事实（比裸 summary 更好召回）
            facts = result.get("key_facts") or []
            doc_text = summary
            if facts:
                doc_text += "\n" + "\n".join(f"- {f}" for f in facts)

            # 记下本轮说话人：不记的话 `recall_texts(uid=...)` 的过滤永不生效
            _uids = sorted({str(t.get("uid") or "") for t in turns
                            if t.get("uid")})
            # 记忆类型：固化器给的，决定半衰期（见 store.KIND_HALF_LIFE）。
            # 认不出来时退回 document（倍率 1.0，等价于旧行为）。
            kind = str(result.get("kind") or "document").strip().lower()
            if kind not in ("document", "dialog", "fact", "preference"):
                kind = "document"
            meta = {
                "uids": _uids,
                "summary": summary,
                "topics": list(result.get("topics") or []),
                "key_facts": list(facts),
                "participants": [p.get("display_name") for p in self._participants(turns)],
                "participant_identities": self._participants(turns),
                "kind": kind,
                "importance": float(result.get("importance", 0.4) or 0.4),
                "session_id": session_id or key,
                "persona_id": persona_id,
                "create_time": time.time(),
                "last_access_time": time.time(),
                "canonical_summary": summary,
                "turns": len(turns),
                "source_window": [t.get("ts") for t in turns],
            }

            vec = await self.embedder.embed(doc_text)
            if vec:
                self._maybe_reembed(len(vec))
            # metadata 直接带进 add()：写入是即时的，事后补字段还得再回写一次
            atom = self.store.add(key, doc_text, vec, persona=persona_id,
                                  uid="", kind=kind,
                                  importance=meta["importance"], metadata=meta)

            memory_id = atom.id and abs(hash(atom.id)) % (10 ** 9) or 0
            meta["source_memory_id"] = memory_id
            atom.metadata["source_memory_id"] = memory_id
            # source_memory_id 是插入之后才算出来的 → 登记这一条并立刻回写
            self.store.touch(key, atom)
            self.store.save(key)

            # 取代检测：新记忆若"讲的是同一件事"，把旧的那条**降权保留**
            # （不删）—— 记忆是用户的资产，判错了也不能丢数据。
            if bool(self._cfg("memory_supersede", True)):
                try:
                    marked = self._mark_superseded(key, atom, vec)
                    if marked:
                        self.stats_counters["superseded"] += marked
                except Exception as e:
                    self.stats_counters["last_error"] = repr(e)

            await self._grow_graph(memory_id, doc_text, meta)

            # 顺手裁剪：`MemoryStore.prune` 之前没有任何生产调用点，
            # 记忆只增不减（长期跑下来 JSON 会一直膨胀）
            try:
                keep = self._int_cfg("memory_max_atoms", 2000)
                if keep > 0:
                    self.store.prune(key, keep=keep)
            except Exception:
                pass

            # 固化成功才清缓冲
            self._pending.pop(key, None)
            self._pending_dirty = True
            self._save_pending()
            self.stats_counters["consolidated"] += 1
            return {"memory_id": memory_id, "summary": summary,
                    "topics": meta["topics"], "facts": len(facts),
                    "degraded": bool(result.get("degraded"))}
        except Exception as e:
            self.stats_counters["last_error"] = repr(e)
            return None

    async def remember(self, key: str, text: str, *, kind: str = "fact",
                       importance: float = 1.0, persona_id: str = "",
                       uid: str = "", user_name: str = "") -> dict:
        """把一句话**立刻**写进长期记忆（importance 拉满）。

        与 `note_turn` 的分工：`note_turn` 只是攒缓冲、等攒够一轮才走 LLM 固化，
        适合"聊天顺带产生的记忆"；`remember` 是即时、精确的一条，用于
        「记住这件事」这类明确指令 —— 不经过 LLM 总结，就不会被总结改写成
        别的意思。

        返回 `{"ok": True, ...}` 或 `{"ok": False, "error": ...}`。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        if not self.enabled:
            return {"ok": False, "error": "memory_disabled"}
        if not self.available:
            return {"ok": False, "error": "embedding_unavailable"}
        kind = str(kind or "fact").strip().lower()
        if kind not in ("document", "dialog", "fact", "preference"):
            kind = "fact"
        vec = await self.embedder.embed(text)
        if not vec:
            return {"ok": False, "error": "embedding_failed"}

        meta = {
            "summary": text,
            "topics": [],
            "key_facts": [text],
            "participants": [user_name] if user_name else [],
            "kind": kind,
            "importance": float(importance),
            "session_id": key,
            "persona_id": persona_id,
            "create_time": time.time(),
            "last_access_time": time.time(),
            "canonical_summary": text,
            "manual": True,
        }
        before = len(self.store.load(key))
        atom = self.store.add(key, text, vec, persona=persona_id, uid=uid,
                              kind=kind, importance=float(importance),
                              metadata=meta)
        deduped = len(self.store.load(key)) == before

        memory_id = abs(hash(atom.id)) % (10 ** 9)
        meta["source_memory_id"] = memory_id
        atom.metadata["source_memory_id"] = memory_id
        self.store.touch(key, atom)
        self.store.save(key)

        # 手动记的也进图谱，否则图谱路（占 35% 权重）永远看不到它
        try:
            await self._grow_graph(memory_id, text, meta)
        except Exception as e:
            self.stats_counters["last_error"] = repr(e)

        # 手动记的多半是"当前状态"，顺手做取代检测
        if bool(self._cfg("memory_supersede", True)):
            try:
                marked = self._mark_superseded(key, atom, vec)
                if marked:
                    self.stats_counters["superseded"] += marked
            except Exception as e:
                self.stats_counters["last_error"] = repr(e)

        self.stats_counters["manual_notes"] += 1
        return {"ok": True, "id": atom.id, "text": atom.text, "kind": kind,
                "deduped": deduped}

    def _mark_superseded(self, key: str, new_atom, new_vec) -> int:
        """把"被新记忆取代"的旧记忆标上 `superseded_by`（**只降权，不删**）。

        判据刻意保守，两道都要过：向量相似度 ≥ `SUPERSEDED_SIM`，
        且至少共享一个 topic。只共享 topic 不相似（同一话题的另一件事）
        不算取代，只相似不共话题（换了说法讲另一件事）也不算。

        降权发生在**召回打分**里，而且只在问「当前状态」的查询上生效
        （`asks_about_past` 命中时照常召回），所以判错也不会让历史问不出来。
        """
        new_topics = {str(t) for t in ((new_atom.metadata or {}).get("topics")
                                      or [])}
        if not new_topics or not len(new_vec or ()):
            return 0
        marked = 0
        for a in self.store.load(key):
            if a.id == new_atom.id or not len(a.vec):
                continue
            if len(a.vec) != len(new_vec):
                continue          # 维度不符算不出余弦，别乱标
            if (a.metadata or {}).get("superseded_by"):
                continue          # 已经被更晚的记忆取代过了，别改指向
            if cosine(a.vec, new_vec) < SUPERSEDED_SIM:
                continue
            old_topics = {str(t) for t in ((a.metadata or {}).get("topics")
                                           or [])}
            if not (new_topics & old_topics):
                continue
            a.metadata["superseded_by"] = new_atom.id
            a.metadata["superseded_at"] = time.time()
            self.store.touch(key, a)
            marked += 1
            if marked >= SUPERSEDED_MAX:
                break
        if marked:
            self.store.save(key)
        return marked

    async def _grow_graph(self, memory_id: int, doc_text: str,
                          meta: dict) -> dict:
        """抽取并写入图谱；顺带给新条目补向量。"""
        g = extract_into(self.graph, memory_id, doc_text, meta, self.config)
        self.stats_counters["graph_entries"] += len(g.entries)

        # 条目向量：默认开，但有上限与开关（每次固化可能新增十几条）
        if bool(self._cfg("graph_embed_entries", True)) and g.entries:
            cap = max(0, self._int_cfg("graph_max_embed_per_memory", 12))
            vectored = []
            for entry in g.entries[:cap]:
                try:
                    v = await self.embedder.embed(entry.content)
                except Exception:
                    v = []
                if v:
                    entry.vec = v
                    vectored.append(entry)
                    self.stats_counters["graph_entry_vectors"] += 1
            # 向量是 upsert 之后才拿到的（embed 要 await），而写入已是即时的
            # → 必须显式补写一次，否则这些向量只活在内存里
            self.graph.save_entries(vectored)
        self.graph.save()
        return {"nodes": len(g.nodes), "edges": len(g.edges),
                "entries": len(g.entries)}

    # ------------------------------------------------------------ 维度迁移
    def _maybe_reembed(self, dim: int) -> None:
        """发现向量维度变了就起一个后台任务，分批重编码旧向量。

        只在**已经成功拿到向量**的路径上调用 —— 那时才知道当前维度。
        同一个维度只处理一次（否则每轮召回都要重扫一遍库）；库里没有
        维度不符的条目时连任务都不建，正常路径零开销。
        """
        if dim <= 0 or dim == self._reembed_dim:
            return
        self._reembed_dim = dim
        if self._reembed_task is not None and not self._reembed_task.done():
            return
        try:
            if self.store.count_stale(dim) <= 0:
                return
        except Exception:
            return
        try:
            self._reembed_task = asyncio.get_running_loop().create_task(
                self._reembed_worker(dim))
        except RuntimeError:                     # 没有运行中的事件循环
            self._reembed_task = None

    async def _reembed_worker(self, dim: int) -> None:
        """把维度不符的旧向量逐批重新编码（后台跑，不阻塞主链路）。

        编码不出来的条目**不删**，只记数并告警 —— 记忆是用户的资产，
        自动迁移失败不该变成自动删除。
        """
        batch = max(1, self._int_cfg("memory_reembed_batch", 32))
        total = self.store.count_stale(dim)
        if total <= 0:
            return
        # 陈旧缓存必须先清：否则 embed() 命中旧维度缓存，重编码等于没做。
        # 只在**缓存与原子同库**时清 —— 两者本就设计成同一个 memory.db，
        # 不同库说明是被测试/非常规装配拆开的，不该去动别人的缓存。
        try:
            if Path(getattr(self.embedder, "db_path", "")) == Path(self.store.db_path):
                self.embedder.purge_stale_cache(dim)
        except Exception:
            pass
        seen: set[str] = set()
        done = failed = 0
        while True:
            try:
                rows = self.store.stale_atoms(dim, limit=batch, skip=seen)
            except Exception:
                break
            if not rows:
                break
            touched: set[str] = set()
            for umo, atom in rows:
                seen.add(atom.id)
                text = (atom.text or "").strip()
                if not text:                     # 空正文无从编码，跳过
                    failed += 1
                    continue
                try:
                    vec = await self.embedder.embed(text)
                except Exception:
                    vec = []
                if not vec or len(vec) != dim:
                    failed += 1
                    continue
                atom.vec = to_f32(vec)
                self.store.touch(umo, atom)
                touched.add(umo)
                done += 1
            for umo in touched:
                self.store.save(umo)
            await asyncio.sleep(0)               # 让出事件循环，别独占
        self.stats_counters["reembedded"] += done
        self.stats_counters["reembed_failed"] += failed
        if failed:
            self._warn_reembed(total, done, failed)

    def _warn_reembed(self, total: int, done: int, failed: int) -> None:
        """重编码有失败时**显式告警**：静默失效的记忆用户是感知不到的。"""
        import logging
        logging.getLogger("astrbot").warning(
            f"[chatcore_neo] embedding 维度已变更：共 {total} 条记忆待重编码，"
            f"成功 {done} 条，{failed} 条重编码失败（正文为空或 embedding 调用失败），"
            f"这些记忆将无法被召回。可在管理页「记忆」里删除，或检查 embedding 配置。")

    # ------------------------------------------------------------ 召回
    async def recall_texts(self, key: str, query: str, *, persona: str = "",
                           uid: str = "", top_k: int | None = None,
                           persona_labels: dict | None = None,
                           policy: int | None = None) -> list[str]:
        """双路召回，返回可直接注入的文本行。

        `persona_labels`：pid → 显示名。当命中的记忆**不是当前人格**写的时，
        注入前加「来源人格」前缀标注，避免当前人格把别人格的第一人称记忆
        当成自己的亲身经历（会话内人格切换后扮演混乱）。

        `policy`：人格策略（缺省读配置 `memory_persona_policy`）——
        0 关闭（跨人格共享、仅标注）/ 1 本命优先（别人格降权保留）/ 2 严格隔离。
        """
        if not self.available or not (query or "").strip():
            return []
        # 闸门：无实词的短消息（"嗯"/"草"/"？？？"）不值得花 embedding 与噪音
        if bool(self._cfg("memory_recall_gate", True)) and recall_gate(query):
            self.stats_counters["recall_gated"] += 1
            return []
        k = top_k or self._int_cfg("memory_top_k", 5)
        if k <= 0:
            return []
        self.stats_counters["recalls"] += 1
        half_life = float(self._cfg("memory_decay_days", 30.0) or 30.0)
        min_score = float(self._cfg("memory_min_score", 0.35) or 0.35)
        # 分类差异化衰减 + 取代降权。取代降权只在**问当前状态**时生效：
        # 用户问「他以前住哪」的时候，被取代的旧记忆恰恰是正确答案。
        kind_decay = bool(self._cfg("memory_kind_decay", True))
        superseded_factor = (1.0 if asks_about_past(query)
                             else SUPERSEDED_FACTOR)

        # 人格策略：把 0/1/2 翻译成两条召回路各自能懂的参数
        if policy is None:
            try:
                policy = int(self._cfg("memory_persona_policy",
                                       PERSONA_POLICY_OFF) or 0)
            except (TypeError, ValueError):
                policy = PERSONA_POLICY_OFF
        policy = int(policy)
        strict = policy >= PERSONA_POLICY_STRICT
        other_factor = (PERSONA_OTHER_FACTOR
                        if policy == PERSONA_POLICY_PREFER else 1.0)
        prefer = persona if policy >= PERSONA_POLICY_PREFER else ""

        # 融合后的 FusedHit 不带来源信息，这里按融合 key 记下「来源人格」
        src_map: dict[str, str] = {}

        # 文档路
        doc_hits: list[RouteHit] = []
        # embed_dedup：文档路与图谱路用的是同一条 query，合并成一次
        # embedding 调用（缓存 miss 时线上实测一次调用可能拖 20~40s，
        # 没必要打两发）。
        qv = await self.embedder.embed_dedup(query)
        if qv:
            # 拿到向量才知道当前维度；若库里还躺着旧维度的向量，顺手起后台迁移
            self._maybe_reembed(len(qv))
            # 人格策略在 store 内落地：policy 0 不按 persona 过滤（persona 只用于
            # 标注来源）；1 把别人格降权但保留；2 只留当前人格。uid 维度始终过滤
            hits = self.store.recall(key, qv, top_k=max(k * 2, k),
                                     min_score=min_score,
                                     half_life_days=half_life,
                                     persona="", uid=uid,
                                     prefer_persona=prefer,
                                     other_factor=other_factor,
                                     strict_persona=strict,
                                     kind_decay=kind_decay,
                                     superseded_factor=superseded_factor)
            for atom, score in hits:
                # key 用 `source_memory_id`（与图谱条目的 doc_id 同源）：
                # 原来文档路用 atom.id、图谱路用 "g:<sha1>"，两个 key 空间
                # 不相交 → DualRoute 的 cross_route_bonus（"两路都命中"的
                # 交叉验证加成）永不触发。
                _mid = (atom.metadata or {}).get("source_memory_id")
                kk = str(_mid or atom.id)
                doc_hits.append(RouteHit(key=kk, score=score,
                                         content=atom.text,
                                         metadata=dict(atom.metadata or {})))
                if atom.persona:
                    src_map[kk] = atom.persona

        # 图谱路
        graph_hits: list[RouteHit] = []
        if bool(self._cfg("enable_graph_retrieval", True)):
            try:
                gh = await self.graph_retriever.search(
                    query, max(k * 2, k), session_id=key or None,
                    persona_id=None, prefer_persona=prefer or None,
                    other_factor=other_factor, strict_persona=strict)
                for r in gh:
                    kk = str(r.doc_id or f"g:{r.entry_key}")
                    graph_hits.append(RouteHit(
                        key=kk,
                        score=r.final_score,
                        content=r.content, source_memory_id=r.doc_id,
                        metadata=dict(r.metadata)))
                    _p = str((r.metadata or {}).get("persona_id") or "")
                    if _p:
                        src_map[kk] = _p
            except Exception as e:
                self.stats_counters["last_error"] = repr(e)

        fused = self.dual.fuse(query, doc_hits, graph_hits, k=k,
                               diversify=bool(self._cfg("memory_mmr", True)))
        if fused:
            self.store.save(key)
        # 图谱条目自带 Summary，正文较长；截断以免挤爆上下文
        cap = self._int_cfg("memory_inject_max_chars", 300)
        out = []
        for h in fused:
            t = (h.content or "").strip()
            if not t:
                continue
            t = t[:cap] if cap > 0 else t
            # 来源标注：跨人格共享防扮演混乱 —— 他人人格的记忆标名字，
            # 无 persona 的历史数据（迁移/旧格式）标"来源不明"，自己的不标
            src = src_map.get(h.key, "")
            if persona:
                if src and src != persona:
                    label = (persona_labels or {}).get(src) or src
                    t = f"[「{label}」视角的记录，仅供参考，不是你亲身经历的] {t}"
                elif not src:
                    t = f"[旧记录，来源人格不明，仅供参考] {t}"
            out.append(t)
        return out

    # ------------------------------------------------------------ 遗忘
    def forget(self, key: str) -> dict:
        """清空某个记忆键。**连带清掉它派生出的图谱条目**。

        图谱条目是由记忆固化时抽出来的，只清记忆会留下孤儿节点/边 ——
        召回时图谱那一路仍会把"已经忘掉"的内容带回来，用户会看到
        `/persona forget` 之后记忆还在。
        （`GraphStore.forget_memory` 本来就是干这个的，之前没有任何生产调用点。）
        """
        dropped = 0
        try:
            for atom in list(self.store.load(key)):
                mid = (atom.metadata or {}).get("source_memory_id")
                if mid:
                    dropped += self.graph.forget_memory(int(mid))
            # 历史脱节兜底：某些图谱条目的 source_memory_id 与其派生原子的
            # 对不上（旧实现两路 mid 不同源），按原子 mid 删永远匹配不到，
            # 删完记忆图里还留着条目，图谱路照样召回 —— 用户会看到
            # "删了记忆 AI 还记得"。改成再按记忆键删一次，两路都兜住。
            dropped += self.graph.forget_session(key)
            if dropped:
                self.graph.save()
        except Exception:
            pass
        n = self.store.clear(key)
        self._pending.pop(key, None)
        self._pending_dirty = True
        self._save_pending()
        return {"atoms": n, "graph_dropped": dropped}

    def forget_atom(self, key: str, atom_id: str) -> dict:
        """删除**单条**记忆原子，并连带清掉它派生出的图谱条目。

        与 `forget(key)`（清空整个记忆键）不同 —— 这里给 WebUI 勾选删除用。
        图谱那一路必须跟着删：条目的 `source_memory_id` 由这条原子的固化产出，
        只删记忆会留下孤儿条目，图谱路照样把内容召回回来。

        注意这里**只按 `source_memory_id`** 联动，不能像 `forget()` 那样再兜底
        删整个 `session_id` —— 那会把同会话其它记忆派生出的图谱一起清掉。
        """
        atom = None
        for a in self.store.load(key):
            if a.id == atom_id:
                atom = a
                break
        if atom is None:
            return {"deleted": 0, "graph_dropped": 0}
        dropped = 0
        try:
            mid = (atom.metadata or {}).get("source_memory_id")
            if mid:
                dropped = int(self.graph.forget_memory(int(mid)) or 0)
                if dropped:
                    self.graph.save()
        except Exception:
            dropped = 0
        ok = self.store.forget(key, atom_id)
        return {"deleted": 1 if ok else 0, "graph_dropped": dropped}

    async def edit_atom(self, key: str, atom_id: str, text: str,
                        *, max_chars: int = 4000) -> dict:
        """改写单条记忆的正文（WebUI 手工纠错）。

        三件事必须一起做，少一件就会出现"改了但还查得到旧内容"：
          ① **重新算向量** —— 只改 `text` 不改 `vec`，召回会拿旧向量算相似度，
             结果与实际内容完全对不上，比不改更糟；
          ② 同步 `metadata.summary` / `canonical_summary` —— 图谱条目的正文是
             `Fact: {事实}. Summary: {摘要}`，摘要不改就还是旧内容；
          ③ 删掉旧图谱条目再按新正文重建（`key_facts` 一并换成新正文），
             否则图谱路会把旧记忆原样召回来。

        embedding 不可用时**拒绝写入**：宁可这次改不成，也不要留下一条
        向量与正文不符的记忆。成功返回 `{id, text, graph_dropped}`，
        失败返回 `{"error": ...}`（调用方按 `error` 判）。
        """
        new_text = (text or "").strip()
        if not new_text:
            return {"error": "empty"}
        if len(new_text) > max_chars:
            return {"error": "too_long", "limit": max_chars}
        atom = None
        for a in self.store.load(key):
            if a.id == atom_id:
                atom = a
                break
        if atom is None:
            return {"error": "not_found"}
        if new_text == (atom.text or "").strip():
            return {"error": "unchanged"}
        if not self.available:
            return {"error": "embedding_unavailable"}
        vec = await self.embedder.embed(new_text)
        if not vec:
            return {"error": "embedding_failed"}

        mid = (atom.metadata or {}).get("source_memory_id")
        dropped = 0
        if mid:
            try:
                dropped = int(self.graph.forget_memory(int(mid)) or 0)
            except Exception:
                dropped = 0

        atom.text = new_text
        atom.vec = to_f32(vec)
        atom.metadata["summary"] = new_text
        atom.metadata["canonical_summary"] = new_text
        atom.metadata["key_facts"] = [new_text]
        atom.metadata["manual_edit"] = True
        atom.metadata["edited_at"] = time.time()
        self.store.touch(key, atom)
        self.store.save(key)

        if mid:
            try:
                await self._grow_graph(int(mid), new_text, atom.metadata)
            except Exception as e:
                self.stats_counters["last_error"] = repr(e)
                try:
                    self.graph.save()      # 至少把"删旧条目"这一步落盘
                except Exception:
                    pass
        self.stats_counters["edited"] += 1
        return {"id": atom.id, "text": atom.text, "graph_dropped": dropped}

    def drop_pending(self, key: str) -> int:
        """丢弃某记忆键里**尚未固化**的轮次（供 /reset 清上下文）。

        不清的话，reset 前攒下的对话会在下一次固化时被写成记忆 ——
        用户会看到"reset 了却还被记得"。返回丢弃的轮数。
        """
        turns = self._pending.pop(key, None) or []
        if turns:
            self._pending_dirty = True
            self._save_pending()
        return len(turns)

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        gs = self.graph.stats()
        return {
            "enabled": self.enabled,
            "available": self.available,
            "top_k": self._int_cfg("memory_top_k", 5),
            "min_score": self._cfg("memory_min_score", 0.35),
            "half_life_days": self._cfg("memory_decay_days", 30.0),
            "consolidate_turns": self._int_cfg("memory_consolidate_turns", 10),
            **self.store.stats(),
            **self.stats_counters,
            "graph": gs,
            **self.embedder.stats(),
            **self.summarizer.report(),
        }

    def flush(self) -> None:
        self.store.save_all()
        self.graph.save()
        self._save_pending()
        self.embedder.flush()


def build_default(context: Any = None, config: Any = None,
                  inst: str = "") -> MemoryService:
    """按插件配置装配记忆服务。

    `inst` 非空时使用该实例的独立库（`instances/<inst>/memory/`）；
    为空时退回旧版单库（仅供迁移脚本与老测试使用）。
    """
    def cfg(key: str, default: Any) -> Any:
        try:
            v = config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    root = inst_memory_dir(inst) if inst else MEMORY_DIR
    root.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(root)
    # 向量缓存跟着 store 的根走：否则测试注入临时 store 时缓存仍写真实
    # plugin_data（违反 memory/service.py 自己写下的"一个根只能有一个来源"）
    base = Path(getattr(store, "root", None) or root)
    embedder = Embedder(context,
                        provider_id=str(cfg("provider_embedding", "") or ""),
                        db_path=base / "memory.db")
    return MemoryService(embedder, store, config=config, context=context)
