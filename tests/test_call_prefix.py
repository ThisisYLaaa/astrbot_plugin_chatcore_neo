# -*- coding: utf-8 -*-
"""叫醒词（call_prefix）的回归测试。

需求原文
--------
> 我希望能设置一个全局"叫醒词"，如果用户的消息开头为叫醒词，那么就会触发
> bot 的回复，但不会触发指令。假如叫醒词是"^"，那么我输入"^persona"并不会
> 触发 persona 指令，而是将去掉叫醒词后的文本作为用户消息触发 bot 的回复
> （bot 收到的消息是"persona"，没有叫醒词"^"）

## 机制依据（读码结论，见记忆 b929b5e3）
* 指令匹配发生在 `WakingCheckStage` 里，判据是 `is_at_or_wake_command`
  （`filter/command.py:192`），且匹配的是**剥完前缀后的** `message_str`。
* LLM 兜底发生在 `ProcessStage`（`process_stage/stage.py:56-61`），
  同样要求 `is_at_or_wake_command` 为真。
* 两者读的是同一个属性但发生在不同时间点 → **先剥词、再置位**即可分流。
* `req.prompt` 直接取 `event.message_str`（`astr_main_agent.py:1455`），
  所以"bot 收到的消息不含叫醒词"必须靠改 `message_str` 实现。
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

# 用**真实**消息组件：剥词那段要 isinstance(Plain)
from astrbot.api.message_components import Plain  # noqa: E402


class _Ctx:
    """假 context：闸门/剥词都要读 wake_prefix。"""

    def __init__(self, wake=None):
        self._wake = ["/"] if wake is None else wake

    def get_config(self, **kw):
        return {"wake_prefix": self._wake}


class _LightNeo(Neo):
    """遮蔽父类的「实例化 property」，让夹具里的同名对象直接生效。

    2026-10-28 的多实例重构把 `attention` / `tts` / `segmenter` … 都改成了
    `self._rt().X`，而 `_rt()` 会去装配整套实例运行时（需要 `_segment_prompts`
    等真实属性）。`_neo()` 抄来的是 `FakePlugin` 的 `__dict__`，那里面本来
    就有这些对象，只是被父类 property 挡住了。用**非数据描述符**遮蔽它。
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


def _prep(p, *, call_prefix=None, wake=None):
    p.context = _Ctx(wake)
    # 多实例重构后 `_cfg()` 走 `cfg_of()` → 读**实例**配置，
    # 而 `_neo()` 抄来的 `p.config` 是那份全局的。不指过去的话，
    # 这里写的 `call_prefix` 到不了 `_call_prefixes()` 读的地方。
    inst = getattr(p, "_inst_cfgs", {}).get(getattr(p, "_default_inst", ""))
    if inst is not None:
        p.config = inst
    if call_prefix is None:
        call_prefix = ["^"]
    p.config["call_prefix"] = call_prefix
    return p


def _group_event(text, *, comps=None, umo="Banyan:GroupMessage:1"):
    """造一条群消息。

    `comps` 显式给消息链（剥词要改第一个 Plain），不给则自动包一个 Plain。
    """
    ev = FakeEvent(text, umo=umo)
    ev.is_at_or_wake_command = False
    ev.is_wake = False
    if comps is not None:
        ev._comps = list(comps)
    else:
        ev._comps = [Plain(text)]
    return ev


# ═══════════════════════════ 配置读取 ═══════════════════════════

def test_call_prefixes_default_empty():
    """默认不启用 —— 不能改变现有行为。"""
    p = _prep(_neo_light(), call_prefix=[])
    assert p._call_prefixes() == []


def test_call_prefixes_accepts_string():
    """面板上单字符串（而非列表）也要认，避免配置手滑写错就失效。"""
    p = _prep(_neo_light(), call_prefix="^")
    assert p._call_prefixes() == ["^"]


def test_call_prefixes_drops_empty_entries():
    p = _prep(_neo_light(), call_prefix=["^", "", None])
    assert p._call_prefixes() == ["^"]


# ═══════════════════════════ 剥词与叫醒 ═══════════════════════════

def test_call_prefix_strips_and_wakes():
    """核心需求：`^persona` → bot 收到 `persona`，且被叫醒进 LLM。"""
    p = _prep(_neo_light())
    ev = _group_event("^persona")
    assert p._maybe_call_prefix(ev) is True
    assert ev.message_str == "persona", \
        f"叫醒词没剥掉，bot 会收到 {ev.message_str!r}"
    assert ev.is_at_or_wake_command is True, "没叫醒 → ProcessStage 不会发 LLM"
    assert ev.get_extra(main.X_CALL_PREFIX, "") == "^"


def test_call_prefix_strips_plain_component_too():
    """消息链里的 Plain 也要剥 —— 群聊上下文注入读的是组件，不是 message_str。"""
    p = _prep(_neo_light())
    ev = _group_event("^persona")
    p._maybe_call_prefix(ev)
    assert ev.get_messages()[0].text == "persona", \
        f"组件仍带叫醒词: {ev.get_messages()[0].text!r}"


