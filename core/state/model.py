# -*- coding: utf-8 -*-
"""状态数值模型：好感度 / 淫乱度 / 恶堕值 / 情绪。

移植自旧 `lili_state` 的 `emotion_label` / `lewdness_label` /
`build_state_snapshot` / `update_state`，**判定阈值与增减量保持一致**，
否则用户会立刻察觉"好感度涨得不一样了"。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

# ---- 标签阈值（与旧实现逐值一致）----

def emotion_label(val: int) -> str:
    if val >= 70:
        return "开心"
    if val >= 40:
        return "平静"
    if val >= 20:
        return "烦躁"
    return "低落"


def lewdness_label(val: int) -> str:
    if val >= 100:
        return "满"
    if val >= 67:
        return "高"
    if val >= 34:
        return "中"
    return "低"


def affection_desc(val: int) -> str:
    if val >= 90:
        return "非常高"
    if val >= 80:
        return "高"
    if val >= 50:
        return "中"
    return "低"


# ---- 关键词表（与旧实现逐词一致）----

POS_KW = ["喜欢", "可爱", "厉害", "好棒", "好牛", "牛逼", "夸", "好厉害",
          "好可爱", "贴贴", "抱抱", "想你了", "爱你", "喜欢你", "好喜欢"]
NEG_KW = ["傻逼", "蠢", "滚", "烦死了", "讨厌", "恶心", "垃圾", "废物",
          "骂你", "傻", "滚开", "烦人"]
EXPLICIT_KW = ["嗯", "啊", "身体", "舒服", "想要", "舔", "摸", "插", "湿", "紧", "热"]


@dataclass
class MemberState:
    """一个 (会话, 人格, 用户) 三元组的状态。"""
    affection: int = 65
    lewdness: int = 20
    depravity: int = 0
    emotion: int = 100
    stutter_done: bool = False
    conversation_log: list[dict[str, Any]] = field(default_factory=list)
    inject: str = ""          # 来自 _inject_overrides 的额外注入提示

    # ------------------------------------------------------------ 序列化
    @classmethod
    def from_dict(cls, d: dict[str, Any] | None,
                  *, default_affection: int = 65,
                  default_lewdness: int = 20,
                  default_depravity: int = 0) -> "MemberState":
        d = d or {}
        return cls(
            affection=int(d.get("affection", default_affection)),
            lewdness=int(d.get("lewdness", default_lewdness)),
            depravity=int(d.get("depravity", default_depravity)),
            emotion=int(d.get("emotion", 100)),
            stutter_done=bool(d.get("stutter_done", False)),
            conversation_log=list(d.get("conversation_log") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "affection": self.affection,
            "lewdness": self.lewdness,
            "depravity": self.depravity,
            "emotion": self.emotion,
            "stutter_done": self.stutter_done,
            "conversation_log": self.conversation_log,
        }

    # ------------------------------------------------------------ 视图
    @property
    def emotion_label(self) -> str:
        return emotion_label(self.emotion)

    @property
    def lewdness_label(self) -> str:
        return lewdness_label(self.lewdness)

    def snapshot(self) -> str:
        """旧实现的单行状态快照（格式逐字保持一致）。"""
        return (f"好感:{self.affection}({affection_desc(self.affection)}) "
                f"情绪:{self.emotion_label}({self.emotion}) "
                f"淫乱:{self.lewdness_label}({self.lewdness}) "
                f"恶堕:{self.depravity}")

    def dynamic_block(self) -> str:
        """注入给模型的动态状态段（L3）。"""
        lines = [
            f"- 好感度：{self.affection}/100"
            f"（{affection_desc(self.affection)}；≥80 可撒娇，≥90 有占有欲，≤30 冷淡）",
            f"- 情绪值：{self.emotion}/100 → 当前【情绪】={self.emotion_label}",
            f"- 淫乱度：{self.lewdness}/100 → {self.lewdness_label}",
            f"- 恶堕值：{self.depravity}/100",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------ 更新
    def update(self, llm_response: str, user_msg: str, *,
               enable_affection: bool = True, enable_lewdness: bool = True,
               rng: random.Random | None = None) -> dict[str, int]:
        """按旧实现的关键词规则更新数值。返回增减量（便于记录/测试）。"""
        r = rng or random
        em = self.emotion
        lewd = self.lewdness
        delta = {"emotion": 0, "affection": 0, "lewdness": 0, "depravity": 0}

        if any(kw in user_msg for kw in POS_KW):
            self.emotion = min(100, em + 8)
            delta["emotion"] = self.emotion - em
        if any(kw in user_msg for kw in NEG_KW):
            before = self.emotion
            self.emotion = max(0, before - 10)
            delta["emotion"] += self.emotion - before

        if enable_affection:
            if any(kw in user_msg for kw in POS_KW):
                before = self.affection
                self.affection = min(100, before + 3)
                delta["affection"] += self.affection - before
            if any(kw in user_msg for kw in NEG_KW):
                before = self.affection
                self.affection = max(0, before - 3)
                delta["affection"] += self.affection - before

        if enable_lewdness:
            is_explicit = (len(llm_response) > 100
                           and any(kw in llm_response for kw in EXPLICIT_KW))
            if is_explicit and self.emotion >= 40:
                before = self.lewdness
                self.lewdness = min(100, lewd + r.randint(5, 15))
                delta["lewdness"] = self.lewdness - before
                before_d = self.depravity
                self.depravity = min(100, self.depravity + r.randint(5, 15))
                delta["depravity"] = self.depravity - before_d
            if emotion_label(self.emotion) == "开心":
                self.lewdness = max(self.lewdness, 30)
            if self.lewdness >= 100:
                self.lewdness = 0
                self.depravity = 0
        return delta

    # ------------------------------------------------------------ 日志
    def append_user(self, content: str, uid: str) -> None:
        self.conversation_log.append(
            {"role": "user", "user_id": uid, "content": content,
             "time": int(time.time())})

    def append_assistant(self, thought: str, content: str = "") -> None:
        """`content` = 回复原文（截断留档）。

        此前只存 40 字 thought，判定器（`_recent_msgs`）看不到 bot 实际
        说过什么，剧情连贯性也无从回溯。原文截 800 字防止日志膨胀。
        """
        entry: dict[str, Any] = {"role": "assistant",
                                 "time": int(time.time()),
                                 "lili_thought": thought}
        text = (content or "").strip()
        if text:
            entry["content"] = text[:800]
        self.conversation_log.append(entry)

    def groom(self, max_count: int, timeout_secs: int) -> None:
        """按旧实现裁剪对话日志：保留最近 N 条 + 超时窗口内的全部。"""
        if max_count <= 0 or not self.conversation_log:
            return
        now = int(time.time())
        cutoff = now - timeout_secs
        recent = [e for e in self.conversation_log if e.get("time", 0) >= cutoff]
        expired = [e for e in self.conversation_log if e.get("time", 0) < cutoff]
        keep = list(recent)
        budget = max(0, max_count - len(recent))
        if expired and budget > 0:
            expired.sort(key=lambda e: e.get("time", 0))
            keep = expired[-budget:] + keep
        seen: set = set()
        deduped = []
        for e in keep:
            sig = (e.get("role", ""),
                   e.get("content", e.get("lili_thought", "")),
                   e.get("time", 0))
            if sig not in seen:
                seen.add(sig)
                deduped.append(e)
        deduped.sort(key=lambda e: e.get("time", 0))
        self.conversation_log = deduped


def build_thought(state: MemberState) -> str:
    """把状态折成一句"内心活动"（旧 `build_bot_thought` 的等价物）。"""
    em_thoughts = {
        "开心": "心情挺好的",
        "平静": "没什么特别的感觉",
        "烦躁": "有点烦",
        "低落": "不太想说话",
    }
    thought = em_thoughts.get(state.emotion_label, "还行")
    if state.affection >= 90:
        thought += "，觉得对方人很好"
    elif state.affection >= 80:
        thought += "，聊得挺舒服"
    elif state.affection <= 30:
        thought += "，不太想理这个人"
    if state.lewdness_label in ("满", "高"):
        thought += "，有点想做坏事"
    elif state.lewdness_label == "中":
        thought += "，脑子里偶尔飘过色色的念头"
    return thought
