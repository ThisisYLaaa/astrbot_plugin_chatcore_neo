# -*- coding: utf-8 -*-
"""W4 注意力与判定测试：概率模型 / 退避 / 读空气 / 时段 / fail-open。"""

from __future__ import annotations

import asyncio
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.attention import DECAY_FACTOR, AttentionManager  # noqa: E402
from core.judge import (Judge, build_judge_prompt,  # noqa: E402
                        fmt_time_ago, parse_list_cfg)


def _am(**kw) -> AttentionManager:
    kw.setdefault("bubble_base", 0.02)
    kw.setdefault("active_cap", 0.30)
    kw.setdefault("decay_minutes", 10.0)
    kw.setdefault("cool_down_seconds", 0.0)   # 测试里默认不冷却，便于看概率
    return AttentionManager(**kw)


# ---------------------------------------------------------------- 概率模型

def test_probability_starts_high_and_decays_to_bubble():
    """刚交互过 → 接近上限；久未交互 → 收敛到底概率。"""
    am = _am()
    umo = "g1"
    now = time.time()
    p_now = am.current_probability(umo, now)
    p_later = am.current_probability(umo, now + 60 * 60)
    assert p_now > p_later, (p_now, p_later)
    # 极久之后收敛到 bubble_base
    p_forever = am.current_probability(umo, now + 60 * 60 * 24 * 30)
    assert abs(p_forever - am.bubble_base) < 1e-3, p_forever


def test_probability_formula_matches_documented_model():
    """把公式钉住：换了系数就是换了手感，必须显式改动。"""
    am = _am(bubble_base=0.02, active_cap=0.30, decay_minutes=10.0)
    umo = "g1"
    now = 1_700_000_000.0
    am._state(umo)["last_interaction_ts"] = now - 300   # 5 分钟前
    expected_base = 0.02 + (0.30 - 0.02) * math.exp(-DECAY_FACTOR * 5.0 / 10.0)
    got = am.current_probability(umo, now)
    assert abs(got - expected_base) < 1e-6, (got, expected_base)


def test_probability_capped():
    am = _am(active_cap=0.30, hard_trigger_boost=5.0)
    umo = "g1"
    for _ in range(5):
        am.record_hard_trigger(umo)
    assert am.current_probability(umo) <= 0.30 + 1e-9


def test_hard_trigger_raises_probability():
    am = _am()
    umo = "g1"
    am._state(umo)["last_interaction_ts"] = time.time() - 3600
    before = am.current_probability(umo)
    am.record_hard_trigger(umo)
    after = am.current_probability(umo)
    assert after > before, (before, after)


def test_cooldown_zeroes_probability():
    am = _am(cool_down_seconds=120.0)
    umo = "g1"
    am.record_reply(umo)
    assert am.current_probability(umo) == 0.0
    assert am.explain(umo)["in_cooldown"] is True


# ---------------------------------------------------------------- 退避 / 读空气

def test_backoff_after_soft_misses():
    am = _am(no_action_backoff=0.5, backoff_floor=0.25)
    umo = "g1"
    # **固定 now**：概率里有基于真实时间的指数衰减，循环执行期间 base 会
    # 一直往下走，用 `p0 * 0.25 - 1e-9` 这种紧 epsilon 比较会随机器抖动而失败。
    now = time.time()
    am._state(umo)["last_interaction_ts"] = now - 600
    p0 = am.current_probability(umo, now)
    am.record_soft_miss(umo)
    p1 = am.current_probability(umo, now)
    assert p1 < p0
    for _ in range(10):
        am.record_soft_miss(umo)
    p_many = am.current_probability(umo, now)
    assert abs(p_many - p0 * 0.25) < 1e-9, (
        f"退避应恰好收敛到下限定值 {p0 * 0.25}，实际 {p_many}")
    am.record_soft_hit(umo)
    assert am.current_probability(umo, now) > p_many


def test_read_air_suppresses_when_others_dense():
    am = _am(read_air_factor=0.5, others_density_threshold=3,
             followup_window_seconds=0.0)
    umo = "g1"
    am._state(umo)["last_interaction_ts"] = time.time() - 600
    p_idle = am.current_probability(umo)
    for _ in range(3):
        am.record_others_message(umo)
    p_dense = am.current_probability(umo)
    assert p_dense < p_idle, "别人聊得热闹时 bot 应更沉默"
    assert am.explain(umo)["read_air"] == 0.5


def test_followup_boost_after_own_reply():
    am = _am(followup_boost=0.5, others_density_threshold=1)
    umo = "g1"
    am._state(umo)["last_interaction_ts"] = time.time() - 600
    am.record_others_message(umo)      # 同时满足"密集"条件
    am.record_reply(umo)
    # followup 窗口内应抬升而不是压制
    assert am.explain(umo)["read_air"] > 1.0


