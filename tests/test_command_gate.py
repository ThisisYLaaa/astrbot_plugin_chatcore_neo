# -*- coding: utf-8 -*-
"""命令输出与 LLM 闸门的回归测试。

两个真实问题
------------
1. **命令输出被分段切碎**：`/-luck` 产出的
   `你今天的运气是: 105\n你今天的运气也太好了吧！` 被 `on_decorating_result`
   按 `\n+` 切成**两条消息**，换行看起来就"没了"。
   分段是给聊天回复设计的，命令返回的是结构化多行文本，不该按这个规则切。

2. **缺了旧插件的 LLM 闸门**：旧 `chatcore_lite` 的 `hook2optimize`
   （`main.py:581-650`）在群聊里对「被唤醒词唤醒、但没 @ 也没引用机器人」的
   消息直接 `stop_event()`，不触发 LLM。chatcore_neo 没有这段，
   而且它的 `_is_hard_trigger` 把 `/` 开头当硬触发 → `/打错的命令`
   反而**必定**走 LLM，白烧 token。
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "tests"))
sys.path.insert(0, str(PLUGIN.parent))

import importlib  # noqa: E402

from test_decision_chain import FakeEvent, _neo  # noqa: E402

try:
    main = importlib.import_module(f"{PLUGIN.name}.main")
except Exception as e:  # pragma: no cover
    from run_tests import Skip
    raise Skip(f"无法导入插件入口：{e!r}")

Neo = main.ChatCoreNeoPlugin


# ---------------------------------------------------------------- A. 命令输出

def test_command_result_is_detected():
    """带 CommandFilter 的 handler 出现在 activated_handlers 里 → 判为命令。"""
    from astrbot.core.star.filter.command import CommandFilter

    class H:
        event_filters = [CommandFilter("sd", alias=None)]

    ev = FakeEvent("sd")
    ev.set_extra("activated_handlers", [H()])
    assert Neo._is_command_result(ev) is True


def test_non_command_result_is_not_detected():
    class H:
        event_filters = []

    ev = FakeEvent("你好")
    ev.set_extra("activated_handlers", [H()])
    assert Neo._is_command_result(ev) is False

    ev2 = FakeEvent("你好")               # 没有 activated_handlers
    assert Neo._is_command_result(ev2) is False


def test_command_output_is_not_segmented():
    """核心不变量：命令产出的多行文本**原样保留**。

    之前会被拆成多个 Plain（=多条消息）。
    """
    p = _neo_light()
    from astrbot.core.message.components import Plain
    from astrbot.core.star.filter.command import CommandFilter

    class H:
        event_filters = [CommandFilter("-luck", alias=None)]

    text = "你今天的运气是: 105\n你今天的运气也太好了吧！"
    ev = FakeEvent("/-luck")
    ev.set_extra("activated_handlers", [H()])
    ev.set_result = lambda r: setattr(ev, "_res", r)
    ev.get_result = lambda: ev._res

    class Res:
        chain = [Plain(text)]

    ev.set_result(Res())
    import asyncio
    asyncio.run(Neo.on_decorating_result(p, ev))
    plains = [c for c in ev._res.chain if isinstance(c, Plain)]
    assert len(plains) == 1, f"命令输出被切成了 {len(plains)} 段：{[c.text for c in plains]}"
    assert plains[0].text == text, repr(plains[0].text)
    assert "\n" in plains[0].text, "换行必须保留"


def test_command_output_is_not_voiced():
    """核心不变量：命令产出的文本**不合成语音**。

    指令返回的是结构化文本（帮助列表、数值结果、人格名…）：
    念出来没有意义，而且会把一次命令变成「一条文字 + 一条语音」两条消息。
    实测触发方式：私聊里开 `enable_tts` + `tts_only_private`，打 `/persona`，
    机器人先回一段文字、紧跟着又发一条语音。
    """
    import asyncio

    from astrbot.api.message_components import Record
    from astrbot.core.message.components import Plain
    from astrbot.core.star.filter.command import CommandFilter

    p = _prep(_neo_light())
    # TTS 真实开启；`only_private=False` 让私聊/群聊都放行，
    # 这样断言失败只可能是"没被命令判据挡住"，而不是被 TTS 自己的开关挡住。
    p.tts.enabled = True
    p.tts.only_private = False
    p.tts.max_chars = 200
    calls: list = []

    async def fake_synth(text):
        calls.append(text)
        return Record(file="fake.wav")

    p.tts.synthesize = fake_synth

    class H:
        event_filters = [CommandFilter("persona", alias=None)]

    text = "当前人格：strinova_kanami"
    ev = FakeEvent("/persona list")
    ev.set_extra("activated_handlers", [H()])
    ev.set_result = lambda r: setattr(ev, "_res", r)
    ev.get_result = lambda: ev._res

    class Res:
        chain = [Plain(text)]

    ev.set_result(Res())
    asyncio.run(Neo.on_decorating_result(p, ev))

    assert Neo._is_command_result(ev) is True, "测试前提：这条必须是命令结果"
    assert not calls, f"命令输出被送去合成语音了：{calls}"
    assert not any(isinstance(c, Record) for c in ev._res.chain), \
        f"命令结果的链子里被塞进了语音：{[type(c).__name__ for c in ev._res.chain]}"


def test_llm_reply_is_still_voiced():
    """反向确认：普通聊天回复仍要合成语音（别把 TTS 整个关掉）。"""
    import asyncio

    from astrbot.api.message_components import Record
    from astrbot.core.message.components import Plain

    p = _prep(_neo_light())
    p.tts.enabled = True
    p.tts.only_private = False
    p.tts.max_chars = 200
    calls: list = []

    async def fake_synth(text):
        calls.append(text)
        return Record(file="fake.wav")

    p.tts.synthesize = fake_synth

    ev = FakeEvent("你好呀")
    ev.set_result = lambda r: setattr(ev, "_res", r)
    ev.get_result = lambda: ev._res

    class Res:
        chain = [Plain("你好呀")]

    ev.set_result(Res())
    asyncio.run(Neo.on_decorating_result(p, ev))

    assert calls == ["你好呀"], f"普通回复没被合成语音：calls={calls}"
    assert any(isinstance(c, Record) for c in ev._res.chain), \
        "普通回复的链子里没有语音"


def test_command_segmentation_can_be_re_enabled():
    """配置 `segment_command_output=true` 时应恢复旧行为。"""
    p = _prep(_neo_light())
    p.config["segment_command_output"] = True
    from astrbot.core.message.components import Plain
    from astrbot.core.star.filter.command import CommandFilter

    class H:
        event_filters = [CommandFilter("-luck", alias=None)]

    ev = FakeEvent("/-luck")
    ev.set_extra("activated_handlers", [H()])

    class Res:
        chain = [Plain("第一句话。第二句话。")]

    res = Res()
    ev.get_result = lambda: res
    import asyncio
    asyncio.run(Neo.on_decorating_result(p, ev))
    # 开着的时候至少允许被处理（这里只要求不抛异常且链非空）。
    # 注意读**实例**：输出层重写的是 `res.chain`，读类属性等于没验证。
    assert res.chain


# ---------------------------------------------------------------- B. LLM 闸门

def test_gate_is_registered_at_high_priority():
    """闸门必须存在，且优先级高于主注入钩子（先执行才有意义）。"""
    from astrbot.core.star.star_handler import star_handlers_registry
    found = []
    for h in star_handlers_registry:
        if "chatcore_neo" in str(getattr(h, "handler_module_path", "")):
            if getattr(h, "handler_name", "") in (
                    "on_llm_request_gate", "on_llm_request"):
                found.append((h.handler_name, getattr(h, "extras_configs", None)
                              or getattr(h, "priority", None)))
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    assert "on_llm_request_gate" in src, "闸门没注册"
    assert "@filter.on_llm_request(priority=95)" in src, "闸门应在 priority=95"
    assert "@filter.on_llm_request(priority=89)" in src, "主钩子应仍是 89"


class _Ctx:
    """假 context：闸门要读 wake_prefix。"""

    def get_config(self):
        return {"wake_prefix": ["/"]}


class _LightNeo(Neo):
    """遮蔽父类的「实例化 property」，让夹具里的同名对象直接生效。

    2026-10-28 的多实例重构把 `attention` / `tts` / `segmenter` … 都改成了
    `self._rt().X`，而 `_rt()` 会去装配整套实例运行时（需要 `_segment_prompts`
    等真实属性）。`_neo()` 抄来的是 `FakePlugin` 的 `__dict__`，那里面本来
    就有这些对象，只是被父类 property 挡住了。
    这里用**非数据描述符**（类属性 = None）遮蔽它，实例属性重新可见。
    """

    attention = None
    judge = None
    segmenter = None
    poke = None
    merge = None
    tts = None
    impressions = None
    scheduler = None
    characters = None
    char_matcher = None


def _neo_light():
    p = _neo()
    p.__class__ = _LightNeo
    return p


def _prep(p):
    p.context = _Ctx()
    # 多实例重构后 `_cfg()` 走 `cfg_of()` → 读**实例**配置，
    # 而 `_neo()` 抄来的 `p.config` 是那份全局的。不指过去的话，
    # 测试里 `p.config[...] = x` 写不进 `_cfg()` 读的地方（读到的恒为默认值）。
    inst = getattr(p, "_inst_cfgs", {}).get(getattr(p, "_default_inst", ""))
    if inst is not None:
        p.config = inst
    return p


def test_gate_uses_wake_command_flag_not_is_wake():
    """判据必须是 `is_at_or_wake_command`，**不能**是 `is_wake`。

    框架在插件 handler 过滤器通过时也会置 `is_wake=True`
    （`waking_check/stage.py:223-224`），而本插件自己就注册了
    `EventMessageType.ALL` 的 `on_any_message` —— 每一条群消息都会
    `is_wake=True`。用 `is_wake` 当判据会让**普通聊天也被拦**，
    机器人在群里彻底不说话（实测过：普通聊天 `llm_request +0`）。
    """
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def on_llm_request_gate")
    seg = src[i:i + 3000]
    assert "is_at_or_wake_command" in seg, "闸门必须用 is_at_or_wake_command"
    assert "if event.is_wake:" not in seg, \
        "闸门不能用 is_wake（会被 ALL handler 误触发）"


def test_gate_blocks_unmatched_wake_in_group():
    """群聊里 `/打错的命令`（未 @/引用、也没命中命令）必须被拦下。"""
    p = _prep(_neo())
    assert p._cfg("block_unmatched_command", True) is True

    ev = FakeEvent("/不存在的命令")
    ev.is_wake = True
    ev.is_at_or_wake_command = True          # 框架被前缀唤醒时会置位

    class Req:
        pass

    import asyncio
    assert getattr(ev, "stopped", False) is False
    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is True, "未命中命令的唤醒消息应被 stop_event()"


def test_gate_does_not_block_plain_group_chat():
    """**普通群聊消息绝不能被拦** —— 这是 is_wake 判据造成的真实故障。"""
    p = _prep(_neo())
    ev = FakeEvent("今天天气不错")
    ev.is_wake = True                        # ALL handler 会把它置 True
    ev.is_at_or_wake_command = False         # 但框架不置位这个

    class Req:
        pass

    import asyncio
    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is False, "普通群聊消息不该被闸门拦下"


def test_gate_blocks_via_wake_prefix_fallback():
    """兜底路径：框架没置位 `is_at_or_wake_command` 时，看原文前缀。"""
    p = _prep(_neo())
    ev = FakeEvent("/不存在的命令")
    ev.is_wake = False
    ev.is_at_or_wake_command = False

    class Req:
        pass

    import asyncio
    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is True, "前缀兜底路径应拦住"


def test_gate_lets_normal_message_through():
    """完全没被唤醒的普通群消息：闸门不该管（交给注意力决策）。"""
    p = _prep(_neo())
    ev = FakeEvent("今天天气不错")
    ev.is_wake = False
    ev.is_at_or_wake_command = False

    class Req:
        pass

    import asyncio
    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is False, "普通消息不归闸门管"


def test_gate_lets_at_through():
    """@ 机器人的消息必须放行（即使以 / 开头）。"""
    p = _prep(_neo())
    ev = FakeEvent("/随便什么", at_bot=True)
    ev.is_wake = False
    ev.is_at_or_wake_command = True
    import asyncio

    class Req:
        pass

    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is False, "@ 机器人时不该拦"


def test_gate_lets_private_through():
    p = _neo()
    ev = FakeEvent("/随便什么", private=True, umo="Sui:FriendMessage:1")
    ev.is_wake = False
    ev.is_at_or_wake_command = True
    import asyncio

    class Req:
        pass

    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is False, "私聊不该拦"


def test_gate_can_be_disabled():
    p = _neo()
    p = _prep(p)
    p.config["block_unmatched_command"] = False
    ev = FakeEvent("/x")
    ev.is_wake = True
    ev.is_at_or_wake_command = True
    import asyncio

    class Req:
        pass

    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert ev.stopped is False, "关掉开关后不该拦"


def test_gate_helpers_exist():
    p = _neo()
    for n in ("_is_command_result", "_bot_at_mentioned", "_bot_quoted",
              "_wake_prefixes"):
        assert hasattr(p, n), f"缺少 {n}"
