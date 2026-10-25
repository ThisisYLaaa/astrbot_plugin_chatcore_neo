# -*- coding: utf-8 -*-
"""群聊注意力状态机（移植自 chatcore_lite / 参考实现 chatcore，MIT）。

决定 bot 是否**自发**参与群聊。每个会话维护轻量状态，概率在读取时惰性
计算（无后台定时器）—— 这一点刻意保留：定时器会引入"什么时刻算"的
额外状态，而惰性计算只依赖时间戳。

概率模型（与旧实现逐项一致）::

    idle_min = (now - last_interaction_ts) / 60
    active_contribution = (active_cap - bubble_base) * exp(-3.0 * idle_min / decay_minutes)
    boost_remaining = sum(boost_i * exp(-3.0 * (now - ts_i) / decay_minutes))
    prob = clamp(bubble_base + active_contribution + boost_remaining, 0, active_cap)

再叠加三个修正因子：
  * **退避**：连续软触发落空 → 概率按 `no_action_backoff^n` 衰减（有下限）
  * **时段规则**：某些时段整体乘系数
  * **读空气**：他人高密度发言压制；bot 刚回复过则抬升
冷却期内软触发概率直接归 0（硬触发不受冷却限制，走调用方分支）。
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

_TONIGHT = "23:59"
_MIDNIGHT = "00:00"

# 与旧实现一致的衰减系数（写死，不暴露成配置 —— 它不是可调项，是模型的一部分）
DECAY_FACTOR = 3.0


class AttentionManager:
    def __init__(
        self,
        bubble_base: float = 0.02,
        active_cap: float = 0.30,
        decay_minutes: float = 10.0,
        hard_trigger_boost: float = 0.10,
        cool_down_seconds: float = 120.0,
        no_action_backoff: float = 0.6,
        backoff_floor: float = 0.25,
        time_rules: list[dict] | None = None,
        read_air_factor: float = 0.5,
        others_window_seconds: float = 180.0,
        others_density_threshold: int = 3,
        followup_boost: float = 0.05,
        followup_window_seconds: float = 180.0,
        rng: random.Random | None = None,
    ) -> None:
        self.bubble_base = max(0.0, min(bubble_base, 1.0))
        self.active_cap = max(self.bubble_base, min(active_cap, 1.0))
        self.decay_minutes = max(0.1, decay_minutes)
        self.hard_trigger_boost = max(0.0, hard_trigger_boost)
        self.cool_down_seconds = max(0.0, cool_down_seconds)
        self.no_action_backoff = max(0.0, min(no_action_backoff, 1.0))
        self.backoff_floor = max(0.0, min(backoff_floor, 1.0))
        self.time_rules = [r for r in (time_rules or []) if isinstance(r, dict)]
        self.read_air_factor = max(0.0, min(read_air_factor, 1.0))
        self.others_window_seconds = max(1.0, others_window_seconds)
        self.others_density_threshold = max(1, int(others_density_threshold))
        self.followup_boost = max(0.0, followup_boost)
        self.followup_window_seconds = max(1.0, followup_window_seconds)
        self._rng = rng or random.Random()
        self._states: dict[str, dict] = {}
        self._overrides: dict[str, dict] = {}

    # ------------------------------------------------------------ 会话级覆盖
    def set_group_override(self, umo: str, params: dict) -> None:
        """为指定会话覆盖参数（全局默认 + 会话级覆盖）。空 dict 表示清除。"""
        if not params:
            self._overrides.pop(umo, None)
            return
        mapping = {
            "bubble_base_prob": "bubble_base",
            "active_max_prob": "active_cap",
            "decay_minutes": "decay_minutes",
            "hard_trigger_boost": "hard_trigger_boost",
            "cool_down_seconds": "cool_down_seconds",
        }
        clean = {attr: params[key] for key, attr in mapping.items()
                 if key in params and params[key] is not None}
        if clean:
            self._overrides[umo] = clean
        else:
            self._overrides.pop(umo, None)

    def clear_group_override(self, umo: str) -> None:
        self._overrides.pop(umo, None)

    def _param(self, umo: str, attr: str, default: float) -> float:
        ov = self._overrides.get(umo)
        if ov and attr in ov:
            return ov[attr]
        return default

    # ------------------------------------------------------------ 状态
    def _state(self, umo: str) -> dict:
        s = self._states.get(umo)
        if s is None:
            s = {"last_interaction_ts": time.time(), "boosts": [],
                 "last_reply_ts": 0.0, "soft_misses": 0, "others_ts": []}
            self._states[umo] = s
        return s

    def _decay(self, elapsed_min: float, umo: str = "") -> float:
        d = self._param(umo, "decay_minutes", self.decay_minutes) if umo \
            else self.decay_minutes
        return math.exp(-DECAY_FACTOR * elapsed_min / d)

    def record_interaction(self, umo: str) -> None:
        self._state(umo)["last_interaction_ts"] = time.time()

    def record_reply(self, umo: str) -> None:
        self._state(umo)["last_reply_ts"] = time.time()

    def record_hard_trigger(self, umo: str) -> None:
        s = self._state(umo)
        now = time.time()
        s["last_interaction_ts"] = now
        v = self._param(umo, "hard_trigger_boost", self.hard_trigger_boost)
        s["boosts"].append({"ts": now, "value": v})
        s["boosts"] = [b for b in s["boosts"]
                       if b["value"] * self._decay((now - b["ts"]) / 60, umo) > 1e-4]

    def record_soft_miss(self, umo: str) -> None:
        self._state(umo)["soft_misses"] += 1

    def record_soft_hit(self, umo: str) -> None:
        self._state(umo)["soft_misses"] = 0

    def record_others_message(self, umo: str) -> None:
        s = self._state(umo)
        now = time.time()
        s["others_ts"].append(now)
        s["others_ts"] = [t for t in s["others_ts"]
                          if now - t <= self.others_window_seconds]

    def bump_probability(self, umo: str, amount: float) -> None:
        """固定量抬升活跃度（隐性意图命中等场景用）。"""
        self._state(umo)["boosts"].append(
            {"ts": time.time(), "value": max(0.0, amount)})

    def reset(self, umo: str) -> None:
        self._states.pop(umo, None)

    # ------------------------------------------------------------ 因子
    def _time_multiplier(self, now: float) -> float:
        if not self.time_rules:
            return 1.0
        st = time.localtime(now)
        cur = f"{st.tm_hour:02d}:{st.tm_min:02d}"
        for rule in self.time_rules:
            start = str(rule.get("start", "")).strip() or _MIDNIGHT
            end = str(rule.get("end", "")).strip() or _TONIGHT
            in_rule = (start <= cur <= end if start <= end
                       else (cur >= start or cur <= end))
            if in_rule:
                try:
                    return float(rule.get("multiplier", 1.0))
                except (TypeError, ValueError):
                    return 1.0
        return 1.0

    def _read_air_multiplier(self, s: dict, now: float) -> float:
        s["others_ts"] = [t for t in s["others_ts"]
                          if now - t <= self.others_window_seconds]
        dense = len(s["others_ts"]) >= self.others_density_threshold
        last_reply = s.get("last_reply_ts", 0.0)
        followup = last_reply > 0 and now - last_reply <= self.followup_window_seconds
        if followup:
            return 1.0 + self.followup_boost
        if dense:
            return self.read_air_factor
        return 1.0

    # ------------------------------------------------------------ 主接口
    def current_probability(self, umo: str, now: float | None = None) -> float:
        s = self._state(umo)
        now = now if now is not None else time.time()
        cd = self._param(umo, "cool_down_seconds", self.cool_down_seconds)
        if now - s["last_reply_ts"] < cd:
            return 0.0
        idle_min = (now - s["last_interaction_ts"]) / 60.0
        bubble = self._param(umo, "bubble_base", self.bubble_base)
        cap = self._param(umo, "active_cap", self.active_cap)
        decay = self._param(umo, "decay_minutes", self.decay_minutes)
        base = bubble + (cap - bubble) * math.exp(-DECAY_FACTOR * idle_min / decay)
        base += sum(b["value"] * self._decay((now - b["ts"]) / 60.0, umo)
                    for b in s["boosts"])
        backoff = max(self.backoff_floor,
                      self.no_action_backoff ** s["soft_misses"])
        prob = base * backoff * self._time_multiplier(now) \
            * self._read_air_multiplier(s, now)
        return max(0.0, min(cap, prob))

    def should_respond(self, umo: str, now: float | None = None) -> bool:
        return self._rng.random() < self.current_probability(umo, now)

    # ------------------------------------------------------------ 排障
    def explain(self, umo: str) -> dict:
        """把概率拆成各因子，供 /persona attention 排障。

        概率模型是个乘法链，只看最终值无法判断"为什么现在不吭声"。
        """
        s = self._state(umo)
        now = time.time()
        cd = self._param(umo, "cool_down_seconds", self.cool_down_seconds)
        in_cd = now - s["last_reply_ts"] < cd
        idle_min = (now - s["last_interaction_ts"]) / 60.0
        bubble = self._param(umo, "bubble_base", self.bubble_base)
        cap = self._param(umo, "active_cap", self.active_cap)
        decay = self._param(umo, "decay_minutes", self.decay_minutes)
        base = bubble + (cap - bubble) * math.exp(-DECAY_FACTOR * idle_min / decay)
        boosts = sum(b["value"] * self._decay((now - b["ts"]) / 60.0, umo)
                     for b in s["boosts"])
        backoff = max(self.backoff_floor,
                      self.no_action_backoff ** s["soft_misses"])
        tm = self._time_multiplier(now)
        ra = self._read_air_multiplier(s, now)
        return {
            "probability": round(self.current_probability(umo, now), 4),
            "in_cooldown": in_cd,
            "idle_minutes": round(idle_min, 1),
            "base": round(base, 4), "boosts": round(boosts, 4),
            "backoff": round(backoff, 3), "time_multiplier": round(tm, 3),
            "read_air": round(ra, 3),
            "soft_misses": s["soft_misses"],
            "others_recent": len(s["others_ts"]),
        }

    def snapshot(self) -> list[dict]:
        return [{"umo": umo, **self.explain(umo)} for umo in self._states]

    def stats(self) -> dict[str, Any]:
        return {"sessions": len(self._states),
                "bubble_base": self.bubble_base, "active_cap": self.active_cap,
                "decay_minutes": self.decay_minutes,
                "cool_down_seconds": self.cool_down_seconds ,
                "hard_trigger_boost": self.hard_trigger_boost,
                "no_action_backoff": self.no_action_backoff,
                "backoff_floor": self.backoff_floor,
                "time_rules": len(self.time_rules or [])}
