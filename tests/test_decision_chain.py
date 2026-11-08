# -*- coding: utf-8 -*-
"""接话决策链的回归测试 —— 直接执行 main.py 的钩子函数体。

为什么需要它
------------
子代理审计时统计过：全部 21 个测试模块里
`_is_hard_trigger` / `_decide_reply` / `on_llm_request` / `on_llm_response` /
`on_decorating_result` / `persona_command` / `_handle_recall` / `on_any_message`
**零命中** —— main.py 的钩子函数体从没被任何测试跑过。
`test_plugin_wiring.py` 只做 AST 静态核对（只看 `self.X.Y()` 形式），
**看不见 staticmethod 里的 `self`**。

于是这个真实 bug 溜了过去：
    main.py  `@staticmethod def _is_hard_trigger(event)` 里写了 `self._cfg(...)`
    → 每条"无 @、非唤醒、不以 / 开头"的普通群消息都抛 NameError
    → 注意力 / 不判定 / 隐性意图 / 读空气 **全部从未执行**
    → 异常被 fail-open 吞掉后 return True → 机器人对群里每条消息都回复

实测（审计方 20 条普通群消息）：errors=20、attention.sessions=0、judge.judge_calls=0。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "tests"))
# main.py 内部用相对导入（from .core import ...），所以必须**按包名**导入，
# 直接 `import main` 会 ImportError: attempted relative import with no known
# parent package —— 这正是 AstrBot 加载插件的方式。
sys.path.insert(0, str(PLUGIN.parent))

import importlib  # noqa: E402

from test_admin_api import _plugin  # noqa: E402

try:
    plugin_main = importlib.import_module(f"{PLUGIN.name}.main")
except Exception as e:  # pragma: no cover
    from run_tests import Skip
    raise Skip(f"无法导入插件入口：{e!r}")

Neo = plugin_main.ChatCoreNeoPlugin

# 用**真实**消息组件：main.py 里全是 isinstance 判断，假组件过不了
from astrbot.api.message_components import At, Poke, Plain  # noqa: E402


class _Comp:
    """最小消息组件。"""

    def __init__(self, kind: str, **kw):
        self.kind = kind
        self.__dict__.update(kw)

    def __repr__(self):
        return f"<{self.kind}>"


class _RawMsg:
    def __init__(self, raw=None, message_id="m1", message_str=""):
        self.raw_message = raw or {}
        self.message_id = message_id
        # 真实的 `AstrBotMessage` 有 `message_str`（适配器写的原文）。
        # 群里多个下游读它（群聊上下文注入、引用回显），剥词类功能必须能改它。
        self.message_str = message_str


class FakeEvent:
    """够 main.py 的决策路径用。"""

    def __init__(self, text="随便聊聊", *, umo="Sui:GroupMessage:1",
                 private=False, at_bot=False, wake=False, admin=False,
                 raw=None, comps=None, uid="42", self_id="999"):
        self._text = text
        self.unified_msg_origin = umo
        self._private = private
        self._at = at_bot
        self._wake = wake
        self._admin = admin
        self._uid = uid
        self._self = self_id
        self.message_obj = _RawMsg(raw, message_str=text)
        self._comps = list(comps or [])
        self.stopped = False
        self._extra: dict = {}
        self.sent: list = []
        # `message_str` 在真实框架里是**普通属性**
        # （`astr_message_event.py:45` `self.message_str = message_str`），
        # 管道和插件都能改它。这里做成 property 只是为了让 `self._text`
        # 与 `self.message_str` 保持同一份数据 —— 原来是**只读** property，
        # 导致剥词类功能在测试里必然失败：那是测试基建与真实框架不一致，
        # 不是被测代码的错。
        self.message_str = text

    @property
    def message_str(self):
        return self._text

    @message_str.setter
    def message_str(self, value):
        self._text = value

    @property
    def is_wake_up(self):
        return self._wake

    def get_messages(self):
        comps = list(self._comps)
        if self._at:
            comps.append(At(qq=self._self))
        return comps

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._admin

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return f"用户{self._uid}"

    def get_self_id(self):
        return self._self

    def get_group_id(self):
        return "" if self._private else "1"

    def get_extra(self, k=None, default=None):
        return self._extra.get(k, default) if k else dict(self._extra)

    def set_extra(self, k, v):
        self._extra[k] = v

    def stop_event(self):
        self.stopped = True

    def get_result(self):
        return None

    def plain_result(self, t):
        return _Comp("Plain", text=t)

    async def send(self, r):
        self.sent.append(r)


def _doc(p):
    docs = p.personas.all()
    if not docs:
        from run_tests import Skip
        raise Skip("没有人格数据")
    return docs[0]


class _LightNeo(Neo):
    """遮蔽父类的「实例化 property」，让夹具里的同名对象直接生效。

    2026-10-28 的多实例重构把 `attention` / `tts` / `segmenter` / `judge` /
    `poke` / `merge` / `impressions` / `scheduler` / `characters` /
    `char_matcher` 全改成了 `@property → self._rt().X`，而 `_rt()` 会去装配
    整套实例运行时（需要 `_segment_prompts` 等真实属性）。
    `FakePlugin.__dict__` 里本来就有这些对象，只是被父类 property 挡住了。
    这里用**非数据描述符**（类属性 = None）遮蔽，实例属性重新可见。
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


