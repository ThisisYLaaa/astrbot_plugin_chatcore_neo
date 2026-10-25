# -*- coding: utf-8 -*-
"""Embedding 封装 + 记忆层门面。

关于"唯一生成入口"这条硬性约束
-------------------------------
约束 1 禁的是**对话生成**绕过管道。Embedding 不是生成，但为了不留下
模糊地带，这里明确两点：
  * 本模块**只在后台任务里**使用 provider，绝不碰主对话的 `req`
  * 由装配器读取的只有 `recall()` 返回的**纯文本**，provider 细节不外泄

`provider_manager` 通过 AstrBot 注入的 Context 获取，不做任何反射查找。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from array import array
from pathlib import Path
from typing import Any

from ..db import connection, open_ready, to_f32, unpack_vec
from ..paths import ROOT
from .store import MemoryAtom, MemoryStore, cosine, decayed_score

# 缓存上限。向量缓存与记忆原子**同库**（memory.db），
# 所以内存里那个 dict 只是这一层的读缓存，不是唯一副本。
MAX_CACHE = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embed_cache (
    key TEXT PRIMARY KEY,
    vec BLOB NOT NULL,
    ts  REAL NOT NULL DEFAULT 0
);
"""


def provider_pid(p: Any) -> str:
    """取 provider 的 id（AstrBot 上 provider_config["id"]）。"""
    cfg = getattr(p, "provider_config", None)
    if isinstance(cfg, dict):
        return str(cfg.get("id") or "")
    return ""


def provider_model(p: Any) -> str:
    """取 provider 的模型名。

    embedding provider 的模型名在 `provider_config["embedding_model"]`
    （实配：id=BAAI_embedding, embedding_model=BAAI/bge-m3），
    **不是** `model_config.model` —— 旧写法在真实环境恒返回空串，
    让下面的"按模型名匹配"变成死代码。
    """
    cfg = getattr(p, "provider_config", None)
    if not isinstance(cfg, dict):
        return ""
    for key in ("embedding_model", "model", "tts_model", "model_name"):
        v = cfg.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    mc = cfg.get("model_config")
    if isinstance(mc, dict) and mc.get("model"):
        return str(mc["model"])
    return ""


def pick_provider(context: Any, want: str, *, kind: str = "embedding") -> Any:
    """按 kind 选一个 provider 实例。

    `kind="embedding"` → `Context.get_all_embedding_providers()`
    `kind="tts"`       → `Context.get_all_tts_providers()`

    只用 Context 的同步方法（ProviderManager 上的同名方法是 async，
    直接调会拿到协程；`get_all_providers` 在 manager 上根本不存在）。

    `want` 的匹配是**容错的**：用户很可能把模型名填进 provider id 一栏
    （实测配置里就是 `Qwen/Qwen3-Embedding-8B`，而真实 id 是
    `Qwen3-Embedding-8B`）。依次尝试：
      ① 精确 id  ② 忽略大小写  ③ 双向包含  ④ 模型名相等/包含
    都不中则回退到第一个同类 provider，并把原因写进 last_error
    —— **回退而不是失败**，否则用户会以为"记忆功能坏了"却看不到原因。
    """
    pick_provider.last_error = ""      # 每次解析前清空，供调用方读取
    if kind == "tts":
        insts = list(context.get_all_tts_providers() or [])
        attr = "get_audio"
    else:
        insts = list(context.get_all_embedding_providers() or [])
        attr = "get_embedding"

    def usable(p: Any) -> bool:
        return p is not None and hasattr(p, attr)

    want = (want or "").strip()
    if want and insts:
        low = want.lower()
        for p in insts:                                   # ① 精确
            if provider_pid(p) == want and usable(p):
                return p
        for p in insts:                                   # ② 忽略大小写
            if provider_pid(p).lower() == low and usable(p):
                return p
        for p in insts:                                   # ③ 双向包含
            pid = provider_pid(p).lower()
            if pid and (pid in low or low in pid) and usable(p):
                return p
        for p in insts:                                   # ④ 模型名
            md = provider_model(p).lower()
            if md and (md == low or md in low or low in md) and usable(p):
                return p

    good = [p for p in insts if usable(p)]
    if good:
        if want:
            # 回退必须留下原因，否则用户在管理页看不到"你填的 id 没匹配上"
            pick_provider.last_error = (
                f"指定的 {kind} provider {want!r} 没匹配到，"
                f"已回退到 {provider_pid(good[0])!r}")
        return good[0]

    # 兜底：TTS 还能问 Context 要"当前使用的"
    if kind == "tts":
        try:
            p = context.get_using_tts_provider()
            if usable(p):
                return p
        except Exception:
            pass
    return None


