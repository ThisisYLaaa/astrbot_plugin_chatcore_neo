# -*- coding: utf-8 -*-
"""W4 补全部分测试：动态注入 / 戳一戳 / 连发合并 / 引用 / TTS / 管理接口。"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import webadmin  # noqa: E402
from core.inject import (affection_feel, build_conversation_context,  # noqa: E402
                         build_identity_block, build_inject_text, core_match,
                         count_duplicate_in_log, depravity_feel,
                         lewdness_feel, msg_similarity_label, period_label)
from core.interactions import (MergeGate, PokeHandler, QuoteInfo,  # noqa: E402
                               build_quote_block, extract_reply_target)
from core.state import MemberState, Privilege, StateStore  # noqa: E402
from core.tts import TTS  # noqa: E402


# ---------------------------------------------------------------- 时段

def test_period_label_boundaries():
    """7 时段边界必须与旧实现逐值一致。"""
    cases = [
        (0, "凌晨"), (4, "凌晨"), (5, "清晨"), (7, "清晨"),
        (8, "上午"), (11, "上午"), (12, "中午"), (13, "中午"),
        (14, "下午"), (17, "下午"), (18, "傍晚"), (19, "傍晚"),
        (20, "晚上"), (23, "晚上"),
    ]
    for h, want in cases:
        got = period_label(datetime(2026, 9, 12, h, 30))
        assert got == want, f"{h}点应为{want}，实际{got}"


# ---------------------------------------------------------------- 内心描摹

def test_affection_feel_tier_split():
    """特权/普通两档用词不同：普通档不能出现依赖感/占有欲。"""
    t1_hot = affection_feel(95, "intimate")
    t2_hot = affection_feel(95, "friend")
    assert "依赖" in t1_hot, t1_hot
    assert "依赖" not in t2_hot and "占有" not in t2_hot, t2_hot
    assert "朋友" in t2_hot, t2_hot
    # 旧格式的 "1"/"2" 也要认
    assert affection_feel(95, "1") == t1_hot
    assert affection_feel(95, "2") == t2_hot


def test_affection_feel_all_levels():
    for tier in ("intimate", "friend"):
        vals = [affection_feel(v, tier) for v in (10, 35, 60, 85, 95)]
        assert all(vals), (tier, vals)
        assert len(set(vals)) == 5, f"{tier} 各档文案应互不相同: {vals}"


def test_lewdness_and_depravity_feel():
    assert "想做坏事" in lewdness_feel(70)
    assert "色色" in lewdness_feel(50)
    assert "没什么特别" in lewdness_feel(10)
    assert depravity_feel(80) and depravity_feel(40) and depravity_feel(1)
    assert depravity_feel(0) == ""


# ---------------------------------------------------------------- 日志工具

def test_core_match_ignores_punctuation():
    # 旧实现要求去标点后长度 > 2 才算匹配，防的是"嗯/哦"这类单字误判
    assert core_match("你好啊！！！", "你好啊") is True
    assert core_match("你好啊", "你好啊") is True
    assert core_match("你好啊", "再见了") is False
    assert core_match("嗯", "嗯") is False, "太短的串不该算匹配（防误判）"
    assert core_match("在吗", "在吗") is False, "两字同样不算（与旧实现一致）"


def test_count_duplicate_in_log():
    log = [{"role": "user", "content": "在吗在吗"},
           {"role": "assistant", "content": "在"},
           {"role": "user", "content": "在吗在吗！"}]
    # 第一条完全相同；第二条去标点后同文（长度 > 2 才算）
    assert count_duplicate_in_log(log, "在吗在吗") == 2
    assert count_duplicate_in_log(log, "别的话呀") == 0


def test_msg_similarity_label():
    log = [{"role": "user", "content": "今天天气不错"}]
    assert msg_similarity_label(log, "今天天气不错") == "完全相同"
    assert msg_similarity_label(log, "今天天气不错。") == "高度相似"
    assert msg_similarity_label(log, "你在干嘛") == "不相似"
    assert msg_similarity_label([], "随便") == "不相似"


def test_build_conversation_context_only_assistant():
    log = [{"role": "user", "content": "用户说的"},
           {"role": "assistant", "lili_thought": "她的想法"}]
    txt = build_conversation_context(log, bot_name="香奈美")
    assert "她的想法" in txt
    assert "用户说的" not in txt, "user 原文已在 messages 里，不该重复注入"


# ---------------------------------------------------------------- 注入主文案

def _base_state() -> MemberState:
    return MemberState(affection=85, emotion=100, lewdness=30, depravity=0)


def test_build_inject_text_core_lines():
    st = _base_state()
    txt = build_inject_text(st, "12345", "你好",
                            display_name="香奈美", tier="intimate",
                            cfg={}, rng=__import__("random").Random(1))
    assert "现在是" in txt and "香奈美心情开心" in txt
    assert "你在跟12345聊天" in txt
    # 状态块只讲状态本身：不再夹带"完整人设规则已内嵌于系统提示"这类元描述
    assert "完整人设规则" not in txt


def test_thinking_guide_toggle():
    st = _base_state()
    import random
    off = build_inject_text(st, "1", "x", display_name="A",
                            cfg={"thinking_mode": "关闭"},
                            rng=random.Random(1))
    assert "内心独白" not in off
    on = build_inject_text(MemberState(), "1", "x", display_name="A",
                           cfg={"thinking_mode": "开启"},
                           rng=random.Random(1))
    assert "内心独白" in on and "第一人称" in on


def test_stutter_is_one_shot():
    """结巴每个会话只触发一次（由 state.stutter_done 守住）。"""
    import random
    st = MemberState()
    cfg = {"stutter_probability": 1.0}
    first = build_inject_text(st, "1", "x", display_name="A", cfg=cfg,
                              rng=random.Random(1))
    assert "结巴" in first and st.stutter_done is True
    second = build_inject_text(st, "1", "x", display_name="A", cfg=cfg,
                               rng=random.Random(1))
    assert "结巴" not in second


def test_stutter_disabled_by_zero_probability():
    import random
    st = MemberState()
    txt = build_inject_text(st, "1", "x", display_name="A",
                            cfg={"stutter_probability": 0.0},
                            rng=random.Random(1))
    assert "结巴" not in txt and st.stutter_done is False


def test_erotic_restriction_and_exemption():
    cfg = {"allow_erotic_content": False}
    st = MemberState()
    blocked = build_inject_text(st, "999", "x", display_name="A", cfg=cfg,
                                unrestricted=set())
    assert "禁止色情内容" in blocked
    allowed = build_inject_text(MemberState(), "999", "x", display_name="A",
                                cfg=cfg, unrestricted={"999"})
    assert "禁止色情内容" not in allowed


def test_privilege_marker_in_state_text():
    """状态标注必须显式标出"对方是不是特权用户（等级 1）"。

    回归：core.md 的「特权用户除外」只有身份被显式点明才会生效 ——
    旧实现从不告诉模型眼前这人是谁，主人问提示词也答"听不懂"。
    """
    priv = build_inject_text(MemberState(), "1705744120", "x",
                             display_name="A", tier="intimate")
    assert "特权用户（等级 1）" in priv
    assert "如实回答" in priv and "提示词" in priv
    plain = build_inject_text(MemberState(), "123", "x",
                              display_name="A", tier="friend")
    assert "特权用户" not in plain, "普通成员不该被标成特权用户"


def test_lewdness_hidden_when_erotic_disallowed():
    """关闭色情内容时，淫乱度不该出现在注入里。"""
    from core.inject import lewdness_feel
    st = MemberState(lewdness=90)
    txt = build_inject_text(st, "1", "x", display_name="A",
                            cfg={"allow_erotic_content": False})
    assert lewdness_feel(90) not in txt
    assert "身体没什么特别的感觉" in txt


def test_period_modifier_for_late_night():
    """凌晨/清晨要带生理修饰（真人这个点确实容易困）。"""
    import core.inject as inj
    real = inj.period_label
    try:
        inj.period_label = lambda now=None: "凌晨"
        txt = inj.build_inject_text(MemberState(), "1", "x", display_name="A")
        assert "容易困" in txt
    finally:
        inj.period_label = real


def test_duplicate_line_appears():
    st = _base_state()
    st.conversation_log = [{"role": "user", "content": "在吗"},
                           {"role": "user", "content": "在吗"}]
    txt = build_inject_text(st, "1", "在吗", display_name="A")
    assert "已经第" in txt


def test_proactive_wording_replaces_time_and_dup_lines():
    """合成事件（主动开口）：没有真实用户发言，要避开对上不上的文案。"""
    st = MemberState()
    st.conversation_log = [{"role": "user", "content": "在吗"},
                           {"role": "user", "content": "在吗"}]
    txt = build_inject_text(st, "1", "在吗", display_name="A", proactive=True)
    assert "由你来起个话头" in txt
    assert "已经第" not in txt
    assert "刚发完上一条" not in txt
    assert "刚发的消息跟之前一样" not in txt


def test_session_nsfw_override_inject():
    """会话级显式开关覆盖全局 allow_erotic_content。"""
    # 全局开、会话级关 → 强制注入禁止
    txt = build_inject_text(MemberState(), "999", "x", display_name="A",
                            cfg={"allow_erotic_content": True},
                            nsfw_enabled=False, unrestricted=set())
    assert "禁止色情内容" in txt
    # 全局关、会话级开 → 豁免（不让群聊开 NSFW 被全局掐死）
    txt2 = build_inject_text(MemberState(), "999", "x", display_name="A",
                             cfg={"allow_erotic_content": False},
                             nsfw_enabled=True, unrestricted=set())
    assert "禁止色情内容" not in txt2


def test_conversation_context_toggle():
    st = _base_state()
    st.conversation_log = [{"role": "assistant", "lili_thought": "想法X"}]
    off = build_inject_text(st, "1", "x", display_name="A",
                            cfg={"inject_conversation_context": False})
    assert "想法X" not in off
    on = build_inject_text(st, "1", "x", display_name="A",
                           cfg={"inject_conversation_context": True})
    assert "想法X" in on


# ---------------------------------------------------------------- 身份强调

def test_identity_block_distinguishes_tiers():
    t1 = build_identity_block(display_name="香奈美", tier="intimate",
                              tier_label="引航者", uid="100000001",
                              user_name="小主人")
    assert "引航者" in t1 and "最高亲近度" in t1
    assert "特权用户（等级 1）" in t1, "群聊身份块要显式标出特权等级"
    t2 = build_identity_block(display_name="香奈美", tier="friend",
                              tier_label="引航者", uid="123",
                              user_name="路人")
    assert "不是" in t2 and "普通群友" in t2
    assert "不要使用专属称谓" in t2
    assert "特权用户" not in t2


# ---------------------------------------------------------------- 戳一戳

def test_poke_only_self_blocks_others():
    p = PokeHandler(only_self=True, probability=1.0, cooldown=0.0)
    assert p.should_reply("u", target_is_self=False) is False
    assert p.stats["skipped_self"] == 1
    assert p.should_reply("u", target_is_self=True) is True


def test_poke_cooldown():
    p = PokeHandler(only_self=False, probability=1.0, cooldown=60.0)
    now = 1000.0
    assert p.should_reply("u", now=now) is True
    assert p.should_reply("u", now=now + 10) is False
    assert p.should_reply("u", now=now + 61) is True


def test_poke_probability_and_disable():
    import random
    p = PokeHandler(only_self=False, probability=0.0, cooldown=0.0,
                    rng=random.Random(1))
    assert p.should_reply("u") is False
    p2 = PokeHandler(enabled=False, probability=1.0)
    assert p2.should_reply("u") is False


def test_poke_report_shape():
    r = PokeHandler().report()
    for k in ("seen", "replied", "skipped_self", "enabled", "reply_mode"):
        assert k in r, k


# ---------------------------------------------------------------- 引用

def test_build_quote_block():
    assert build_quote_block(None) == ""
    assert build_quote_block(QuoteInfo(text="  ")) == ""
    b = build_quote_block(QuoteInfo(sender="小明", text="  原始消息  "))
    assert "小明" in b and "原始消息" in b


def test_extract_reply_target_without_reply_component():
    class Ev:
        def get_messages(self):
            return []
    assert extract_reply_target(Ev()) is None


# ---------------------------------------------------------------- 连发合并

def test_merge_requires_min_count():
    g = MergeGate(enabled=True, window=10.0, min_count=2)
    g.note("u", "第一句", now=100.0)
    assert g.take_notice("u") == "", "只有一条时不该提示"
    g.note("u", "第二句", now=101.0)
    notice = g.take_notice("u")
    assert notice and "第一句" in notice and "第二句" in notice


def test_merge_notice_consumed_once():
    g = MergeGate(enabled=True, window=10.0, min_count=2)
    g.note("u", "a", now=1.0)
    g.note("u", "b", now=2.0)
    assert g.take_notice("u")
    assert g.take_notice("u") == "", "同一批只提示一次"


def test_merge_window_resets_batch():
    g = MergeGate(enabled=True, window=5.0, min_count=2)
    g.note("u", "a", now=1.0)
    g.note("u", "b", now=100.0)      # 超出窗口 → 新批次
    assert g.take_notice("u") == "", "跨窗口的两条不该算连发"
    assert g.stats["batches"] == 2


def test_merge_disabled():
    g = MergeGate(enabled=False)
    g.note("u", "a")
    g.note("u", "b")
    assert g.take_notice("u") == ""


def test_merge_sweep():
    g = MergeGate(enabled=True, window=1.0)
    g.note("u", "a", now=1.0)
    assert g.sweep(now=100.0) == 1


# ---------------------------------------------------------------- TTS

def test_tts_disabled_by_default():
    t = TTS(None, enabled=False)
    assert t.available is False
    assert t.should_synthesize("你好", is_private=True) is False


def test_tts_only_private_gate():
    t = TTS(None, enabled=True, only_private=True, max_chars=50)
    assert t.should_synthesize("你好", is_private=True) is True
    assert t.should_synthesize("你好", is_private=False) is False


def test_tts_max_chars_gate():
    t = TTS(None, enabled=True, only_private=False, max_chars=5)
    assert t.should_synthesize("12345", is_private=True) is True
    assert t.should_synthesize("123456", is_private=True) is False


def test_tts_synthesize_fails_open_without_provider():
    import asyncio
    t = TTS(None, enabled=True, only_private=False)
    assert asyncio.run(t.synthesize("你好")) is None
    assert t.stats["failed"] >= 1
    assert t.report()["available"] is False


# ---------------------------------------------------------------- 管理接口

class _FakePlugin:
    def __init__(self, root: Path):
        self.state = StateStore(root)
        self.privilege = Privilege(uid="100000001")
        self.personas = _FakePersonas()

    def _cfg(self, key, default=None):
        return default


class _FakePersonas:
    def all(self):
        return []

    def resolve(self, name):
        return None


def test_webadmin_state_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        plugin = _FakePlugin(Path(td))
        plugin.state.load("umo", "p", "1")
        d = webadmin.set_state(plugin, "umo", "p", "1",
                               {"affection": 88, "emotion": 55,
                                "lewdness": 70, "depravity": 3})
        assert d["state"]["affection"] == 88
        assert d["state"]["emotion"] == 55
        got = webadmin.build_state(plugin, "umo", "p", "1")
        assert got["state"]["lewdness"] == 70
        assert "好感:88" in got["snapshot"]
        assert got["log_count"] == 0


def test_webadmin_state_clamps_range():
    with tempfile.TemporaryDirectory() as td:
        plugin = _FakePlugin(Path(td))
        d = webadmin.set_state(plugin, "umo", "p", "1",
                               {"affection": 999, "emotion": -5})
        assert d["state"]["affection"] == 100
        assert d["state"]["emotion"] == 0


def test_webadmin_clear_log():
    with tempfile.TemporaryDirectory() as td:
        plugin = _FakePlugin(Path(td))
        st = plugin.state.load("umo", "p", "1")
        st.append_user("hi", "1")
        st.emotion = 20
        plugin.state.save("umo", "p", "1", st)
        r = webadmin.clear_log(plugin, "umo", "p", "1")
        assert r["cleared"] == 1
        after = plugin.state.load("umo", "p", "1")
        assert after.conversation_log == [] and after.emotion == 100


def test_webadmin_inject_and_impression():
    with tempfile.TemporaryDirectory() as td:
        plugin = _FakePlugin(Path(td))
        webadmin.set_inject(plugin, "umo", "p", "1", "你暗恋这个人")
        assert webadmin.build_state(plugin, "umo", "p", "1")["inject"] == "你暗恋这个人"

        webadmin.set_impression(plugin, "1", sfw="自动", sfw_custom="手写")
        got = webadmin.build_impression(plugin, "1")
        assert got["impression"]["sfw"] == "自动"
        assert got["effective"] == "手写", "人工覆写应优先生效"
        r = webadmin.set_impression(plugin, "1", reset_count=True)
        assert r["impression"]["msg_count"] == 0


def test_webadmin_sessions_listing():
    with tempfile.TemporaryDirectory() as td:
        plugin = _FakePlugin(Path(td))
        plugin.state.load("Sui:GroupMessage:1", "strinova_kanami", "42")
        plugin.state.save("Sui:GroupMessage:1", "strinova_kanami", "42",
                          MemberState())
        d = webadmin.build_sessions(plugin)
        # 会话 umo 必须**还原为规格化冒号形式**（`实例:类型:id`），
        # 不能再把 safe 目录名（下划线）直接当 umo 给页面。
        assert any("Sui:GroupMessage:1" == s["umo"] for s in d["sessions"]), \
            [s["umo"] for s in d["sessions"]]
        entry = [s for s in d["sessions"] if s["umo"] == "Sui:GroupMessage:1"][0]
        assert "strinova_kanami" in entry["personas"]
        assert "42" in entry["members"]["strinova_kanami"]


def test_webadmin_api_prefix():
    assert webadmin.API_PREFIX.startswith("/")
    assert "chatcore_neo" in webadmin.API_PREFIX