def _neo():
    """真实方法 + 临时目录属性。

    FakePlugin 只有**属性**（数据都指向临时目录），没有 main.py 的方法。
    用 `_LightNeo.__new__` 造一个壳，再把属性搬过去 —— 这样既能调到真实的
    `_decide_reply` / `_is_hard_trigger` / `on_llm_request`，又不会碰真实数据。
    """
    fake = _plugin()
    obj = _LightNeo.__new__(_LightNeo)
    obj.__dict__.update(fake.__dict__)
    # `_cfg()` 走 `cfg_of()` → 读**实例**配置；把 `config` 指向它，
    # 否则测试里 `p.config[...] = x` 写不到 `_cfg()` 读的地方。
    inst = obj.__dict__.get("_inst_cfgs", {}).get(obj.__dict__.get("_default_inst", ""))
    if inst is not None:
        obj.__dict__["config"] = inst
    return obj


# ═══════════════════════════ 结构性断言 ═══════════════════════════

def test_is_hard_trigger_is_not_staticmethod():
    """`_is_hard_trigger` 用到了 self（读配置），**不能**是 staticmethod。

    这条断言本可以在事故当天就拦住它 —— 它不需要跑任何东西。
    """
    import inspect
    fn = Neo.__dict__.get("_is_hard_trigger")
    assert fn is not None, "找不到 _is_hard_trigger"
    assert not isinstance(fn, staticmethod), (
        "_is_hard_trigger 是 staticmethod，但函数体里用了 self._cfg(...) "
        "→ 每条普通群消息都会 NameError")


def test_no_staticmethod_uses_self():
    """全类扫描：任何 `@staticmethod` 的函数体里都不许出现 `self`。

    这是**通用**守卫，能拦住同类错误，而不只是这一个函数。
    """
    import ast
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(d, ast.Name) and d.id == "staticmethod"
                       for d in fn.decorator_list):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id == "self":
                    bad.append(f"{cls.name}.{fn.name} (行 {node.lineno})")
    assert not bad, (
        "以下 @staticmethod 的函数体里用了 self —— 运行时会 NameError：\n  "
        + "\n  ".join(sorted(set(bad))))


# ═══════════════════════════ 行为断言 ═══════════════════════════

def test_normal_group_message_does_not_raise():
    """普通群消息（无 @、非唤醒、非指令）走决策链**不能抛异常**。

    这正是审计复现出的那条：20 条普通消息 → 20 次 NameError。
    """
    p = _neo()
    doc = _doc(p)
    ev = FakeEvent("今天天气不错")          # 普通发言
    r = asyncio.run(Neo._decide_reply(p, ev, ev.unified_msg_origin, "42",
                                      doc, ev.message_str))
    assert isinstance(r, bool), r
    assert p._stats.get("errors", 0) == 0, \
        f"决策链抛异常了: {p._last_error}"
    assert p._last_error == "", p._last_error


