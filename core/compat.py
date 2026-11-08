# -*- coding: utf-8 -*-
"""框架兼容层：合成事件重入原生管道（方案 3.6）。

为什么必须走合成事件
--------------------
旧架构有两个 LLM 调用路径：原生管道 + `proactive_chat` 自建链路。
主动消息走后者，**绕过所有钩子**，于是每个基于钩子的功能都要在
`proactive_chat` 里做一份补偿（"如果类名是 LiliStatePlugin 就……"）。
这是整个旧系统最贵的结构性缺陷。

合成事件把主动消息送回**同一条原生管道**：
    proactive 触发 → 造一个 AstrMessageEvent → `get_event_queue().put_nowait()`
    → 管道照常跑 9 阶段 → 本插件的注入/状态更新/记忆全部自动发生

于是"补偿点"这一整类代码消失。

本模块的职责
------------
1. 造/复用事件模板（缺模板时从 `storage/event_templates.json` 取，
   而不是像参考实现那样直接放弃）
2. 给合成事件打 `_proactive_simulated` 标记，让自身钩子跳过"防死循环"判定
3. 尊重 `gutoken` 的 `_gutoken_active` / `_gutoken_text` extras
   （token 估算请求不写日志、不产生副作用）
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import EVENT_TEMPLATES

# 用 stdlib logger 而不是 `from astrbot.api import logger`：
# `core/*` 必须能**脱离框架**被 import（测试就是这么用的）。
logger = logging.getLogger("astrbot")

SIMULATED_FLAG = "_proactive_simulated"
GUTOKEN_ACTIVE = "_gutoken_active"
GUTOKEN_TEXT = "_gutoken_text"

# 事件模板只存骨架，不含正文
TEMPLATE_FIELDS = ("platform", "self_id", "bot", "sender", "group_id",
                   "session_id", "message_type")


@dataclass
class EventTemplate:
    """从真实消息里抽出的最小骨架，用于日后合成同类事件。"""
    platform: str = ""
    self_id: str = ""
    bot: Any = None
    sender: dict = field(default_factory=dict)
    group_id: str = ""
    session_id: str = ""
    message_type: str = ""
    saved_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "self_id": str(self.self_id),
            "sender": dict(self.sender or {}),
            "group_id": str(self.group_id or ""),
            "session_id": str(self.session_id or ""),
            "message_type": self.message_type,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EventTemplate":
        return cls(
            platform=str(d.get("platform") or ""),
            self_id=str(d.get("self_id") or ""),
            sender=dict(d.get("sender") or {}),
            group_id=str(d.get("group_id") or ""),
            session_id=str(d.get("session_id") or ""),
            message_type=str(d.get("message_type") or ""),
            saved_at=float(d.get("saved_at") or 0.0),
        )


class EventTemplateStore:
    """事件模板持久化。

    参考实现的做法是"该会话从未收到过消息就回退旧路径" —— 也就是冷启动
    时主动消息不具备重入能力。持久化模板消除这个限制：只要这个会话
    曾经来过一条消息，模板就一直在。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or EVENT_TEMPLATES
        self._data: dict[str, dict] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            d = json.loads(raw) if raw else {}
            self._data = d if isinstance(d, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except Exception:
            self._data = {}

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False

    def get(self, umo: str) -> EventTemplate | None:
        d = self._data.get(umo)
        return EventTemplate.from_dict(d) if d else None

    def pop(self, umo: str) -> bool:
        """删除某会话的事件模板（管理页清理残留时用）。"""
        if umo in self._data:
            del self._data[umo]
            self._dirty = True
            self.save()
            return True
        return False

    def put(self, umo: str, tpl: EventTemplate) -> None:
        tpl.saved_at = time.time()
        self._data[umo] = tpl.to_dict()
        self._dirty = True

    def ensure_from_event(self, event: Any) -> EventTemplate | None:
        """从一条真实事件里抽骨架并存下来（已存在则跳过）。"""
        try:
            umo = event.unified_msg_origin
        except Exception:
            return None
        if not umo:
            return None
        existing = self.get(umo)
        if existing is not None:
            return existing
        # 注意：AstrMessageEvent 上**没有** platform / self_id / group_id /
        # sender 这些属性（只有 get_platform_name() / get_self_id() /
        # get_group_id() 等方法，session_id 是 property）。
        # 旧实现用 getattr(event, "platform", "") 读，永远拿到默认空串，
        # 于是存下来的骨架里 platform/self_id/group_id/sender 全是空的，
        # 日后合成的 UMO 就变成 ":GroupMessage:"。
        def _call(name: str, default: str = "") -> str:
            fn = getattr(event, name, None)
            if not callable(fn):
                return default
            try:
                return str(fn() or default)
            except Exception:
                return default

        sender_obj = None
        try:
            sender_obj = getattr(getattr(event, "message_obj", None), "sender", None)
        except Exception:
            sender_obj = None
        sender_dict = {}
        if sender_obj is not None:
            sender_dict = {
                "user_id": str(getattr(sender_obj, "user_id", "") or ""),
                "nickname": str(getattr(sender_obj, "nickname", "") or ""),
            }

        mt = getattr(event, "get_message_type", None)
        mt_name = ""
        if callable(mt):
            try:
                raw = mt()
                mt_name = str(getattr(raw, "value", "") or raw or "")
            except Exception:
                mt_name = ""

        # **必须用 get_platform_id()**：UMO 的第一段是 platform_meta.**id**
        # （本机 name='aiocqhttp' 而 id='Banyan'，UMO 里出现的是 Banyan）。
        # 旧实现读 getattr(event,"platform") 拿到的是整个 PlatformMetadata
        # 的 repr，导致重建出来的 UMO 完全对不上。
        tpl = EventTemplate(
            platform=_call("get_platform_id") or _call("get_platform_name"),
            self_id=_call("get_self_id"),
            sender=sender_dict,
            group_id=_call("get_group_id"),
            session_id=(_call("get_session_id")
                        or str(getattr(event, "session_id", "") or "")),
            message_type=mt_name,
        )
        self.put(umo, tpl)
        return tpl

    def stats(self) -> dict:
        return {"templates": len(self._data),
                "path": str(self.path)}

    def items(self) -> list[tuple[str, dict]]:
        """全部模板（umo → 骨架 dict）。用于从历史昵称里补用户表。"""
        return [(k, dict(v)) for k, v in self._data.items()]


# ---------------------------------------------------------------- 标记

def mark_simulated(event: Any, *, source: str = "proactive") -> None:
    """给合成事件打标记。本插件钩子据此跳过"防死循环/防自答"判定。"""
    try:
        setattr(event, SIMULATED_FLAG, True)
        setattr(event, "_proactive_source", source)
    except Exception:
        pass


def is_simulated(event: Any) -> bool:
    return bool(getattr(event, SIMULATED_FLAG, False))


def simulated_source(event: Any) -> str:
    """合成事件的来源标记（"proactive" / "poke" …）；真实事件返回空串。"""
    try:
        return str(getattr(event, "_proactive_source", "") or "")
    except Exception:
        return ""


def is_gutoken(event: Any) -> bool:
    """gutoken 插件的 token 估算请求：不写日志、不产生副作用。"""
    try:
        return bool(getattr(event, GUTOKEN_ACTIVE, False))
    except Exception:
        return False


def gutoken_text(event: Any) -> str:
    try:
        return str(getattr(event, GUTOKEN_TEXT, "") or "")
    except Exception:
        return ""


def should_skip_side_effects(event: Any) -> bool:
    """是否应当跳过一切副作用（状态更新/记忆写入/日志）。"""
    return is_gutoken(event)


async def reenter_pipeline(event: Any, *, context: Any = None,
                           source: str = "proactive",
                           delay: float = 0.0) -> bool:
    """把合成事件放回原生事件队列 —— 本方案的**唯一**生成入口。

    这是硬性约束 1 的落地点：本函数不调用 provider，只投递事件；
    真正的生成由原生管道完成，因而所有钩子照常生效。

    `context` **必须显式传**
    ------------------------
    框架从不在 `AstrMessageEvent` 上挂 `context` 属性 —— 事件的
    `__init__` 只收 `message_str / message_obj / platform_meta / session_id`
    （aiocqhttp 再多一个 `bot`），`role`、`_extras`、`trace` 也是基类自己
    初始化的。所以 `getattr(event, "context", None)` **恒为 None**。

    旧写法正是靠这个取值，于是重入永远静默返回 False：
    戳一戳的 llm 模式与主动消息**一条都没真正发出去过**，
    而且失败路径既不抛异常也不打日志，从外面看就是"什么都没发生"。

    现在：拿不到 context 或入队失败一律 **warning**，不再有无声失败。
    """
    import asyncio
    try:
        mark_simulated(event, source=source)
        if delay > 0:
            await asyncio.sleep(delay)
        ctx = context if context is not None else getattr(event, "context", None)
        if ctx is None:
            logger.warning(
                "[chatcore_neo] 事件重入失败：没有 context，拿不到原生事件"
                f"队列（source={source}）")
            return False
        queue = ctx.get_event_queue()
        queue.put_nowait(event)
        return True
    except Exception as e:
        logger.warning(f"[chatcore_neo] 事件重入失败（source={source}）: {e!r}")
        return False
