# -*- coding: utf-8 -*-
"""用户印象：按轮数阈值在后台自动总结。

与旧实现的兼容
--------------
存储沿用旧 `lili_state` 的形状，字段逐个一致：

    {"sfw": "", "nsfw": "", "sfw_custom": "", "nsfw_custom": "",
     "updated": 0, "msg_count": 1}

所以迁移过来的印象文件可以直接读，不需要转换。
`*_custom` 是人工覆写位，**总结任务永不写它**（否则会覆盖手工编辑）。

为什么分 sfw / nsfw 两份
------------------------
印象会被注入到动态段。旧实现把两套分开，是因为有些场合不该注入 NSFW
印象（例如群里其他人也在看）。保留这个切分，注入侧按配置选择。

同样遵守"唯一生成入口"的边界：本模块做的是**摘要**，用独立 provider、
不碰主对话 `req`，且**fail-open**（失败就不更新，不影响聊天）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .judge import resolve_subagent_provider

import logging

# 用 stdlib logger 而不是 `from astrbot.api import logger`：
# `core/*` 必须能**脱离框架**被 import（测试就是这么用的）。
# 名字取 "astrbot"，输出会并进框架自己的 logger，级别/格式一致。
logger = logging.getLogger("astrbot")

SFW_SYSTEM = (
    "你在为一个聊天机器人整理「对某个用户的印象」。"
    "只根据给定对话记录，输出一段 60~120 字的中文印象，供机器人在后续对话中参考。\n"
    "要求：\n"
    "1. 写这个人的**话题偏好、交流习惯、性格倾向**，以及机器人该如何对待他\n"
    "2. 只写有依据的内容，不要编造、不要脑补身份\n"
    "3. 不要记录好感度/情绪等状态数值（这些由系统单独管理）\n"
    "4. 直接输出印象正文，不要标题、不要解释、不要列表符号"
)

NSFW_SYSTEM = (
    "你在为一个聊天机器人整理「对某个用户的私密向印象」。"
    "只根据给定对话记录，输出一段 40~100 字的中文印象。\n"
    "要求：\n"
    "1. 只写与亲密互动有关的偏好与边界（能接受的、明显回避的）\n"
    "2. 记录不存在的暗示属于编造，绝对不要写\n"
    "3. 若记录里没有相关内容，只输出：无\n"
    "4. 直接输出正文，不要标题、不要解释"
)


def build_impression_prompt(display_name: str, log: list[dict],
                            limit: int = 40) -> str:
    """把状态日志折成给摘要模型的输入。"""
    lines = []
    for e in (log or [])[-limit:]:
        role = e.get("role")
        if role == "user":
            txt = str(e.get("content", ""))[:200]
            if txt:
                lines.append(f"用户：{txt}")
        else:
            txt = str(e.get("lili_thought") or e.get("content") or "")[:200]
            if txt:
                lines.append(f"{display_name}：{txt}")
    return "\n".join(lines) or "（无对话记录）"


class ImpressionSummarizer:
    """印象自动总结。"""

    def __init__(self, context: Any, config: Any,
                 state_store: Any, *, threshold: int = 20) -> None:
        self.context = context
        self.config = config
        self.store = state_store
        self.threshold = max(1, threshold)
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats = {"checked": 0, "updated": 0, "skipped": 0,
                      "errors": 0, "last_error": ""}

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _lock(self, uid: str) -> asyncio.Lock:
        lk = self._locks.get(uid)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[uid] = lk
        return lk

    def due(self, uid: str, msg_count: int) -> bool:
        """这一轮是否到了该更新印象的时候。

        **按轮数取模判定**，与旧 lili_state 一致
        （旧实现：`interval <= 0 or msg_count % interval == 0`）。

        为什么不能用"和已存轮数比大小"
        ------------------------------
        `on_llm_response` 的顺序是**先把计数 +1 落盘、再问要不要总结**，
        所以文件里的 `msg_count` 和传进来的 `msg_count` 永远是同一个值，
        `msg_count - stored >= threshold` 恒等于 `0 >= threshold` ——
        印象一辈子都不会自动更新。
        （这不是推测：生产日志里「印象总结·触发」出现 0 次，
        而同一批加进去的「记忆总结」出现 8 次。）

        取模判定不依赖调用顺序，且计数是**持久化**的，重启后从已存轮数
        接着数，不会把同一批轮数再触发一遍。
        """
        if not bool(self._cfg("enable_impression", True)):
            return False
        if not uid or int(msg_count or 0) <= 0:
            return False
        return int(msg_count) % self.threshold == 0

    async def maybe_update(self, umo: str, uid: str, display_name: str,
                           log: list[dict], msg_count: int) -> bool:
        """到点就总结。返回是否真的更新了。"""
        self.stats["checked"] += 1
        if not self.due(uid, msg_count):
            self.stats["skipped"] += 1
            return False
        logger.info(
            f"[chatcore_neo] 印象总结·触发 umo={umo!r} uid={uid!r} "
            f"累计 {msg_count} 条（到点，开始总结）")
        _t0 = time.time()
        async with self._lock(uid):
            # 双重检查：等锁期间可能已被另一条消息更新
            if not self.due(uid, msg_count):
                self.stats["skipped"] += 1
                return False
            try:
                text = build_impression_prompt(display_name, log)
                timeout = float(self._cfg("impression_timeout", 30) or 30)
                model = str(self._cfg("impression_model", "") or "")

                sfw = await self._ask(umo, model, SFW_SYSTEM, text, timeout)
                nsfw = ""
                if bool(self._cfg("impression_nsfw", True)):
                    nsfw = await self._ask(umo, model, NSFW_SYSTEM, text, timeout)
                    if nsfw.strip() in ("无", "（无）", "None"):
                        nsfw = ""

                if not sfw.strip():
                    self.stats["skipped"] += 1
                    return False

                # 按 impression_max_chars 截断（配置项此前是空壳）
                try:
                    cap = int(self._cfg("impression_max_chars", 300) or 300)
                except (TypeError, ValueError):
                    cap = 300
                if cap > 0:
                    sfw = sfw[:cap]
                    nsfw = nsfw[:cap]

                imp = self.store.load_impression(uid)
                imp["sfw"] = sfw.strip()
                if nsfw.strip():
                    imp["nsfw"] = nsfw.strip()
                # 人工覆写位不动
                imp.setdefault("sfw_custom", "")
                imp.setdefault("nsfw_custom", "")
                imp["updated"] = int(time.time())
                imp["msg_count"] = msg_count
                self.store.save_impression(uid, imp)
                self.stats["updated"] += 1
                logger.info(
                    f"[chatcore_neo] 印象总结·完成 umo={umo!r} uid={uid!r} "
                    f"累计 {msg_count} 条 sfw={len(sfw)}字 nsfw={len(nsfw)}字 "
                    f"耗时 {(time.time()-_t0)*1000:.0f}ms")
                return True
            except Exception as e:
                # fail-open：印象更新失败不影响聊天
                self.stats["errors"] += 1
                self.stats["last_error"] = repr(e)
                logger.info(
                    f"[chatcore_neo] 印象总结·失败（不影响聊天）uid={uid!r} "
                    f"耗时 {(time.time()-_t0)*1000:.0f}ms 错误={e!r}")
                return False

    async def _ask(self, umo: str, model: str, system: str, user: str,
                   timeout: float) -> str:
        prov = resolve_subagent_provider(self.context, model, umo)
        if prov is None or not hasattr(prov, "text_chat"):
            return ""
        resp = await asyncio.wait_for(
            prov.text_chat(system_prompt=system, prompt=user, temperature=0.3),
            timeout=timeout)
        return (getattr(resp, "completion_text", "") or "").strip()

    def report(self) -> dict:
        return {**self.stats, "threshold": self.threshold,
                "locks": len(self._locks)}