def test_normal_group_message_records_attention():
    """决策链必须真的把注意力状态记下来（旧 bug 下 sessions 恒为 0）。"""
    p = _neo()
    doc = _doc(p)
    umo = "Sui:GroupMessage:1"
    for _ in range(3):
        ev = FakeEvent("随便说说")
        asyncio.run(Neo._decide_reply(p, ev, umo, "42", doc, ev.message_str))
    snap = p.attention.snapshot()
    assert snap, "注意力状态为空 —— 说明决策链根本没执行到 record_*"
    assert snap[0]["umo"] == umo


def test_hard_trigger_paths_still_work():
    """@ 机器人与 `/` 开头必须仍是硬触发，且不报错。

    **`wake=True`（= `is_wake`）不算硬触发** —— 本插件注册了
    `EventMessageType.ALL` 的 `on_any_message`，框架的
    `WakingCheckStage:223-224` 会给**每条群消息**置 `is_wake=True`，
    拿它当硬触发判据会让注意力/不回复判定/隐性意图三条链路全部失效
    （实测 `judge_calls` 恒为 0）。旧 chatcore_lite 的 `_is_hard_trigger`
    本来也没有这个判据，是移植时新加进来的回归。
    详见 `test_attention_wake.py`。
    """
    p = _neo()
    doc = _doc(p)
    for kw in ({"at_bot": True}, {}):
        ev = FakeEvent("/persona list" if not kw else "在吗", **kw)
        r = asyncio.run(Neo._decide_reply(p, ev, "Sui:GroupMessage:1", "42",
                                          doc, ev.message_str))
        assert r is True, (kw, r)
    assert p._stats.get("errors", 0) == 0, p._last_error


def test_judge_is_reachable_on_non_hard_trigger():
    """注意力命中时，决策链必须能走到判定器。

    `attention_hard_trigger_force` 只作用于硬触发；普通消息是否回由
    「注意力 → 判定器」两级决定，所以判定器只在注意力命中后才会被咨询。
    审计发现旧 bug 下 `judge.judge_calls` 恒为 0 —— 因为异常在那之前就抛了。
    """
    p = _neo()
    doc = _doc(p)
    # AttentionManager 在 FakePlugin 构造时就定型了，改 p.config 影响不到它，
    # 必须直接改实例参数把概率顶到 1.0，让 should_respond 必定命中。
    p.attention.bubble_base = 1.0
    p.attention.active_cap = 1.0
    ev = FakeEvent("继续说说看")
    asyncio.run(Neo._decide_reply(p, ev, "Sui:GroupMessage:1", "42",
                                  doc, ev.message_str))
    rep = p.judge.report()
    assert rep.get("judge_calls", 0) >= 1, \
        f"注意力命中后仍未走到判定器: {rep}"


def test_decide_reply_never_swallows_by_exception_path():
    """决策链即使内部出错也必须返回 bool，且错误计数可见（不能静默）。"""
    p = _neo()
    doc = _doc(p)

    class BadEvent(FakeEvent):
        def get_messages(self):
            raise RuntimeError("模拟内部故障")

    ev = BadEvent("x")
    r = asyncio.run(Neo._decide_reply(p, ev, "Sui:GroupMessage:1", "42",
                                      doc, "x"))
    assert isinstance(r, bool), "异常路径也必须返回 bool（不能吞掉消息）"


# ═══════════════════════════ poke ═══════════════════════════

def test_poke_detected_via_sub_type():
    """回归：OneBot 的戳一戳是 `notice_type=notify` + `sub_type=poke`，
    不是 `notice_type=poke`（旧实现判错字段 → 整条功能死）。"""
    from core.interactions import is_poke_event
    ev = FakeEvent("", raw={"post_type": "notice", "notice_type": "notify",
                            "sub_type": "poke", "target_id": "999"})
    assert is_poke_event(ev) is True


