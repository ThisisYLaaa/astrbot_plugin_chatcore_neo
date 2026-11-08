# -*- coding: utf-8 -*-
"""给「注意力触发」补回归测试。

对应今天查出的移植回归：
  * `_is_hard_trigger` 用 `is_wake_up`（== `is_wake`），而本插件的
    `EventMessageType.ALL` 处理器让每条群消息 `is_wake` 都是 True
    → 每条消息都被当成硬触发 → 注意力/判定器/隐性意图全部失效
  * `on_any_message` 里**没有**注意力判定块（旧 chatcore_lite 有）
    → 普通群消息走不到 `on_llm_request`，注意力永远不会被评估
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


def _prep(p):
    """给假插件补上注意力判定需要的零件。"""

    class Ctx:
        def get_config(self, **kw):
            return {"wake_prefix": ["/"]}

    p.context = Ctx()
    return p


# ───────────────────────────────────────────── _is_hard_trigger

def test_hard_trigger_ignores_is_wake():
    """**核心回归**：`is_wake` 为 True 时不能被当成硬触发。

    本插件注册了 `EventMessageType.ALL` 的钩子，`WakingCheckStage:223-224`
    会给每条群消息置 `is_wake=True`。若拿它当硬触发判据，
    注意力/判定器/隐性意图全部失效（实测 `judge_calls` 恒为 0）。
    """
    p = _prep(_neo())
    ev = FakeEvent("今天天气不错")
    ev.is_wake = True                 # 框架会置成 True
    ev.is_at_or_wake_command = False  # 但这条不是被 @/前缀唤醒的
    assert p._is_hard_trigger(ev) is False, \
        "is_wake 为 True 时不该判为硬触发（会导致注意力永远不生效）"


def test_hard_trigger_still_detects_at_bot():
    p = _prep(_neo())
    ev = FakeEvent("你好", at_bot=True)
    ev.is_wake = True
    ev.is_at_or_wake_command = True
    assert p._is_hard_trigger(ev) is True, "@ 机器人必须仍是硬触发"


def test_hard_trigger_still_detects_slash():
    p = _prep(_neo())
    ev = FakeEvent("/随便什么")
    ev.is_wake = True
    ev.is_at_or_wake_command = True
    assert p._is_hard_trigger(ev) is True, "/ 开头必须仍是硬触发"


# ───────────────────────────────────────────── _addresses_other_user

def test_addresses_other_user_true():
    """@ 了别人的消息 → 软触发不插话。"""
    from astrbot.api.message_components import At

    p = _prep(_neo())
    ev = FakeEvent("作业写完了吗", comps=[At(qq="12345")])
    assert p._addresses_other_user(ev) is True


def test_addresses_other_user_false_for_all_and_self():
    from astrbot.api.message_components import At

    p = _prep(_neo())
    for qq in ("all", "999"):          # FakeEvent 默认 self_id="999"
        ev = FakeEvent("说话", comps=[At(qq=qq)])
        assert p._addresses_other_user(ev) is False, f"At({qq}) 不该算「发给别人」"


# ───────────────────────────────────────────── _maybe_attention_wake

def _fake(umo="Banyan:GroupMessage:1"):
    ev = FakeEvent("随便聊聊")
    ev.is_at_or_wake_command = False
    ev.is_wake = True
    ev.umo = umo
    return ev


def test_attention_wake_skips_already_woken():
    """已被框架唤醒（@/前缀）的消息不掷骰子 —— 硬触发不走概率。"""
    p = _prep(_neo())
    ev = _fake()
    ev.is_at_or_wake_command = True
    assert p._maybe_attention_wake(ev) is False
    assert not ev.get_extra(main.X_ATTN_WAKE, False)


def test_attention_wake_skips_command_prefix():
    """以唤醒前缀开头的指令消息不参与注意力。"""
    p = _prep(_neo())
    assert p._maybe_attention_wake(FakeEvent("/sd img xxx")) is False


def test_attention_wake_skips_private():
    """私聊不走注意力（框架本来必回）。"""
    p = _prep(_neo())
    ev = FakeEvent("在吗", private=True, umo="Sui:FriendMessage:1")
    assert p._maybe_attention_wake(ev) is False


def test_attention_wake_skips_addressed_to_other():
    from astrbot.api.message_components import At

    p = _prep(_neo())
    ev = FakeEvent("随便聊聊", comps=[At(qq="12345")])
    assert p._maybe_attention_wake(ev) is False


def test_attention_wake_hit_wakes_the_event():
    """**核心不变量**：命中时必须把 `is_at_or_wake_command` 置 True。

    否则框架的 `ProcessStage` 不会发起 LLM 请求
    （`process_stage/stage.py:56-61`），"主动接话"永远发不出去。
    """
    p = _prep(_neo())
    # 把概率拉到 1，保证掷中
    p.attention = type("A", (), {
        "record_others_message": lambda self, u: None,
        "current_probability": lambda self, u: 1.0,
        "should_respond": lambda self, u: True,
        "record_interaction": lambda self, u: None,
        "record_soft_hit": lambda self, u: None,
        "record_soft_miss": lambda self, u: None,
        "_state": lambda self, u: {"soft_misses": 0},
    })()
    ev = _fake()
    assert p._maybe_attention_wake(ev) is True
    assert ev.get_extra(main.X_ATTN_WAKE, False) is True
    assert ev.is_at_or_wake_command is True, "命中必须叫醒消息"
    assert ev.is_wake is True


def test_attention_wake_miss_changes_nothing():
    """不命中时**不得**改动事件 —— 框架照旧跳过，零开销。"""
    p = _prep(_neo())
    p.attention = type("A", (), {
        "record_others_message": lambda self, u: None,
        "current_probability": lambda self, u: 0.0,
        "should_respond": lambda self, u: False,
        "record_interaction": lambda self, u: None,
        "record_soft_hit": lambda self, u: None,
        "record_soft_miss": lambda self, u: None,
        "_state": lambda self, u: {"soft_misses": 1},
    })()
    ev = _fake()
    assert p._maybe_attention_wake(ev) is False
    assert not ev.get_extra(main.X_ATTN_WAKE, False)
    assert ev.is_at_or_wake_command is False, "不命中不能叫醒"


# ───────────────────────────────────────────── 闸门放行

def test_gate_allows_attention_woken():
    """闸门必须放行「注意力唤醒」的消息。

    否则它会按"未命中命令的唤醒消息"拦掉 —— 旧 chatcore_lite 用
    `_ALLOW_KEY` 解决同一问题（`main.py:44/348/348`）。
    """
    import asyncio

    p = _prep(_neo())
    p.config["block_unmatched_command"] = True
    ev = _fake()
    ev.is_at_or_wake_command = True          # 被叫醒了 → 闸门本会拦
    ev.set_extra(main.X_ATTN_WAKE, True)

    class Req:
        pass

    asyncio.run(Neo.on_llm_request_gate(p, ev, Req()))
    assert not getattr(ev, "stopped", False), "注意力唤醒的消息不该被闸门拦"


# ───────────────────────────────────────────── 接线检查

def test_on_any_message_calls_attention():
    """`on_any_message` 必须真的调用注意力判定 —— 这是本次补回的移植遗漏。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def on_any_message")
    j = src.find("async def ", i + 10)
    seg = src[i:j]
    assert "_maybe_attention_wake" in seg, \
        "on_any_message 里没有注意力判定 —— 普通群消息永远走不到 on_llm_request"


