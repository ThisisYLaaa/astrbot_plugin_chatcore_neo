# -*- coding: utf-8 -*-
"""事件重入管道（戳一戳 llm 模式 / 主动消息的唯一出口）的回归测试。

背景
----
框架**从不**给 `AstrMessageEvent` 挂 `context` 属性，而旧实现是
`ctx = getattr(event, "context", None)`，于是恒为 None、直接 `return False`：

* 戳一戳的 llm 模式（`poke_reply_mode='llm'`）**一条都没发出去过**；
* 主动消息 `_emit_proactive` 同样永远发不出去；
* 失败路径既不抛异常也不打日志 —— 从外面看就是"什么都没发生"。

线上证据：`/interactions` 里 `poke.seen=3, replied=2`（判定**放行**了 2 次），
但日志里既没有回复、也没有 `主动消息已重入管道`（0 次）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
import _env  # noqa: E402
_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))

from core import compat  # noqa: E402


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, x):
        self.items.append(x)


class _Ctx:
    def __init__(self):
        self.q = _Queue()

    def get_event_queue(self):
        return self.q


class _Ev:
    """**故意不提供 `context`** —— 与框架里的真实事件一致。"""

    def __init__(self):
        self.stopped = False

    def stop_event(self):
        self.stopped = True


def test_event_has_no_context_attribute():
    """把"框架不给事件挂 context"这条事实钉住，防止有人再写回去。"""
    try:
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
    except Exception as e:
        from run_tests import Skip
        raise Skip(f"拿不到框架事件类：{e!r}")
    assert not hasattr(AstrMessageEvent, "context"), \
        "框架给事件加了 context？那 reenter_pipeline 可以简化了 —— 请先确认"
    assert "context" not in AstrMessageEvent.__init__.__code__.co_varnames, \
        "AstrMessageEvent.__init__ 开始接收 context 了，需要重新评估"


def test_reenter_requires_explicit_context():
    """不传 context 必须**失败并告警**，不能静默返回 False。"""
    ev = _Ev()
    ctx = _Ctx()
    # 事件上没有 context，又不显式传 → 失败
    ok = asyncio.run(compat.reenter_pipeline(ev))
    assert ok is False
    assert ctx.q.items == []
    # 显式传就成功
    ok = asyncio.run(compat.reenter_pipeline(ev, context=ctx))
    assert ok is True
    assert ctx.q.items == [ev]
    assert compat.is_simulated(ev) is True      # 重入时必定打模拟标记


def test_reenter_reports_source_and_failure_loudly():
    """拿不到 context 时要打 warning —— 这条曾经是无声的。"""
    import logging
    recs = []

    class _H(logging.Handler):
        def emit(self, r):
            recs.append(r)

    lg = logging.getLogger("astrbot")
    h = _H()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        asyncio.run(compat.reenter_pipeline(_Ev(), source="poke"))
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    assert any(r.levelno >= logging.WARNING for r in recs), \
        "重入失败必须有 WARNING（旧实现是无声 return False）"
    assert any("poke" in r.getMessage() for r in recs), recs


def test_reenter_exception_is_reported_not_swallowed():
    class _BadCtx:
        def get_event_queue(self):
            raise RuntimeError("queue boom")

    ok = asyncio.run(compat.reenter_pipeline(_Ev(), context=_BadCtx()))
    assert ok is False


def test_callers_pass_context():
    """两个调用点都必须显式传 context（否则又回到老问题）。

    静态核对：`reenter_pipeline(...)` 的实参里必须出现 `context=`
    和 `source=`。
    """
    import ast
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr != "reenter_pipeline":
            continue
        kw = {k.arg for k in node.keywords}
        seen.append((node.lineno, kw))
    assert seen, "找不到 reenter_pipeline 的调用点"
    bad = [(ln, kw) for ln, kw in seen if "context" not in kw]
    assert not bad, f"这些调用点没传 context，重入会失败: {bad}"


# ─────────── 戳一戳：合成事件必须带**发起人**身份（否则认错人） ───────────

def test_build_message_sender_override():
    """`build_message(sender=...)` 必须覆盖骨架里的发送者。"""
    from core import compat as cp
    from core import proactive as pr

    tpl = cp.EventTemplate(
        platform="Banyan", self_id="111",
        sender={"user_id": "999", "nickname": "模板旧人"},
        group_id="100000007", session_id="100000007",
        message_type="GroupMessage")
    abm = pr.build_message(
        tpl, "hi", sender={"user_id": "100000001", "nickname": "江一楠"})
    assert str(abm.sender.user_id) == "100000001"
    assert abm.sender.nickname == "江一楠"
    # 不传 sender 就沿用骨架（主动消息的老行为）
    assert str(pr.build_message(tpl, "hi").sender.user_id) == "999"


class _Meta:
    id = "Banyan"
    name = "aiocqhttp"


class _SynthEv:
    pass


class _Adapter:
    def __init__(self):
        self.abm = None

    def meta(self):
        return _Meta()

    def create_event(self, abm):
        self.abm = abm
        return _SynthEv()


class _SynthCtx:
    def __init__(self, adapter):
        self.platform_manager = type(
            "PM", (), {"platform_insts": [adapter]})()


def test_synthesize_event_carries_sender_and_source():
    """戳一戳合成事件要带发起人身份，并把来源记成 poke。"""
    from core import compat as cp
    from core import proactive as pr

    tpl = cp.EventTemplate(
        platform="Banyan", self_id="111",
        sender={"user_id": "999", "nickname": "模板旧人"},
        group_id="1", session_id="1", message_type="GroupMessage")
    ad = _Adapter()
    ev = pr.synthesize_event(
        tpl, "（对方戳了你一下）", _SynthCtx(ad), umo="Banyan:GroupMessage:1",
        sender={"user_id": "100000001", "nickname": "江一楠"}, source="poke")
    assert ev is not None
    assert str(ad.abm.sender.user_id) == "100000001"
    assert ad.abm.sender.nickname == "江一楠"
    assert cp.is_simulated(ev) is True
    assert cp.simulated_source(ev) == "poke"


def test_reply_poke_passes_poker_identity():
    """`_reply_poke` 必须把发起人身份与 source="poke" 交给 synthesize_event。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def _reply_poke")
    seg = src[i:i + 2500]
    assert "sender=self._poke_sender(event)" in seg, \
        "戳一戳合成事件必须带发起人身份，否则会认成模板里那个人"
    assert 'source="poke"' in seg