def test_time_multiplier_rule():
    am = _am()
    am.time_rules = [{"start": "00:00", "end": "23:59", "multiplier": 0.25}]
    umo = "g1"
    am._state(umo)["last_interaction_ts"] = time.time() - 600
    am2 = _am()
    am2._state(umo)["last_interaction_ts"] = am._state(umo)["last_interaction_ts"]
    assert am.current_probability(umo) < am2.current_probability(umo)
    assert am.explain(umo)["time_multiplier"] == 0.25


def test_group_override_and_clear():
    am = _am()
    umo = "g1"
    am.set_group_override(umo, {"active_max_prob": 0.9})
    assert am._param(umo, "active_cap", am.active_cap) == 0.9
    am.set_group_override(umo, {})
    assert am._param(umo, "active_cap", am.active_cap) == am.active_cap


def test_should_respond_is_deterministic_with_seeded_rng():
    am = AttentionManager(cool_down_seconds=0.0, rng=random.Random(42))
    umo = "g1"
    a = [am.should_respond(umo) for _ in range(5)]
    am2 = AttentionManager(cool_down_seconds=0.0, rng=random.Random(42))
    b = [am2.should_respond(umo) for _ in range(5)]
    assert a == b, "同种子应产生同序列（可复现排障）"


# ---------------------------------------------------------------- 判定器

def test_parse_list_cfg_variants():
    assert parse_list_cfg(None) == []
    assert parse_list_cfg([]) == []
    assert parse_list_cfg("a, b，c") == ["a", "b", "c"]
    assert parse_list_cfg(["x", " y "]) == ["x", "y"]
    assert parse_list_cfg(100000001) == ["100000001"], "纯数字配置不能被丢掉"


def test_build_judge_prompt_contains_context():
    system, user = build_judge_prompt(
        "在吗", [{"sender_name": "小明", "text": "你好", "ts": time.time()}],
        attention_hint="概率 0.3")
    assert "REPLY" in system and "NOP" in system
    assert "小明" in user and "在吗" in user
    assert "注意力状态" in user


def test_build_judge_prompt_custom_system():
    system, _ = build_judge_prompt("x", [], custom_system="只用 REPLY")
    assert system == "只用 REPLY"


def test_fmt_time_ago():
    now = time.time()
    assert fmt_time_ago(now) == "刚刚"
    assert fmt_time_ago(now - 300) == "5分前"
    assert fmt_time_ago(now - 7200) == "2小时前"
    assert fmt_time_ago(0) == "?"


def _judge(cfg: dict | None = None) -> Judge:
    class Cfg(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    return Judge(context=None, config=Cfg(cfg or {}))


def test_judge_skips_private_and_commands():
    """私聊必回、指令必回 —— 不浪费判定调用。"""
    class Ev:
        def is_private_chat(self):
            return True

    j = _judge()
    ok, why = asyncio.run(j.should_skip(
        Ev(), "Banyan:FriendMessage:1", "u", "在吗"))
    assert not ok and why == "private"

    ok, why = asyncio.run(j.should_skip(
        Ev(), "Banyan:GroupMessage:1", "u", "/help"))
    assert not ok and why == "command"


def test_judge_skips_exempt_users():
    class Ev:
        def is_private_chat(self):
            return False

    j = _judge({"no_reply_judge_exempt_users": ["100000001"]})
    ok, why = asyncio.run(j.should_skip(
        Ev(), "Banyan:GroupMessage:1", "100000001", "随便说点什么"))
    assert not ok and why == "exempt"


def test_judge_attention_only_mode():
    class Ev:
        def is_private_chat(self):
            return False

    j = _judge({"no_reply_judge_attention_only": True})
    ok, why = asyncio.run(j.should_skip(
        Ev(), "g", "u", "普通发言", attention_triggered=False))
    assert not ok and why == "not-attention-triggered"


def test_judge_fails_open_without_provider():
    """**最关键的一条**：判定链路任何问题都必须按"照常回复"处理，绝不吞消息。"""
    class Ev:
        def is_private_chat(self):
            return False

    j = _judge()          # context=None → 拿不到 provider
    ok, why = asyncio.run(j.should_skip(
        Ev(), "g", "u", "你好呀", attention_triggered=True))
    assert ok is False, "无 provider 时必须 fail-open"
    assert why in ("timeout", "error", "empty")


def test_judge_intent_fails_open():
    j = _judge()
    assert asyncio.run(j.intent_score("g", "你们说呢？")) == 0


def test_judge_disabled_by_config():
    class Ev:
        def is_private_chat(self):
            return False

    j = _judge({"enable_no_reply_judge": False})
    ok, why = asyncio.run(j.should_skip(Ev(), "g", "u", "hi"))
    assert not ok and why == "disabled"

    j2 = _judge({"enable_intent": False})
    assert asyncio.run(j2.intent_score("g", "hi")) == 0