def test_call_prefix_strips_message_obj_str():
    """`message_obj.message_str` 同步剥，避免下游按原文回显。"""
    p = _prep(_neo_light())
    ev = _group_event("^你好")
    p._maybe_call_prefix(ev)
    assert ev.message_obj.message_str == "你好"


def test_call_prefix_tolerates_space_after_prefix():
    """`^ persona`（词后带空格）也认，且不留前导空格。"""
    p = _prep(_neo_light())
    ev = _group_event("^   persona")
    assert p._maybe_call_prefix(ev) is True
    assert ev.message_str == "persona"


def test_call_prefix_keeps_trailing_content_verbatim():
    """只剥左边的词，右侧原样保留（结尾空格可能是用户有意为之）。"""
    p = _prep(_neo_light())
    ev = _group_event("^  今天天气不错  ")
    p._maybe_call_prefix(ev)
    assert ev.message_str == "今天天气不错  "


def test_call_prefix_alone_does_not_wake():
    """只有叫醒词、没正文 → 不叫醒（否则拿空 prompt 烧一次 LLM）。

    与框架"仅有唤醒前缀"的处理同口径（builtin_stars/astrbot/main.py:69-72）。
    """
    p = _prep(_neo_light())
    for t in ("^", "^   ", "^\n"):
        ev = _group_event(t)
        assert p._maybe_call_prefix(ev) is False, f"{t!r} 不该叫醒"
        assert ev.is_at_or_wake_command is False
        assert ev.message_str == t, "没命中就不该改文本"


def test_call_prefix_no_match_returns_false():
    p = _prep(_neo_light())
    ev = _group_event("今天天气不错")
    assert p._maybe_call_prefix(ev) is False
    assert ev.message_str == "今天天气不错"


def test_call_prefix_ignores_leading_whitespace():
    """消息前面有空格也算（框架自己也 strip）。"""
    p = _prep(_neo_light())
    ev = _group_event("   ^你好")
    assert p._maybe_call_prefix(ev) is True
    assert ev.message_str == "你好"


def test_call_prefix_is_idempotent():
    """二次调用不得再剥一次（防重入时把正文吃掉）。"""
    p = _prep(_neo_light())
    ev = _group_event("^你好")
    p._maybe_call_prefix(ev)
    assert p._maybe_call_prefix(ev) is False, "已处理过的消息不该再剥"
    assert ev.message_str == "你好"


# ═══════════════════════════ 与唤醒前缀的分工 ═══════════════════════════

def test_wake_prefix_takes_precedence():
    """消息以唤醒词（/）开头 → 走指令路径，叫醒词不干预。

    否则 `/persona` 会变成"只回复"，指令全部失效。
    """
    p = _prep(_neo_light())
    ev = _group_event("/persona list")
    assert p._maybe_call_prefix(ev) is False
    assert ev.message_str == "/persona list", "唤醒词消息的文本不该被动"
    assert ev.is_at_or_wake_command is False


def test_wake_prefix_precedence_even_when_custom():
    """唤醒前缀改成 ["!"] 后同理：`!x` 是指令，`^x` 才是叫醒。"""
    p = _prep(_neo_light(), wake=["!"])
    assert p._maybe_call_prefix(_group_event("!persona")) is False
    ev = _group_event("^persona")
    assert p._maybe_call_prefix(ev) is True
    assert ev.message_str == "persona"


def test_call_prefix_not_in_wake_prefix():
    """叫醒词与唤醒前缀重叠时，叫醒词必须让位。

    这是配置层面的坑：把 ^ 同时填进 wake_prefix，框架会先剥它并置位，
    指令过滤器就能命中 → 叫醒词失效。测试把这条不变式钉住。
    """
    p = _prep(_neo_light(), wake=["/", "^"])
    ev = _group_event("^persona")
    # 已被"唤醒前缀"形状命中（这里是模拟框架已经置位的情形）
    ev.is_at_or_wake_command = True
    assert p._maybe_call_prefix(ev) is False, \
        "框架已唤醒的消息，叫醒词不该插手"


def test_already_woken_message_is_left_alone():
    """@ 机器人 / 引用唤起的消息：叫醒词不干预。"""
    from astrbot.api.message_components import At
    p = _prep(_neo_light())
    ev = _group_event("^你好", comps=[At(qq="999"), Plain("^你好")])
    ev.is_at_or_wake_command = True
    assert p._maybe_call_prefix(ev) is False
    assert ev.message_str == "^你好"


# ═══════════════════════════ 私聊 / 合成事件 ═══════════════════════════

def test_private_message_still_strips():
    """私聊也要剥词 —— 否则机器人收到的永远是 "^xxx"。"""
    p = _prep(_neo_light())
    ev = FakeEvent("^你好", umo="Sui:FriendMessage:1")
    ev._comps = [Plain("^你好")]
    ev.is_at_or_wake_command = False
    assert p._maybe_call_prefix(ev) is True
    assert ev.message_str == "你好"


