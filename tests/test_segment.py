# -*- coding: utf-8 -*-
"""分段子代理测试：JSON 解析 / 内容守恒 / 阈值 / 回退 / fail-open。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.segment_agent import (GROUP_SYSTEM, PRIVATE_SYSTEM,  # noqa: E402
                                GROUP_NARRATIVE_SYSTEM,
                                PRIVATE_NARRATIVE_SYSTEM,
                                SegmentAgent, parse_segments,
                                verify_preserved)


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _agent(cfg: dict | None = None) -> SegmentAgent:
    return SegmentAgent(None, _Cfg(cfg or {}))


def _stub(provider_returns: str):
    """打桩 provider，返回一段固定文本。返回 (agent, restore_fn)。"""
    import core.segment_agent as sa

    class _R:
        completion_text = provider_returns

    class _P:
        async def text_chat(self, **kw):
            return _R()

    orig = sa.resolve_subagent_provider
    sa.resolve_subagent_provider = lambda *a, **kw: _P()
    return orig


# ---------------------------------------------------------------- JSON 解析

def test_parse_plain_array():
    assert parse_segments('["甲", "乙"]') == ["甲", "乙"]


def test_parse_with_code_fence():
    assert parse_segments('```json\n["甲", "乙"]\n```') == ["甲", "乙"]
    assert parse_segments('```\n["甲"]\n```') == ["甲"]


def test_parse_object_wrapper():
    assert parse_segments('{"segments": ["甲", "乙"]}') == ["甲", "乙"]
    assert parse_segments('{"parts": ["x"]}') == ["x"]


def test_parse_array_embedded_in_prose():
    """模型话多时，从文本里抠出数组。"""
    txt = '好的，我把它分成两段：\n["你好呀", "今天过得怎么样"]\n希望满意。'
    assert parse_segments(txt) == ["你好呀", "今天过得怎么样"]


def test_parse_single_element_array():
    assert parse_segments('["只有一段"]') == ["只有一段"]


def test_parse_rejects_non_array():
    assert parse_segments("我把它分成了两段") is None
    assert parse_segments("") is None
    assert parse_segments("```json\n{}\n```") is None


def test_parse_filters_empty_and_coerces():
    assert parse_segments('["甲", "", "  ", "乙"]') == ["甲", "乙"]
    assert parse_segments('[1, 2]') == ["1", "2"]


# ---------------------------------------------------------------- 内容守恒

def test_verify_exact_match():
    ok, ratio, exact = verify_preserved(["甲乙", "丙丁"], "甲乙丙丁")
    assert ok and exact and ratio == 1.0


def test_verify_ignores_whitespace_only():
    ok, _r, exact = verify_preserved(["甲 乙", "丙\n丁"], "甲乙丙丁")
    assert ok and exact, "只差空白不算改写"


def test_verify_rejects_rewrite():
    ok, ratio, exact = verify_preserved(["改过的内容"], "原本的内容在这里")
    assert not ok and not exact
    assert ratio < 0.95


def test_verify_rejects_dropped_text():
    ok, _r, _e = verify_preserved(["一二三"], "一二三四五六七八九")
    assert not ok, "漏字必须被拒"


def test_verify_lenient_band():
    """差异极小时（长文本里差一个字）按阈值放行，但标记为非精确。

    注意：短文本差一个字相对差异很大（"你好呀！" vs "你好呀" ≈ 0.86），
    会被 0.95 阈值拒掉 —— 这是**有意的严格**。要测放行带得用长文本。
    """
    body = "甲" * 200
    ok, ratio, exact = verify_preserved([body + "！"], body)
    assert not exact
    assert ratio > 0.95
    assert ok, f"相似度 {ratio:.4f} 应被放行"

    # 短文本差一个字应被拒（严格档）
    ok2, ratio2, exact2 = verify_preserved(["你好呀！"], "你好呀")
    assert not exact2 and not ok2, f"短文本差异应被拒（ratio={ratio2:.3f}）"


def test_verify_empty_is_rejected():
    assert verify_preserved([], "原文")[0] is False
    assert verify_preserved(["甲"], "")[0] is False


# ---------------------------------------------------------------- 开关阈值

def test_enabled_flags():
    a = _agent({"private_format_agent": True, "group_format_agent": False})
    assert a.enabled_for(is_private=True) is True
    assert a.enabled_for(is_private=False) is False


def test_threshold_defaults_and_override():
    a = _agent()
    assert a.threshold_for(is_private=True) == 0
    assert a.threshold_for(is_private=False) == 20
    assert _agent({"group_format_threshold": 50}).threshold_for(
        is_private=False) == 50


def test_short_text_skips_without_calling():
    a = _agent({"group_format_agent": True, "group_format_threshold": 100})
    out = asyncio.run(a.segment("短", is_private=False))
    assert out == []
    assert a.stats["skipped_short"] == 1
    assert a.stats["calls"] == 0, "低于阈值不该发起调用"


def test_disabled_returns_empty():
    a = _agent({"private_format_agent": False})
    assert asyncio.run(a.segment("随便什么", is_private=True)) == []
    assert a.stats["calls"] == 0


# ---------------------------------------------------------------- fail-open

def test_fails_open_without_provider():
    a = _agent()
    out = asyncio.run(a.segment("一二三四五六七八九十", is_private=True))
    assert out == [], "拿不到 provider 时返回空，由调用方回退本地规则"
    assert a.stats["fallback"] >= 1
    assert a.stats["errors"] >= 1


def test_parse_failure_counts_and_falls_back():
    import core.segment_agent as sa
    orig = _stub("我分不好，这句子太长了")
    try:
        a = _agent()
        out = asyncio.run(a.segment("一二三四五", is_private=True))
        assert out == []
        assert a.stats["parse_failed"] == 1
        assert a.stats["fallback"] == 1
    finally:
        sa.resolve_subagent_provider = orig


def test_success_path_returns_cleaned_segments():
    import core.segment_agent as sa
    orig = _stub('["你好呀。", "今天过得怎么样"]')
    try:
        a = _agent()
        out = asyncio.run(a.segment("你好呀。今天过得怎么样", is_private=True))
        # clean_segments 会去掉末尾句号（rules_base：句尾不加句号）
        assert out == ["你好呀", "今天过得怎么样"], out
        assert a.stats["used"] == 1
    finally:
        sa.resolve_subagent_provider = orig


def test_rewrite_is_rejected_and_counted():
    """**最关键的一条**：模型改了字就弃用它的结果。"""
    import core.segment_agent as sa
    orig = _stub('["我帮你改写了一下这句话"]')
    try:
        a = _agent()
        out = asyncio.run(a.segment("原本应该是这一句话", is_private=True))
        assert out == [], "内容被改写时必须返回空（调用方回退本地规则）"
        assert a.stats["rejected"] == 1
        assert a.stats["used"] == 0
    finally:
        sa.resolve_subagent_provider = orig


def test_timeout_is_fail_open():
    import core.segment_agent as sa

    class _P:
        async def text_chat(self, **kw):
            await asyncio.sleep(5)
            return None

    orig = sa.resolve_subagent_provider
    sa.resolve_subagent_provider = lambda *a, **kw: _P()
    try:
        a = _agent({"private_format_timeout": 1})
        out = asyncio.run(a.segment("一二三四五", is_private=True))
        assert out == []
        assert a.stats["fallback"] >= 1
    finally:
        sa.resolve_subagent_provider = orig


# ---------------------------------------------------------------- 提示词

def test_prompts_demand_json_and_forbid_rewrite():
    for sys_prompt in (PRIVATE_SYSTEM, GROUP_SYSTEM):
        assert "JSON" in sys_prompt
        assert "不改写" in sys_prompt or "不得增删" in sys_prompt
        assert "[" in sys_prompt, "要给数组示例"
    assert "{min_segment}" in GROUP_SYSTEM, "群聊提示要留 min_segment 占位"
    assert "群聊" in GROUP_SYSTEM


def test_report_shape():
    r = _agent().report()
    for k in ("calls", "used", "lenient", "rejected", "parse_failed",
              "skipped_short", "fallback", "errors"):
        assert k in r, k


# ---------------------------------------------------------------- 剧情向规则

def test_narrative_prompts_split_parens_and_speech():
    """剧情向提示：括号单独成段、括号间说话单独成段、说话不细拆。"""
    from core.segment_agent import (DEFAULT_PROMPTS, GROUP_NARRATIVE_SYSTEM,
                                    PRIVATE_NARRATIVE_SYSTEM)
    assert set(DEFAULT_PROMPTS) == {
        "private_plain", "private_narrative", "group_plain", "group_narrative"}
    # 信息模式不变：默认就是旧的两份
    assert DEFAULT_PROMPTS["private_plain"] == PRIVATE_SYSTEM
    assert DEFAULT_PROMPTS["group_plain"] == GROUP_SYSTEM
    for p in (PRIVATE_NARRATIVE_SYSTEM, GROUP_NARRATIVE_SYSTEM):
        assert "单独成一段" in p, "括号与说话必须各自成段"
        assert "绝不按句子/逗号/语气词再拆" in p, "说话不许细拆"
        assert "JSON" in p and "不改写" in p
        # 剧情向提示不许再带「完整句结尾才断开」这条会打架的断点规则
        assert "只在完整句结尾" not in p
    # 剧情向私聊不许再教"括号与台词放同一段"
    assert "与紧跟的台词放同一段" not in PRIVATE_NARRATIVE_SYSTEM
    # 群聊剧情向不依赖 min_segment（括号段可能很短，不该被并走）
    assert "{min_segment}" not in GROUP_NARRATIVE_SYSTEM


def test_prompt_key_routing():
    from core.segment_agent import prompt_key
    from core.session_params import MODE_NARRATIVE, MODE_PLAIN
    assert prompt_key(True, MODE_NARRATIVE) == "private_narrative"
    assert prompt_key(True, MODE_PLAIN) == "private_plain"
    assert prompt_key(False, MODE_NARRATIVE) == "group_narrative"
    assert prompt_key(False, MODE_PLAIN) == "group_plain"
    assert prompt_key(True, 0) == "private_plain", "非法模式回退纯信息"


def test_prompt_override_set_reset():
    from tempfile import mkdtemp
    from pathlib import Path
    from core.sessionmap import JsonMap
    store = JsonMap(Path(mkdtemp()) / "_segment_prompts.json")
    a = SegmentAgent(None, _Cfg({}), prompts_store=store)
    assert a.prompts()["private_narrative"]["customized"] is False
    a.set_prompt("private_narrative", "自定义剧情向提示词")
    assert a._prompt("private_narrative") == "自定义剧情向提示词"
    assert a.prompts()["private_narrative"]["customized"] is True
    # 空文本 = 清除覆盖回默认
    a.set_prompt("private_narrative", "  ")
    assert a._prompt("private_narrative") == PRIVATE_NARRATIVE_SYSTEM
    # 未知键拒绝
    assert a.set_prompt("bogus", "x") is False
    # 恢复默认
    a.set_prompt("group_plain", "群聊改过")
    a.reset_prompt()
    assert a._prompt("group_plain") == GROUP_SYSTEM
    a.set_prompt("group_plain", "又改")
    a.reset_prompt("group_plain")
    assert a._prompt("group_plain") == GROUP_SYSTEM


def test_segment_msg_mode_picks_narrative_prompt():
    """msg_mode 决定用哪份提示词（剧情向 vs 纯信息）。"""
    import core.segment_agent as sa
    captured = {}

    class _R:
        completion_text = '["（香奈美拿起袋子）", "哇，这里面装了什么呀"]'

    class _P:
        async def text_chat(self, **kw):
            captured["system"] = kw.get("system_prompt", "")
            return _R()

    orig = sa.resolve_subagent_provider
    sa.resolve_subagent_provider = lambda *a, **kw: _P()
    try:
        a = _agent()
        from core.session_params import MODE_NARRATIVE, MODE_PLAIN
        out = asyncio.run(a.segment("（香奈美拿起袋子）哇，这里面装了什么呀",
                                    is_private=True, msg_mode=MODE_NARRATIVE))
        assert out == ["（香奈美拿起袋子）", "哇，这里面装了什么呀"]
        assert "剧情向" in captured["system"]
        assert "单独成一段" in captured["system"]
        out2 = asyncio.run(a.segment("（香奈美拿起袋子）哇，这里面装了什么呀",
                                     is_private=True, msg_mode=MODE_PLAIN))
        # 纯信息走旧提示：括号与台词同一段，不出现剧情向的"单独成一段"
        assert "单独成一段" not in captured["system"]
        assert "与紧跟的台词放同一段" in captured["system"]
    finally:
        sa.resolve_subagent_provider = orig