def test_poke_detected_via_component():
    """适配器会往消息链塞 Poke 组件，这条路更可靠。"""
    from core.interactions import is_poke_event, poke_target
    ev = FakeEvent("", comps=[Poke(id="999")])
    assert is_poke_event(ev) is True
    assert poke_target(ev) == "999"


def test_poke_old_field_still_accepted():
    from core.interactions import is_poke_event
    ev = FakeEvent("", raw={"post_type": "notice", "notice_type": "poke"})
    assert is_poke_event(ev) is True


def test_poke_target_read_from_raw():
    from core.interactions import poke_target
    ev = FakeEvent("", raw={"post_type": "notice", "notice_type": "notify",
                            "sub_type": "poke", "target_id": "12345"})
    assert poke_target(ev) == "12345"


def test_only_self_now_actually_matters():
    """`poke_only_self` 两个取值必须产生不同结果（旧实现硬编码 True）。"""
    from core.interactions import PokeHandler
    strict = PokeHandler(enabled=True, probability=1.0, cooldown=0.0,
                         only_self=True)
    loose = PokeHandler(enabled=True, probability=1.0, cooldown=0.0,
                        only_self=False)
    assert strict.should_reply("u", target_is_self=False) is False
    assert loose.should_reply("u", target_is_self=False) is True


def test_simulated_poke_event_is_excluded_so_count_is_not_doubled():
    """一次戳只能让 `should_reply` 被调用一次。

    戳一戳 llm 模式的回应方式，是造一条**正文为 `POKE_NOTICE`** 的合成
    事件重入管道；而 `is_poke_event` 的第 ③ 条文本兜底恰好认这个字符串
    —— 合成事件会被再判成一次戳一戳，于是：

    * 线上实测 `seen` 一次戳涨 **2**（`replied` 1 + `skipped_cooldown` 1）；
    * `poke_cool_down_seconds = 0` 时会**无限套娃**。

    这里按 `main.on_any_message` 的真实条件走一遍，断言只算一次。
    """
    from core import compat
    from core.interactions import (POKE_NOTICE, PokeHandler, is_poke_event,
                                   poke_target)

    ph = PokeHandler(enabled=True, probability=1.0, cooldown=0.0,
                     only_self=True)

    real = FakeEvent("", comps=[Poke(id="999")], self_id="999")
    synth = FakeEvent(POKE_NOTICE)              # 合成事件：正文就是那句提示
    compat.mark_simulated(synth, source="poke")

    # 形态上两者都像戳一戳，只能靠"是不是合成事件"区分
    assert is_poke_event(real) is True
    assert is_poke_event(synth) is True
    assert compat.is_simulated(real) is False
    assert compat.is_simulated(synth) is True

    replied = 0
    for ev in (real, synth):
        # ↓ 与 main.on_any_message 里的条件逐字一致
        if is_poke_event(ev) and not compat.is_simulated(ev):
            if ph.should_reply(ev.unified_msg_origin,
                               target_is_self=(poke_target(ev)
                                               == ev.get_self_id())):
                replied += 1
    assert replied == 1, f"一次戳回应了 {replied} 次（合成事件没被排除）"
    assert ph.stats["seen"] == 1, f"seen={ph.stats['seen']}，应为 1（没翻倍）"


def test_gate_lets_simulated_events_through():
    """合成事件（戳一戳回应 / 主动消息）**不能**被命令闸门拦掉。

    线上实录（群里戳一戳）：
        戳一戳已重入管道 umo='...:GroupMessage:200000001' 成功=True
        闸门拦截：唤醒前缀唤醒，未 @/引用机器人　文本='（对方戳了你一下）'

    根因：`synthesize_event` 必然把 `is_at_or_wake_command` 置真，而闸门第 ③ 条
    正是拿它判"被唤醒前缀唤醒"。私聊因为"私聊一律放行"那条不会复现 ——
    所以这个 bug 只在群里出现。
    """
    from core import compat
    obj = _neo()

    def _mk() -> FakeEvent:
        ev = FakeEvent("（对方戳了你一下）", umo="Sui:GroupMessage:1",
                       wake=True)
        ev.is_at_or_wake_command = True       # 合成事件的真实形态
        return ev

    # ① 先确认这种形态**确实会被**拦 —— 否则下面的断言没有意义
    plain = _mk()
    asyncio.run(Neo.on_llm_request_gate(obj, plain, None))
    assert plain.stopped is True, "这个形态本该被闸门拦下，测试前提不成立"

    # ② 打上合成标记后必须放行
    synth = _mk()
    compat.mark_simulated(synth, source="poke")
    asyncio.run(Neo.on_llm_request_gate(obj, synth, None))
    assert synth.stopped is False, "合成事件被闸门拦掉了（群里戳一戳/主动消息会哑）"