def test_simulated_event_is_skipped():
    """合成事件（主动消息/戳一戳回应）不剥词。

    它们的正文是插件自己造的，不会有叫醒词；剥了反而可能吃掉正文首字符。
    """
    from core import compat
    p = _prep(_neo_light())
    ev = _group_event("^你好")
    compat.mark_simulated(ev, source="proactive")
    assert p._maybe_call_prefix(ev) is False
    assert ev.message_str == "^你好"


def test_gutoken_is_skipped():
    """gutoken 的 token 估算请求不能有副作用。"""
    from core import compat
    p = _prep(_neo_light())
    ev = _group_event("^你好")
    compat.mark_simulated(ev)  # 先打标记，再补 gutoken 字段
    setattr(ev, compat.GUTOKEN_ACTIVE, True)
    assert p._maybe_call_prefix(ev) is False


def test_plugin_disabled_is_noop():
    p = _prep(_neo_light())
    p.config["enabled"] = False
    ev = _group_event("^你好")
    assert p._maybe_call_prefix(ev) is False
    assert ev.message_str == "^你好"


# ═══════════════════════════ 与命令闸门的配合 ═══════════════════════════

def test_gate_lets_call_prefix_through():
    """**关键**：叫醒词消息不能被命令闸门当成"未命中命令"拦掉。

    它的形状（is_at_or_wake_command=True、没 @、没引用）与
    "用户打了 / 却没命中命令"完全一致，不排除就永远发不出回复。
    """
    import asyncio
    p = _prep(_neo_light())
    ev = _group_event("^persona")
    assert p._maybe_call_prefix(ev) is True
    assert ev.message_str == "persona"

    class Req:
        pass

    assert ev.is_at_or_wake_command is True
    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert getattr(ev, "stopped", False) is False, \
        "叫醒词消息被闸门拦了 —— 机器人会完全不回复"


def test_gate_still_blocks_plain_wake_prefix_message():
    """反向确认：普通唤醒前缀消息仍要被闸门拦（别把闸门放太松）。"""
    import asyncio
    p = _prep(_neo_light())
    ev = _group_event("不存在的命令")
    ev.is_at_or_wake_command = True     # 框架被 / 唤醒的形态
    ev.is_wake = True

    class Req:
        pass

    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert getattr(ev, "stopped", False) is True, \
        "未命中命令的唤醒消息仍应被拦（闸门被放太松了）"


# ═══════════════════════════ 与注意力的配合 ═══════════════════════════

def test_attention_skips_call_prefix_message():
    """叫醒词消息不掷骰子 —— 它是硬触发，再掷一次会放大/压低回复率。"""
    p = _prep(_neo_light())
    ev = _group_event("^你好")
    p._maybe_call_prefix(ev)
    assert p._maybe_attention_wake(ev) is False
    assert not ev.get_extra(main.X_ATTN_WAKE, False), "不该走软触发路径"


def test_hard_trigger_recognizes_call_prefix():
    """`_is_hard_trigger` 必须认叫醒词消息。

    剥词后 `message_str` 已经不含 "^"，原来那条硬编码的 `startswith("/")`
    判据再也认不出它 → 会被当成普通群消息去掷骰子。
    """
    p = _prep(_neo_light())
    ev = _group_event("^你好")
    p._maybe_call_prefix(ev)
    assert p._is_hard_trigger(ev) is True, \
        "叫醒词消息必须是硬触发（否则要走注意力概率）"


# ═══════════════════════════ 接线检查 ═══════════════════════════

def test_on_any_message_calls_call_prefix():
    """`on_any_message` 必须真的调用叫醒词处理，且在注意力判定**之前**。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def on_any_message")
    assert i > 0
    j = src.find("async def ", i + 10)
    seg = src[i:j]
    assert "_maybe_call_prefix" in seg, \
        "on_any_message 里没有叫醒词处理 —— 功能根本没接线"
    k_call = seg.find("_maybe_call_prefix")
    k_attn = seg.find("_maybe_attention_wake")
    assert k_attn == -1 or k_call < k_attn, \
        "叫醒词必须在注意力判定之前（否则要掷两次骰子）"


def test_gate_checks_call_prefix_extra():
    """闸门源码里必须认 X_CALL_PREFIX。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def on_llm_request_gate")
    seg = src[i:i + 6000]
    assert "X_CALL_PREFIX" in seg, "闸门没有放行叫醒词消息"


def test_schema_declares_call_prefix():
    """配置项必须声明，否则用户在 WebUI 里改不到（幽灵开关）。"""
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "call_prefix" in schema, "schema 缺少 call_prefix"
    item = schema["call_prefix"]
    assert item["type"] == "list", item
    assert item["default"] == [], "默认必须为空（不改变现有行为）"


def test_hard_trigger_checks_call_prefix():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("def _is_hard_trigger")
    seg = src[i:i + 2600]
    assert "X_CALL_PREFIX" in seg, \
        "_is_hard_trigger 没认叫醒词（剥词后认不出它）"
