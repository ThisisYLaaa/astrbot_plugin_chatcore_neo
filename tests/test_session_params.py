# -*- coding: utf-8 -*-
"""会话发言参数：resolve 默认值/覆盖 + slice 装配保真断言。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.session_params import (MODE_NARRATIVE, MODE_PLAIN,  # noqa: E402
                                 describe, normalize_override, resolve)


# ---------------------------------------------------------------- 解析

def test_private_defaults():
    m, nsfw = resolve(True, None, global_mode=1)
    assert (m, nsfw) == (MODE_NARRATIVE, True)
    # 全局 private_msg_mode=2 时私聊默认纯信息
    m, nsfw = resolve(True, None, global_mode=2)
    assert (m, nsfw) == (MODE_PLAIN, True)


def test_group_defaults_plain_and_no_nsfw():
    m, nsfw = resolve(False, None, global_mode=1)
    assert (m, nsfw) == (MODE_PLAIN, False), "群聊默认必须是纯信息/关NSFW"


def test_explicit_override_beats_default():
    # 群聊开剧情向 + NSFW
    assert resolve(False, {"mode": 1, "rules": ["nsfw"]}) == (MODE_NARRATIVE, True)
    # 私聊显式关 NSFW
    m, nsfw = resolve(True, {"rules": []})
    assert nsfw is False and m == MODE_NARRATIVE
    # 私聊显式纯信息但 NSFW 继承（默认开）
    m, nsfw = resolve(True, {"mode": 2})
    assert m == MODE_PLAIN and nsfw is True


def test_normalize_override_keeps_valid_keys():
    assert normalize_override({"mode": 1, "rules": ["nsfw"]}) == \
        {"mode": 1, "rules": ["nsfw"]}
    assert normalize_override({"mode": 2}) == {"mode": 2}
    # 非法 mode / 未知规则被丢弃
    assert normalize_override({"mode": 3, "rules": ["nsfw", "bogus"]}) == \
        {"rules": ["nsfw"]}
    assert normalize_override({"mode": "x"}) == {}
    assert normalize_override(None) == {}
    assert normalize_override({}) == {}


def test_describe_shape():
    d = describe(False, None, global_mode=1)
    assert d["mode"] == MODE_PLAIN and d["nsfw"] is False
    assert d["inherited"] is True
    d2 = describe(False, {"mode": 1, "rules": ["nsfw"]}, global_mode=1)
    assert d2["mode"] == MODE_NARRATIVE and d2["nsfw"] is True
    assert d2["inherited"] is False


# ---------------------------------------------------------------- 装配保真

def _assembly_has(pred: str, marker: str) -> bool:
    return marker in pred


def test_group_plain_assembly():
    from core.persona.render import load_framework
    t = load_framework(False, MODE_PLAIN)
    for key in ("纪律", "群聊场景：QQ 群", "格式底线", "纯信息格式（日常聊天）",
                "说话风格（聊天场景）", "状态标注怎么用", "理解对方的话"):
        assert key in t, f"群聊纯信息装配缺: {key}"
    assert "群聊剧情向格式" not in t
    assert "不是在讲故事" in t, "纯信息群聊要保留'不是讲故事'的定性"


def test_group_narrative_overlay_enabled():
    from core.persona.render import load_framework
    t = load_framework(False, MODE_NARRATIVE)
    assert "群聊剧情向格式" in t, "群聊剧情向必须注入允许括号那一片"
    assert "括号描写" in t
    assert "纯信息格式（日常聊天）" not in t, "剧情向不该再带纯信息专属的禁括号规则"
    # 剧情向群聊走独立场景块：不得混入"不是讲故事/不推进剧情"这类纯信息定性
    assert "群聊场景：多人在场（剧情向）" in t
    assert "推进剧情" in t and "塑造氛围" in t
    for bad in ("不是在讲故事", "不推进剧情", "日常交流"):
        assert bad not in t, f"群聊剧情向装配混进了纯信息措辞「{bad}」"


def test_private_narrative_assembly():
    from core.persona.render import load_framework
    t = load_framework(True, MODE_NARRATIVE)
    for key in ("私聊场景：面对面", "剧情向发言规范", "格式底线", "状态标注怎么用"):
        assert key in t, f"私聊剧情向装配缺: {key}"
    assert "纯信息格式（日常聊天）" not in t


def test_private_plain_assembly():
    from core.persona.render import load_framework
    t = load_framework(True, MODE_PLAIN)
    assert "私聊场景：隔着手机屏幕" in t
    assert "纯信息格式（日常聊天）" in t
    assert "§n" not in t, "括号规范不该混进纯信息模式"


def test_nsfw_slices_per_scene():
    from core.persona.render import load_nsfw_body
    assert "露骨内容规范（括号版）" in load_nsfw_body(True, MODE_NARRATIVE)
    assert "无括号模式下的表达方式" in load_nsfw_body(True, MODE_PLAIN)
    g = load_nsfw_body(False, MODE_PLAIN)
    assert "群内露骨内容的额外约束" in g


def test_assembly_cache_order_independent():
    """与旧实现同回归：先群后私再群，各次输出必须稳定。"""
    from core.persona.render import load_framework
    a = load_framework(True, MODE_NARRATIVE)
    load_framework(False, MODE_PLAIN)
    b = load_framework(True, MODE_NARRATIVE)
    assert a == b and len(b) > 0


# ---------------------------------------------------------------- NSFW 真注入

def _docs():
    from core.persona import PersonaService
    return PersonaService().all()


def test_nsfw_rules_actually_injected_when_enabled():
    """会话级 nsfw=True 必须把 NSFW 规则**真的拼进稳定段**。

    线上 bug：`load_nsfw_body` 只在 legacy_shape（装配预览/迁移校验）里被调用，
    在线请求路径从未注入 —— 于是"WebUI 里开了 NSFW，模型照旧拒绝"：
    `nsfw_enabled` 只解除了禁止语，没有任何正向许可与写法规范。
    """
    from core.assembler import AssemblyInput, PromptAssembler
    docs = _docs()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    doc = docs[0]

    on = PromptAssembler().build(AssemblyInput(
        doc=doc, is_private=True, msg_mode=MODE_NARRATIVE, nsfw_enabled=True))
    for k in ("非常主动", "露骨", "♡", "生图工具"):
        assert k in on.system_suffix, f"开启 NSFW 后稳定段缺: {k}"

    off = PromptAssembler().build(AssemblyInput(
        doc=doc, is_private=True, msg_mode=MODE_NARRATIVE, nsfw_enabled=False))
    for k in ("非常主动", "露骨", "生图工具"):
        assert k not in off.system_suffix, f"关闭 NSFW 却注入了: {k}"


def test_nsfw_injection_picks_scene_variant():
    """群聊开 NSFW 走群内策略+无括号；私聊剧情向走括号版。"""
    from core.assembler import AssemblyInput, PromptAssembler
    docs = _docs()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    doc = docs[0]
    g = PromptAssembler().build(AssemblyInput(
        doc=doc, is_private=False, msg_mode=MODE_PLAIN, nsfw_enabled=True))
    assert "群内露骨内容的额外约束" in g.system_suffix
    p = PromptAssembler().build(AssemblyInput(
        doc=doc, is_private=True, msg_mode=MODE_NARRATIVE, nsfw_enabled=True))
    assert "露骨内容规范（括号版）" in p.system_suffix
    assert "群内露骨内容的额外约束" not in p.system_suffix


def test_preview_counts_nsfw_like_runtime():
    """装配预览必须与实况一致：传 nsfw=True 时稳定段变大（多出 NSFW 规则）。"""
    from core.persona import authoring as A
    docs = _docs()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    doc = docs[0]
    off = A.preview(doc, is_private=True, msg_mode=MODE_NARRATIVE, nsfw=False)
    on = A.preview(doc, is_private=True, msg_mode=MODE_NARRATIVE, nsfw=True)
    assert on["stable_bytes"] > off["stable_bytes"]
    assert on["stable_bytes"] - off["stable_bytes"] > 1500


def test_webadmin_preview_defaults_nsfw_from_config():
    """WebUI 装配预览未显式传 nsfw 时，应跟随全局 allow_erotic_content。"""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from test_admin_api import _plugin
    from core import webadmin
    p = _plugin()
    docs = p.personas.all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    # 桩配置没写 allow_erotic_content → 走默认 True
    on = webadmin.build_preview(p, docs[0].id, is_private=True, msg_mode=1)
    assert on.get("nsfw") is True
    off = webadmin.build_preview(p, docs[0].id, is_private=True, msg_mode=1,
                                 nsfw=False)
    assert off.get("nsfw") is False
    assert on["stable_bytes"] > off["stable_bytes"], "预览没把 NSFW 规则算进去"

def test_describe_exposes_inherited_defaults():
    """页面要能显示「继承（全局：剧情向）」——默认值必须由后端给出。

    回归 2026-10-30：私聊那行原来只写「继承（全局设置）」，用户看不出
    实际生效是剧情向还是纯信息（截图里的「Sui · 私聊」看起来像特例，
    其实只是走了私聊的默认值）。
    """
    d = describe(True, None, global_mode=1)
    assert d["default_mode"] == MODE_NARRATIVE, d
    assert d["default_nsfw"] is True, d
    assert "剧情向" in d["default_label"], d
    assert d["overridden"] is False and d["inherited"] is True, d

    d2 = describe(True, None, global_mode=2)
    assert d2["default_mode"] == MODE_PLAIN, "private_msg_mode=2 时私聊继承纯信息"

    d3 = describe(False, None, global_mode=1)
    assert d3["default_mode"] == MODE_PLAIN and d3["default_nsfw"] is False, d3

    d4 = describe(False, {"mode": 1, "rules": ["nsfw"]}, global_mode=1)
    assert d4["overridden"] is True and d4["inherited"] is False, d4
    assert d4["default_mode"] == MODE_PLAIN, "有无覆盖都不影响「默认」的展示"