def test_on_any_message_does_not_count_simulated_as_user_activity():
    """合成事件不算"用户说话了"。

    否则戳一戳回应 / 主动开口会把自己刚说的话记成用户活动，
    空闲计时器被自己清零 —— 页面上"空闲 X 分钟"永远是 0，
    主动消息的 idle 判据也被自己的上一句话重置。
    """
    from core import compat
    obj = _neo()
    umo = "Sui:GroupMessage:1"

    real = FakeEvent("在吗", umo=umo)
    asyncio.run(Neo.on_any_message(obj, real))
    ts_real = obj.scheduler.state.activity(umo).last_user_ts
    assert ts_real > 0, "真实消息应当记录用户活动时间"

    # 合成事件：把时间戳清掉再看它会不会又被写上
    obj.scheduler.state.activity(umo).last_user_ts = 0.0
    synth = FakeEvent("（对方戳了你一下）", umo=umo)
    compat.mark_simulated(synth, source="poke")
    asyncio.run(Neo.on_any_message(obj, synth))
    assert obj.scheduler.state.activity(umo).last_user_ts == 0.0, \
        "合成事件被当成了用户活动"


def test_poke_branch_in_main_guards_against_simulated_events():
    """静态核对：`on_any_message` 里的戳一戳分支必须带 `is_simulated` 判断。

    少了它就会退化成上面的翻倍 / 套娃。这条断言防的是"以后有人顺手删掉"。
    """
    import ast
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = {n.func.attr for n in ast.walk(node.test)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        calls |= {n.func.id for n in ast.walk(node.test)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "is_poke_event" not in calls:
            continue
        text = ast.unparse(node.test)
        found.append((node.lineno, text))
        assert "is_simulated" in text, (
            f"main.py:{node.lineno} 的戳一戳分支没排除合成事件，"
            f"会导致统计翻倍且在冷却为 0 时无限套娃: {text}")
    assert found, "没找到戳一戳分支（条件变了？请同步更新本测试）"


# ═══════════════ on_llm_response：合成事件不能踩空局部名 ═══════════════

class _Resp:
    """够 on_llm_response 用的 LLMResponse。"""

    def __init__(self, text):
        self.completion_text = text


def _llm_response_event(umo="Sui:GroupMessage:1", *, persona="", uid="42",
                        user_msg="你好", simulated=False, source="proactive"):
    """造一条 on_llm_request 之后形态的事件（extras 齐备）。"""
    from core import compat
    ev = FakeEvent("你好", umo=umo, uid=uid)
    ev.set_extra(plugin_main.X_UMO, umo)
    ev.set_extra(plugin_main.X_PERSONA, persona)
    ev.set_extra(plugin_main.X_UID, uid)
    ev.set_extra(plugin_main.X_USER_MSG, user_msg)
    ev.set_extra(plugin_main.X_INJECTED, True)   # 没这个标记函数会直接 return
    if simulated:
        compat.mark_simulated(ev, source=source)
    return ev


def test_on_llm_response_survives_simulated_event():
    """合成事件（主动消息 / 戳一戳回应）走 on_llm_response 不能抛。

    回归：印象的 uid 与轮数**只在 `if not simulated:` 里赋值**，而末尾的
    印象钩子 `if _doc is not None and imp_count:` 是**无条件读**的 ——
    合成事件跳过赋值那一支，于是读到未绑定的局部名，抛
        UnboundLocalError: cannot access local variable 'imp_count'
    线上表现：每发一条主动消息就往「总览 → 最近错误」写一条，看起来像
    "状态更新失败"（`_stats['errors']` 同步 +1）。
    """
    p = _neo()
    doc = _doc(p)
    ev = _llm_response_event(persona=doc.id, simulated=True)
    asyncio.run(Neo.on_llm_response(p, ev, _Resp("我主动说句话")))
    assert p._last_error == "", f"合成事件仍然踩空: {p._last_error}"
    assert p._stats["errors"] == 0, p._stats


def test_on_llm_response_real_event_still_counts_impression():
    """修好之后真实用户轮**照旧**推进印象轮数（别把功能一起修没了）。"""
    p = _neo()
    doc = _doc(p)
    ev = _llm_response_event(persona=doc.id, simulated=False)
    asyncio.run(Neo.on_llm_response(p, ev, _Resp("嗯嗯")))
    assert p._last_error == "", p._last_error
    imp = p.state.load_impression("42")
    assert int(imp.get("msg_count", 0) or 0) == 1, imp


def test_on_llm_response_simulated_event_does_not_count_impression():
    """合成事件不推进印象轮数 —— 它没有真实用户发言。"""
    p = _neo()
    doc = _doc(p)
    ev = _llm_response_event(persona=doc.id, simulated=True)
    asyncio.run(Neo.on_llm_response(p, ev, _Resp("我主动说句话")))
    imp = p.state.load_impression("42")
    assert int(imp.get("msg_count", 0) or 0) == 0, imp


# ═══════════════ on_llm_request：主动消息不该带 @ 目标 ═══════════════

class _Req:
    """够 on_llm_request 用的 ProviderRequest。

    动态段（含 @ 协议说明）走 `extra_user_content_parts`，
    **不写** `system_prompt` —— 见 `main._append_dynamic`。
    所以断言要看 `dynamic()`，不能看 system_prompt。
    """

    def __init__(self):
        self.system_prompt = "（原有系统提示）"
        self.contexts = []
        self.extra_user_content_parts = None

    def dynamic(self) -> str:
        return "\n".join(getattr(p, "text", "") or ""
                         for p in (self.extra_user_content_parts or []))


def test_proactive_round_has_no_at_target():
    """主动消息不得告诉模型「@ 当前说话的人」。

    回归（2026-09-16 线上「莫名其妙一直艾特一个人」）：合成事件的 sender
    是从**事件模板**里抄来的 —— 而模板只在会话首次写入、此后永不更新，
    所以它永远是"该会话历史上第一个说话的人"。注入层把这当成"本轮说话人"
    登记成唯一可 @ 的目标，于是每一条主动消息都点名同一个人。
    """
    from core import compat
    p = _neo()
    ev = FakeEvent("（主动开口）", umo="Sui:GroupMessage:1", uid="300000004")
    compat.mark_simulated(ev, source="proactive")
    req = _Req()
    asyncio.run(Neo.on_llm_request(p, ev, req))
    assert p._last_error == "", f"注入抛异常: {p._last_error}"
    dyn = req.dynamic()
    assert "本轮没有可用的引用 / @ 目标" in dyn, \
        f"主动消息仍然给模型登记了 @ 目标: {dyn[-400:]!r}"
    assert "[[at:300000004]]" not in dyn, \
        "主动消息把模板里那个固定的人登记成了 @ 目标"


def test_real_group_reply_still_has_at_target():
    """真实群消息照旧要给出 @ 目标（别把功能一起修没了）。"""
    p = _neo()
    ev = FakeEvent("在吗", umo="Sui:GroupMessage:1", uid="300000004",
                   at_bot=True)          # 硬触发 → 不走概率决策
    req = _Req()
    asyncio.run(Neo.on_llm_request(p, ev, req))
    assert p._last_error == "", f"注入抛异常: {p._last_error}"
    dyn = req.dynamic()
    assert "[[at:300000004]]" in dyn, f"真实消息的 @ 目标丢了: {dyn[-400:]!r}"


# ═══════════════ 消息里提到的角色：资料注入 ═══════════════

def test_mentioned_character_block_is_temp_dynamic():
    """消息里命中角色名时，角色资料必须以 temp 形式进动态段。

    端到端回归：角色扮演里常见"AI 说不认识某个剧情人物"。这里保证
    "消息里明确提到了谁" 会被纯逻辑认出来、把资料递到模型手上，
    而且只影响本轮（`_no_save=True`，不落聊天记录、不进 system_prompt）。
    """
    p = _neo()
    ev = FakeEvent("伊薇特是谁啊", umo="Sui:GroupMessage:1", uid="42",
                   at_bot=True)
    req = _Req()
    asyncio.run(Neo.on_llm_request(p, ev, req))
    assert p._last_error == "", f"注入抛异常: {p._last_error}"
    dyn = req.dynamic()
    assert "# 消息里提到的角色（资料参考）" in dyn, dyn[-400:]
    assert "伊薇特" in dyn
    assert "# 消息里提到的角色" not in req.system_prompt, \
        "角色资料不能进 system_prompt 稳定段"
    # 承载该块的 part 必须已 mark_as_temp（否则会落库）
    holder = [x for x in (req.extra_user_content_parts or [])
              if "# 消息里提到的角色" in (getattr(x, "text", "") or "")]
    assert holder, "角色资料不在 extra_user_content_parts 里"
    assert all(getattr(x, "_no_save", False) is True for x in holder), \
        "角色资料没有 mark_as_temp()"


def test_no_character_mention_no_block():
    """没提到任何角色时不应多注入一个空块。"""
    p = _neo()
    ev = FakeEvent("今天天气不错", umo="Sui:GroupMessage:1", uid="42",
                   at_bot=True)
    req = _Req()
    asyncio.run(Neo.on_llm_request(p, ev, req))
    assert p._last_error == "", f"注入抛异常: {p._last_error}"
    assert "# 消息里提到的角色" not in req.dynamic()


# ═══════════════ 输出层：旧 §n 标记不能漏给用户 ═══════════════

def test_legacy_segment_marker_never_reaches_user():
    """回归：旧 `§n` 分段标记必须被清掉，不能出现在发给用户的消息里。

    新环境的分段是"子代理返回 JSON 段数组"，没有标记协议 —— 但模型可能因
    旧提示词/旧历史惯性写 `§n`，而链路上没有任何环节会删它。
    2026-09-19 日志里它确实原样发给了用户。
    """
    from astrbot.core.message.components import Plain

    p = _neo()
    ev = FakeEvent("随便聊聊")

    class Res:
        chain = [Plain("今天排练好累啊§n不过出新歌了还挺开心的")]

    # 必须拿**实例**：输出层是在 `res` 实例上重写 chain 的，
    # 读类属性会永远看到初始值（等于什么都没验证）。
    res = Res()
    ev.get_result = lambda: res
    asyncio.run(Neo.on_decorating_result(p, ev))

    plains = [c for c in res.chain if isinstance(c, Plain)]
    joined = "".join(c.text for c in plains)
    assert "§" not in joined, f"§n 漏给了用户: {joined!r}"
    # 内容不能丢（标记只该被换成断点，不该吃掉文字）
    assert "好累啊" in joined and "不过" in joined
    assert "n不过" not in joined, "标记的 n 没被吃掉，会残留一个孤立的 n"


def test_clean_text_is_untouched_by_marker_cleanup():
    """没有 § 的普通回复不能被这一步碰到（内容与段数都不变）。"""
    from astrbot.core.message.components import Plain

    p = _neo()
    p.config["enable_segmentation"] = False       # 只验证清理步骤本身
    ev = FakeEvent("随便聊聊")

    class Res:
        chain = [Plain("第一句话。第二句话。")]

    res = Res()
    ev.get_result = lambda: res
    asyncio.run(Neo.on_decorating_result(p, ev))
    plains = [c for c in res.chain if isinstance(c, Plain)]
    assert [c.text for c in plains] == ["第一句话。第二句话。"]