def test_no_is_wake_up_in_hard_trigger():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("def _is_hard_trigger")
    j = src.find("def ", src.find("return False", i))
    seg = src[i:i + 2600]
    body = "\n".join(l for l in seg.splitlines()
                     if not l.strip().startswith("#"))
    assert "is_wake_up" not in body, \
        "`_is_hard_trigger` 不该用 is_wake_up（会让每条消息都成硬触发）"

def test_attention_wake_skips_empty_message():
    """**回归**：没有任何内容的消息不该掷骰子。

    纯表情/空消息的 message_str 是空串；实测会命中并走进整条链路
    （日志里 消息=''）。纯图片消息有组件，仍算有内容。
    """
    p = _prep(_neo())
    assert p._maybe_attention_wake(FakeEvent("")) is False
    assert p._maybe_attention_wake(FakeEvent("   ")) is False


def test_attention_wake_allows_image_only_message():
    """纯图片消息有组件 → 不算"没内容"，仍可参与。"""
    from astrbot.api.message_components import Image

    p = _prep(_neo())
    p.attention = type("A", (), {
        "record_others_message": lambda self, u: None,
        "current_probability": lambda self, u: 1.0,
        "should_respond": lambda self, u: True,
        "record_interaction": lambda self, u: None,
        "record_soft_hit": lambda self, u: None,
        "record_soft_miss": lambda self, u: None,
        "_state": lambda self, u: {"soft_misses": 0},
    })()
    ev = FakeEvent("", comps=[Image(file="x.jpg")])
    assert p._maybe_attention_wake(ev) is True