class Embedder:
    """把文本转成向量。provider 缺失或调用失败时**降级为不可用**，
    记忆功能整体静默关闭，而不是让聊天崩掉。"""

    def __init__(self, context: Any = None, provider_id: str = "",
                 db_path: Path | None = None) -> None:
        self.context = context
        self.provider_id = (provider_id or "").strip()
        # 向量缓存与记忆原子同库。默认值指向真实数据根，
        # 所以测试必须显式传 db_path（见 tests/test_providers.py 的说明）。
        self.db_path = Path(db_path) if db_path else (
            ROOT / "memory" / "memory.db")
        self._provider: Any = None
        self._cache: dict[str, array] = {}
        self._loaded = False
        self._ready = False
        self._failed = 0
        self._last_error = ""

    # ------------------------------------------------------------ provider
    def _resolve(self) -> Any:
        if self._provider is not None:
            return self._provider
        if self.context is None:
            return None
        try:
            pick_provider.last_error = ""
            self._provider = pick_provider(
                self.context, self.provider_id, kind="embedding")
            reason = getattr(pick_provider, "last_error", "")
            if reason:
                self._last_error = reason
        except Exception as e:
            self._last_error = repr(e)
            self._provider = None
        # openai SDK 默认 max_retries=2 × provider timeout(20s)：keepalive
        # 死连接复用超时后，重试链最坏拖 60s 才返回（线上实测"20s 超时 →
        # retry → 1s 内 200"出现 36 次，召回被卡 13~38s 全是这个）。
        # embedding 是召回前缀，卡它=卡整条回复，把重试压到 1 次：宁可
        # 这一轮没记忆（召回空列表本来就是合法降级），也不要干等。
        client = getattr(self._provider, "client", None)
        if client is not None and getattr(client, "max_retries", None) not in (0, 1):
            try:
                client.max_retries = 1
            except Exception:
                pass
        return self._provider

    @property
    def available(self) -> bool:
        if self._failed >= 5:
            return False
        return self._resolve() is not None

    # ------------------------------------------------------------ 缓存
    def _ensure_cache(self) -> None:
        """懒加载缓存。

        **不在 `__init__` 里加载**：默认路径指向真实 plugin_data，
        而 `Embedder(context=None)` 这种"只想知道可不可用"的构造到处都是，
        一构造就去读十几 MB 是白花的。真正要用缓存时再开库。
        """
        if self._loaded:
            return
        self._loaded = True
        self._ready = open_ready(self.db_path, _SCHEMA)
        if not self._ready:
            return
        try:
            with connection(self.db_path) as con:
                rows = con.execute(
                    "SELECT key, vec FROM embed_cache ORDER BY ts").fetchall()
            self._cache = {r["key"]: unpack_vec(r["vec"]) for r in rows}
        except Exception:
            self._cache = {}

    def cache_put(self, key: str, vec) -> None:
        """把一条向量直接写进缓存（迁移 / 预热用）。

        `key` 是 `Embedder.key_of(text)` 的结果，缓存里只存它，不存原文。
        """
        self._ensure_cache()
        stored = to_f32(vec)
        if not len(stored):
            return
        self._cache[key] = stored
        self._put(key, stored)

    def _put(self, key: str, vec: array) -> None:
        """单条即写即落 —— 不再攒 20 条然后把整份缓存重写一遍。"""
        if not self._ready:
            return
        try:
            with connection(self.db_path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO embed_cache (key, vec, ts)"
                    " VALUES (?,?,?)", (key, vec.tobytes(), time.time()))
        except Exception:
            pass
        if len(self._cache) > MAX_CACHE:
            self._trim()

    def _trim(self) -> None:
        """超出上限就丢最旧的（`_cache` 按 ts 升序装载，新条目追加在尾部）。"""
        excess = len(self._cache) - MAX_CACHE
        if excess <= 0:
            return
        for key in list(self._cache)[:excess]:
            self._cache.pop(key, None)
        if not self._ready:
            return
        try:
            with connection(self.db_path) as con:
                con.execute(
                    "DELETE FROM embed_cache WHERE key IN ("
                    " SELECT key FROM embed_cache ORDER BY ts DESC"
                    " LIMIT -1 OFFSET ?)", (MAX_CACHE,))
        except Exception:
            pass

    @staticmethod
    def key_of(text: str) -> str:
        """缓存键：sha256(原文) 前 32 位。"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------ 主接口
    async def embed(self, text: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            return []
        key = self.key_of(text)
        self._ensure_cache()
        hit = self._cache.get(key)
        if hit:
            return list(hit)
        p = self._resolve()
        if p is None:
            return []
        try:
            vec = await self._call(p, text)
        except Exception as e:
            self._failed += 1
            self._last_error = repr(e)
            return []
        if vec:
            # 成功即复位：`_failed` 是**连续**失败计数，不是累计。
            # 不复位的话，网络抖 5 次就会把 available 永久钉成 False，
            # 记忆要到重启才恢复 —— 用户只会看到"记忆功能莫名其妙坏了"。
            self._failed = 0
            self._last_error = ""
            self._cache[key] = to_f32(vec)
            self._put(key, self._cache[key])
        return vec

    # ------------------------------------------------------------ 并发去重
    # 同一文本的并发 embed 合并成一次调用。真实场景：主对话召回与图谱路
    # 几乎同时 embed 同一条查询（recall_texts 里两路各调一次 embed(query)），
    # 而 openai SDK 的 /embeddings 在网络抖动时会 20s 超时重试（线上实测
    # 36 次 Retry，单次最坏拖 40s）——两次并发的重试叠起来就是把回复卡死
    # 半分钟。in-flight 合并后同一查询永远只打一发请求。
    _inflight: dict[str, asyncio.Task] = {}

    async def embed_dedup(self, text: str) -> list[float]:
        """带并发去重的 embed：相同文本的并发调用共享同一次请求。"""
        text = (text or "").strip()
        if not text:
            return []
        key = self.key_of(text)
        self._ensure_cache()
        hit = self._cache.get(key)
        if hit:
            return list(hit)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self.embed(text))
            self._inflight[key] = task
            try:
                return await task
            finally:
                self._inflight.pop(key, None)
        return await asyncio.shield(task)

    @staticmethod
    def _as_vec(r: Any) -> list[float]:
        """把 provider 的返回值规整成 list[float]（兼容 [[...]] 包装）。"""
        if not isinstance(r, list) or not r:
            return []
        if isinstance(r[0], (int, float)):
            return [float(x) for x in r]
        if isinstance(r[0], list) and r[0] and \
                isinstance(r[0][0], (int, float)):
            return [float(x) for x in r[0]]
        return []

    @staticmethod
    async def _call(provider: Any, text: str) -> list[float]:
        """调 provider 拿向量。

        按 `get_embedding` → `embed` → `get_embeddings` 依次尝试（不同版本
        命名不同），但**每个名字只调一次**，并且把真实异常带出去：
        旧实现在末尾又兜底调了一次 `get_embedding`，导致失败时每次 embed
        要调 provider 两次，而且真正的原因被循环里的 `continue` 吞掉，
        对外只剩一句"没有可用的 embedding 接口"，无法排障。
        """
        last_exc: Exception | None = None
        for meth in ("get_embedding", "embed", "get_embeddings"):
            fn = getattr(provider, meth, None)
            if fn is None:
                continue
            try:
                r = fn(text)
                if asyncio.iscoroutine(r):
                    r = await r
            except Exception as e:
                last_exc = e
                continue
            vec = Embedder._as_vec(r)
            if vec:
                return vec
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("provider 没有可用的 embedding 接口")

    def purge_stale_cache(self, dim: int) -> int:
        """删掉缓存里维度与 `dim` 不符的向量，返回删掉的条数。

        换 embedding 模型后必须做这一步：`embed()` 会拿 `sha256(原文)` 命中
        **旧维度**的缓存条目并原样返回，后台重编码就会"编了等于没编"。
        缓存只是缓存，删掉不影响任何数据（下次会重新调 provider）。
        """
        if dim <= 0:
            return 0
        self._ensure_cache()
        bad = [k for k, v in self._cache.items() if len(v) != dim]
        for k in bad:
            self._cache.pop(k, None)
        if self._ready:
            try:
                with connection(self.db_path) as con:
                    con.execute("DELETE FROM embed_cache WHERE length(vec) != ?",
                                (int(dim) * 4,))
            except Exception:
                pass
        return len(bad)

    def flush(self) -> None:
        """写入已是即时的，这里只为兼容调用点保留（不再有"攒着待写"的状态）。"""

    def stats(self) -> dict:
        self._ensure_cache()
        return {"available": self.available, "cached": len(self._cache),
                "failures": self._failed, "last_error": self._last_error,
                "provider": self.provider_id or "(auto)"}


# 说明：记忆服务（累积/固化/双路召回）已移到 `core/memory/service.py`。
# 旧版这里有一个「每轮存一条原子」的 MemoryService，已被固化式实现取代 ——
# 原始轮次信息密度低、噪音大，直接向量召回质量差，且图谱没有输入。