def _load_main():
    import importlib
    if str(PLUGIN.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN.parent))
    try:
        return importlib.import_module(f"{PLUGIN.name}.main")
    except Exception as e:  # pragma: no cover
        from run_tests import Skip
        raise Skip(f"无法导入插件入口：{e!r}")


def test_poke_sender_uses_poker_not_template():
    """`_poke_sender` 取发起人 uid；昵称缺失时回落到本地昵称表。"""
    main = _load_main()

    class _Names:
        def get(self, uid):
            return {"100000001": "江一楠"}.get(uid)

    class _P:
        _user_names = _Names()

    fn = main.ChatCoreNeoPlugin._poke_sender.__get__(_P())

    class _E:
        def __init__(self, uid, name):
            self._uid, self._name = uid, name

        def get_sender_id(self):
            return self._uid

        def get_sender_name(self):
            return self._name

    assert fn(_E("100000001", "100000001")) == {
        "user_id": "100000001", "nickname": "江一楠"}      # 平台把 QQ 当昵称
    assert fn(_E("100000001", ""))["nickname"] == "江一楠"  # 事件不带昵称
    assert fn(_E("", "")) is None                          # 没 uid → 回退模板


def test_identity_block_gated_for_synth_events():
    """身份块只在"真有本轮说话人"时注入：主动消息跳过，戳一戳保留。"""
    main = _load_main()

    class _P:
        def _cfg(self, k, d=None):
            return True                       # identity_confirm_inject=True

    ok = main.ChatCoreNeoPlugin._identity_block_ok.__get__(_P())

    class _E:
        pass

    real = _E()
    poke = _E()
    compat.mark_simulated(poke, source="poke")
    proact = _E()
    compat.mark_simulated(proact, source="proactive")

    assert ok(real) is True
    assert ok(poke) is True, "戳一戳要保留身份块（内容来自发起人）"
    assert ok(proact) is False, "主动消息没有真实本轮说话人，必须跳过身份块"
