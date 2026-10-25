# -*- coding: utf-8 -*-
"""记忆固化：把一段对话窗口总结成一条**带元数据的记忆**。

为什么必须有这一步
------------------
图谱不是直接从对话长出来的 —— 它需要 `topics` / `participants` / `key_facts`
这些**结构化字段**，而它们只能由 LLM 从原始对话里读出来。
旧库 1495 条 documents / 7354 条 atoms / 13.4 万条图谱条目，
全都是这条固化链路的产物。

固化产出（一条"记忆文档"）：

    {
      "summary":      "这段对话在讲什么（1~3 句）",
      "topics":       ["话题标签", ...]        # → topic 节点
      "key_facts":    ["可独立成立的事实", ...] # → fact 节点
      "participants": [{"display_name","identity_key"}, ...]  # → person 节点
      "importance":   0.0~1.0
    }

图谱抽取是这组字段的**确定性投影**（`core/graph/extract.py`），不需要第二次 LLM。

失败一律降级：总结失败就把原始文本当 summary、无 topics/facts，
图谱仍会生成一个 `summary` 节点兜底。**记忆不会因为总结失败而丢失。**
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..judge import resolve_subagent_provider

import logging

# 用 stdlib logger 而不是 `from astrbot.api import logger`：
# `core/*` 必须能**脱离框架**被 import（测试就是这么用的）。
# 名字取 "astrbot"，输出会并进框架自己的 logger，级别/格式一致。
logger = logging.getLogger("astrbot")
import time

SYSTEM = (
    "你是「对话记忆固化器」，同时是你所扮演角色的记忆化身。"
    "给定一段群聊/私聊记录（记录中标记为 `bot：` 的是你本人，"
    "即你所扮演的角色的发言），把这段对话总结成一条结构化记忆，"
    "供长期记忆库与知识图谱使用。\n\n"
    "记忆必须用**所扮演角色的第一人称**写，而且要**带着角色的感情**写，"
    "不要写成冷冰冰的事实清单。summary 要像角色自己此刻在回忆这段经历："
    "带上当时的情绪、感受、语气与态度（开心、害羞、无奈、在意、别扭……），"
    "是「经历过的回忆」，不是「登记在案的事件」。\n"
    "示范（带感情）：\n"
    "  - 好：他今天夸我厉害，夸得我心花怒放，嘴上说着不好意思，其实心里可美了\n"
    "  - 差：他夸了我厉害。我回复了谢谢\n"
    "key_facts 同样带感情视角落笔（如「他夸我厉害，我嘴上谦虚心里却甜丝丝的」"
    "「她说周末要来找我玩，我可盼着啦」），不要只写谁做了什么。\n\n"
    "输出格式（严格遵守）：一个 JSON 对象，不要代码块、不要解释：\n"
    "{\n"
    '  "summary": "这段对话在讲什么，1~3 句，以我（角色）的第一人称带感情地回忆",\n'
    '  "topics": ["话题标签1", "话题标签2"],\n'
    '  "key_facts": ["带感情视角、可独立成立的事实1", "事实2"],\n'
    '  "kind": "document",\n'
    '  "importance": 0.5\n'
    "}\n\n"
    "要求：\n"
    "- `topics`：2~6 个短标签（如「SD画图」「音游」「周末安排」），不要整句\n"
    "- `key_facts`：1~8 条**能脱离上下文独立成立**的事实陈述，"
    "带「我」的感情与态度；但要具体，别空泛抒情，谁做了什么要写清\n"
    "- `kind`：这条记忆的性质，四选一（决定它被记得多久）：\n"
    "  ・`preference` 对方的稳定喜好/厌恶/习惯（喜欢什么、讨厌什么、作息习惯）\n"
    "  ・`fact` 关于某人/某事的稳定事实（住哪、做什么工作、身体情况、叫什么）\n"
    "  ・`dialog` 一次性的闲聊、当时的情绪与打闹，过后意义不大\n"
    "  ・`document` 一段事件的完整记录或总结，以上都不像时的默认值\n"
    "- `importance`：0~1，日常闲聊 0.3~0.5，重要事件/偏好/承诺 0.7~0.9\n"
    "- 只依据给定记录，**不要编造**没出现过的信息\n"
    "- 纯寒暄无实质内容的，`key_facts` 可以为空数组，但 `summary` 要写"
)

# `kind` 的合法取值（决定半衰期，见 `store.KIND_HALF_LIFE`）
KINDS = ("document", "dialog", "fact", "preference")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_consolidation(raw: str) -> dict | None:
    """解析固化结果；解析不出来返回 None。"""
    txt = (raw or "").strip()
    if not txt:
        return None

    def _coerce(obj: Any) -> dict | None:
        if not isinstance(obj, dict):
            return None
        summary = str(obj.get("summary") or "").strip()
        topics = [str(x).strip() for x in (obj.get("topics") or [])
                  if str(x).strip()]
        facts = [str(x).strip() for x in (obj.get("key_facts") or [])
                 if str(x).strip()]
        try:
            imp = float(obj.get("importance", 0.5))
        except (TypeError, ValueError):
            imp = 0.5
        kind = str(obj.get("kind") or "").strip().lower()
        if kind not in KINDS:
            # 认不出来就退回 document —— 半衰期倍率 1.0，等价于旧行为
            kind = "document"
        if not summary and not facts and not topics:
            return None
        return {"summary": summary, "topics": topics[:6],
                "key_facts": facts[:8], "kind": kind,
                "importance": max(0.0, min(1.0, imp))}

    try:
        got = _coerce(json.loads(txt))
        if got:
            return got
    except Exception:
        pass
    m = _FENCE.search(txt)
    if m:
        try:
            got = _coerce(json.loads(m.group(1).strip()))
            if got:
                return got
        except Exception:
            pass
    i, j = txt.find("{"), txt.rfind("}")
    if 0 <= i < j:
        try:
            got = _coerce(json.loads(txt[i:j + 1]))
            if got:
                return got
        except Exception:
            pass
    return None


def fallback_consolidation(text: str, participants: list[dict] | None = None
                           ) -> dict:
    """总结不可用时的降级：原文当 summary，facts 用首句兜底。"""
    t = (text or "").strip()
    head = t[:300]
    facts = [ln.strip()[:200] for ln in t.splitlines() if len(ln.strip()) >= 4][:3]
    # 兜底：连一行都不够长时，把摘要本身当一条事实。
    # 图谱需要至少一个 fact 节点才有输入，宁可粗糙也不能空手。
    if not facts and head:
        facts = [head]
    return {"summary": head, "topics": [], "key_facts": facts,
            "kind": "document", "importance": 0.4, "degraded": True}


class MemorySummarizer:
    """把对话窗口固化成一条带元数据的记忆。"""

    def __init__(self, context: Any, config: Any) -> None:
        self.context = context
        self.config = config
        self.stats = {"calls": 0, "ok": 0, "degraded": 0, "errors": 0,
                      "last_error": ""}

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    async def consolidate(self, window_text: str,
                          participants: list[dict] | None = None,
                          umo: str = "", persona: str = "") -> dict:
        """返回固化结果字典（永不为 None，失败走降级）。

        `persona`：角色身份描述（如「你扮演的角色是星绘……」），
        让总结以所扮演角色的第一人称落笔；为空时退化为通用口吻。
        """
        text = (window_text or "").strip()
        if not text:
            return fallback_consolidation("")

        self.stats["calls"] += 1
        _t0 = time.time()
        logger.info(
            f"[chatcore_neo] 记忆总结·开始 umo={umo!r} 窗口 {len(text)} 字 "
            f"参与人={len(participants or [])}")
        try:
            model = str(self._cfg("memory_summary_model", "") or "")
            timeout = float(self._cfg("memory_summary_timeout", 30) or 30)
            # umo 必须透传：模型留空时要靠它解析"会话主模型"
            prov = resolve_subagent_provider(self.context, model, umo)
            if prov is None or not hasattr(prov, "text_chat"):
                raise RuntimeError("无可用 provider")
            cap = int(self._cfg("memory_summary_max_chars", 4000) or 4000)
            user = ""
            if persona:
                user += f"{persona}\n\n"
            user += (f"【对话记录】\n{text[:cap]}\n\n"
                     "请输出 JSON 对象：")
            resp = await asyncio.wait_for(
                prov.text_chat(system_prompt=SYSTEM, prompt=user, temperature=0),
                timeout=timeout)
            got = parse_consolidation(getattr(resp, "completion_text", "") or "")
            if not got:
                raise ValueError("输出无法解析为 JSON")
            got["degraded"] = False
            self.stats["ok"] += 1
            logger.info(
                f"[chatcore_neo] 记忆总结·完成 umo={umo!r} "
                f"事实 {len(got.get('key_facts') or [])} 条 / "
                f"话题 {len(got.get('topics') or [])} 个 "
                f"耗时 {(time.time()-_t0)*1000:.0f}ms")
            return got
        except asyncio.TimeoutError:
            self.stats["errors"] += 1
            self.stats["degraded"] += 1
            self.stats["last_error"] = "timeout"
            logger.info(
                f"[chatcore_neo] 记忆总结·超时（降级规则切分）umo={umo!r} "
                f"窗口 {len(text)} 字 耗时 {(time.time()-_t0)*1000:.0f}ms")
            return fallback_consolidation(text, participants)
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["degraded"] += 1
            self.stats["last_error"] = repr(e)
            logger.info(
                f"[chatcore_neo] 记忆总结·异常（降级规则切分）umo={umo!r} "
                f"窗口 {len(text)} 字 耗时 {(time.time()-_t0)*1000:.0f}ms "
                f"错误={e!r}")
            return fallback_consolidation(text, participants)

    def report(self) -> dict:
        return dict(self.stats)
