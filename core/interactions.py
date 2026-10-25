# -*- coding: utf-8 -*-
"""消息级交互：戳一戳 / 引用回溯 / 连续消息合并。

这三件事都发生在**同一条消息的生命周期**上，且都只读事件、不改 `req`，
所以放在一起。它们的共同点是：**没有它们也能跑，但用户会立刻感觉"少了点什么"**。

* **戳一戳**（poke）：QQ 的"戳一戳"是独立通知类型。不回显得没反应，
  每次都用 LLM 回又太贵。策略：概率回复 + 冷却 + 只理自己（可配）。
* **引用回溯**（quote）：用户引用某条消息说话时，模型看不到被引用的内容。
  这里把被引用消息的正文取回来附在 prompt 里（按深度上限防止长链）。
* **连续消息合并**（merge）：用户连发 3 条短消息，bot 逐条回会显得很蠢。
  合并成一轮回答，并附一句提示让模型别漏。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 戳一戳

POKE_NOTICE = "（对方戳了你一下）"
DEFAULT_POKE_TEXT = "别戳我啦~"


@dataclass
class PokeState:
    last_reply_ts: float = 0.0
    count: int = 0
    replied: int = 0


class PokeHandler:
    """戳一戳处理。

    默认 **只在被戳自己时** 才回应（`poke_only_self`）——
    群里互相戳来戳去时 bot 插嘴会很吵。
    """

    def __init__(self, *, enabled: bool = True, probability: float = 1.0,
                 cooldown: float = 60.0, only_self: bool = True,
                 reply_mode: str = "llm", text: str = DEFAULT_POKE_TEXT,
                 rng: random.Random | None = None) -> None:
        self.enabled = enabled
        self.probability = max(0.0, min(1.0, probability))
        self.cooldown = max(0.0, cooldown)
        self.only_self = only_self
        self.reply_mode = (reply_mode or "llm").strip().lower()
        self.text = text or DEFAULT_POKE_TEXT
        self._rng = rng or random.Random()
        self._states: dict[str, PokeState] = {}
        self.stats = {"seen": 0, "replied": 0, "skipped_self": 0,
                      "skipped_cooldown": 0, "skipped_prob": 0}

    def _state(self, umo: str) -> PokeState:
        s = self._states.get(umo)
        if s is None:
            s = PokeState()
            self._states[umo] = s
        return s

    def should_reply(self, umo: str, *, target_is_self: bool = True,
                     now: float | None = None) -> bool:
        """要不要回应这次戳一戳。**计数也在这里**（`seen` / `count`）。

        曾经还有一个 `note_poke()` 专门加计数，且全工程没有调用点 ——
        一旦有人两边都调，`seen` 就会翻倍（和"合成事件被再判成戳一戳"
        那个坑表现一样）。已经删掉，计数只有一个入口。
        """
        if not self.enabled:
            return False
        self.stats["seen"] += 1
        now = now if now is not None else time.time()
        s = self._state(umo)
        s.count += 1
        if self.only_self and not target_is_self:
            self.stats["skipped_self"] += 1
            return False
        if self.cooldown and s.last_reply_ts and \
                now - s.last_reply_ts < self.cooldown:
            self.stats["skipped_cooldown"] += 1
            return False
        if self._rng.random() >= self.probability:
            self.stats["skipped_prob"] += 1
            return False
        s.last_reply_ts = now
        s.replied += 1
        self.stats["replied"] += 1
        return True

    def report(self) -> dict:
        return {**self.stats, "enabled": self.enabled,
                "probability": self.probability, "cooldown": self.cooldown,
                "only_self": self.only_self, "reply_mode": self.reply_mode}


def is_poke_event(event: Any) -> bool:
    """判断是不是戳一戳。

    **OneBot v11 的戳一戳是 `notice_type="notify"` + `sub_type="poke"`**，
    不是 `notice_type="poke"` —— 旧实现判错了字段，导致整个戳一戳功能
    永远不触发（配置项全成了摆设）。

    另一条更稳的路：AstrBot 的 aiocqhttp 适配器在收到戳一戳时
    **会往消息链里塞一个 `Poke` 组件**，所以直接看组件类型即可，
    这比读 `raw_message` 更不依赖协议端实现。

    ⚠️ 调用方必须自己排除**合成事件**
    --------------------------------
    第 ③ 条兜底（`message_str == POKE_NOTICE`）会认下"正文正好是那句提示"
    的事件，而戳一戳的 llm 回应**恰恰**就是造一条正文为 `POKE_NOTICE`
    的合成事件重入管道。于是合成事件会被再判成一次戳一戳：

    * 一次戳让 `should_reply` 被调用两次（`seen` 统计翻倍）；
    * `poke_cool_down_seconds = 0` 时会**无限套娃**。

    所以 `main.on_any_message` 里的条件是
    `is_poke_event(event) and not compat.is_simulated(event)`。
    这里刻意不加这个判断：本函数只回答"这条事件的形态像不像戳一戳"，
    "该不该当成用户动作"是调用方的语义决定。
    """
    # ① 组件判断（首选：适配器已经解析好了，兼容性最好）
    try:
        from astrbot.api.message_components import Poke
        for comp in (event.get_messages() or []):
            if isinstance(comp, Poke):
                return True
    except Exception:
        pass
    # ② 原始字段（兼容不同协议端；两种命名都认）
    try:
        msg = getattr(event, "message_obj", None)
        raw = getattr(msg, "raw_message", None)
        if isinstance(raw, dict) and str(raw.get("post_type", "")) == "notice":
            nt = str(raw.get("notice_type", ""))
            st = str(raw.get("sub_type", ""))
            if nt == "poke" or (nt == "notify" and st == "poke"):
                return True
    except Exception:
        pass
    return str(getattr(event, "message_str", "") or "") == POKE_NOTICE


def poke_target(event: Any) -> str:
    """取被戳的人（QQ 号）。取不到返回空串。"""
    try:
        from astrbot.api.message_components import Poke
        for comp in (event.get_messages() or []):
            if isinstance(comp, Poke):
                # Poke 自带 target_id()，它会同时兼容 id / qq 两个字段
                fn = getattr(comp, "target_id", None)
                if callable(fn):
                    try:
                        v = fn()
                        if v:
                            return str(v)
                    except Exception:
                        pass
                return str(getattr(comp, "id", "") or
                           getattr(comp, "qq", "") or "")
    except Exception:
        pass
    try:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            return str(raw.get("target_id", "") or "")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 引用回溯

@dataclass
class QuoteInfo:
    msg_id: str = ""
    sender: str = ""
    text: str = ""


def extract_reply_target(event: Any, *, max_chars: int = 400) -> QuoteInfo | None:
    """从消息链里取被引用的那条消息。

    只取**链上第一条** Reply，不做递归展开 —— 所以"深度"这个概念本身不成立。
    真正需要限制的是**被引用正文的长度**：一条长文引用足以挤爆上下文。
    （原参数名 `max_depth` 只在签名与 docstring 里出现，函数体从不使用它，
    对应的 `quote_max_depth` 是幽灵配置；现改名 `max_chars` 并真正生效。）
    """
    try:
        from astrbot.api.message_components import Reply
    except Exception:
        return None
    try:
        chain = event.get_messages() or []
    except Exception:
        return None
    for comp in chain[:3]:
        if not isinstance(comp, Reply):
            continue
        info = QuoteInfo(
            msg_id=str(getattr(comp, "id", "") or ""),
            sender=str(getattr(comp, "sender_nickname", "")
                       or getattr(comp, "sender_id", "") or ""),
            text=str(getattr(comp, "message_str", "")
                     or getattr(comp, "text", "") or "")[:max(1, int(max_chars))],
        )
        if info.text:
            return info
    return None


def build_quote_block(info: QuoteInfo) -> str:
    """把被引用内容折成一段提示，附在动态段里。"""
    if info is None or not info.text.strip():
        return ""
    who = f"{info.sender}：" if info.sender else ""
    return f"# 被引用的消息\n{who}{info.text.strip()}"


# ---------------------------------------------------------------- 连续消息合并

MERGE_NOTICE_DEFAULT = (
    "用户刚刚连续发送了多条消息，请在这一次回复中一并回应所有消息，"
    "不要逐条重复确认，也不要漏掉任何一条。"
)


@dataclass
class PendingBatch:
    texts: list[str] = field(default_factory=list)
    first_ts: float = 0.0
    last_ts: float = 0.0

    @property
    def size(self) -> int:
        return len(self.texts)


class MergeGate:
    """连续消息合并闸门。

    工作方式（**不阻塞、不延迟**，只做"合并说明"）：
      * 同一个会话在 `window` 秒内到达的多条消息会被记成一批
      * 本轮的动态段里附上一句"用户连发了几条，一并回应"
      * 已回复过的批次不再重复拼接

    之所以不做"等服务端攒够再发"：那会引入人为延迟，
    用户发一句等 2 秒才收到回应，体验反而更差。
    """

    def __init__(self, *, enabled: bool = True, window: float = 8.0,
                 min_count: int = 2, notice: str = "") -> None:
        self.enabled = enabled
        self.window = max(0.5, window)
        self.min_count = max(2, int(min_count))
        self.notice = (notice or "").strip() or MERGE_NOTICE_DEFAULT
        self._batches: dict[str, PendingBatch] = {}
        self.stats = {"batches": 0, "notices": 0}

    def note(self, umo: str, text: str, now: float | None = None) -> int:
        """记一条消息，返回当前批次大小。"""
        if not self.enabled:
            return 1
        now = now if now is not None else time.time()
        b = self._batches.get(umo)
        if b is None or (now - b.last_ts) > self.window:
            b = PendingBatch(texts=[], first_ts=now, last_ts=now)
            self._batches[umo] = b
            self.stats["batches"] += 1
        b.texts.append(text or "")
        b.last_ts = now
        return b.size

    def take_notice(self, umo: str) -> str:
        """取出并消费合并提示（同一批只提示一次）。"""
        if not self.enabled:
            return ""
        b = self._batches.get(umo)
        if b is None or b.size < self.min_count:
            return ""
        body = "\n".join(f"- {t}" for t in b.texts if t.strip())
        self._batches.pop(umo, None)
        self.stats["notices"] += 1
        return f"# 用户连发消息\n{self.notice}\n{body}"

    def peek(self, umo: str) -> int:
        b = self._batches.get(umo)
        return b.size if b else 0

    def cancel(self, umo: str) -> None:
        """丢弃某会话待合并的批次（供 /reset 清上下文，避免 reset 后仍提示连发）。"""
        self._batches.pop(umo, None)

    def sweep(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        dead = [k for k, b in self._batches.items()
                if now - b.last_ts > self.window * 4]
        for k in dead:
            self._batches.pop(k, None)
        return len(dead)

    def report(self) -> dict:
        return {**self.stats, "enabled": self.enabled, "window": self.window,
                "min_count": self.min_count, "pending": len(self._batches)}
