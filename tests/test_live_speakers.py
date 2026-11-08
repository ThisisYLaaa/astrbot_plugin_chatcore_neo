# -*- coding: utf-8 -*-
"""群聊发言流水：记录说话人 → 注入给模型，修复群聊"认错人"。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parent))
import _env  # noqa: E402
_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))
plugin_main = importlib.import_module(f"{PLUGIN.name}.main")

from test_admin_api import _plugin  # noqa: E402

Neo = plugin_main.ChatCoreNeoPlugin


def _live_plugin():
    """FakePlugin + 绑定真实 _record_live/_live_text 实现。"""
    p = _plugin()
    p._live = {}
    p._record_live = Neo._record_live.__get__(p)
    p._live_text = Neo._live_text.__get__(p)
    return p


class _Ev:
    """最小事件桩：够 _record_live / _live_text 用。"""

    def __init__(self, umo, uid, name, text, private=False):
        self.umo = umo
        self.uid = uid
        self.name = name
        self.text = text
        self.private = private

    def is_private_chat(self):
        return self.private

    @property
    def unified_msg_origin(self):
        return self.umo

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.name

    @property
    def message_str(self):
        return self.text


def _ev_group(uid="100000001", name="翼龙", text="在吗"):
    return _Ev("Banyan:GroupMessage:200000002", uid, name, text)


def test_group_messages_recorded_with_speaker():
    p = _live_plugin()
    p._record_live(_ev_group(text="我喜欢往中间拉"))
    p._record_live(_ev_group(uid="999", name="扶摇风息", text="我草你呢"))
    buf = p._live["Banyan:GroupMessage:200000002"]
    assert len(buf) == 2
    assert buf[0] == {"u": "100000001", "n": "翼龙", "t": "我喜欢往中间拉",
                      "ts": buf[0]["ts"]}
    assert buf[1]["u"] == "999" and buf[1]["n"] == "扶摇风息"


def test_private_messages_not_recorded():
    p = _live_plugin()
    p._record_live(_Ev("Banyan:FriendMessage:100000001", "100000001",
                       "翼龙", "私聊内容", private=True))
    assert p._live == {}


def test_live_text_includes_speaker_labels():
    p = _live_plugin()
    p._record_live(_ev_group(text="我喜欢往中间拉"))
    p._record_live(_ev_group(uid="999", name="扶摇风息", text="扶摇风息那是跟你在讲道理呢"))
    txt = p._live_text("Banyan:GroupMessage:200000002")
    assert "翼龙（100000001）：我喜欢往中间拉" in txt
    assert "扶摇风息（999）：扶摇风息那是跟你在讲道理呢" in txt


def test_live_text_has_date_weekday_time_prefix():
    """行前缀带「日期 星期 时分」——补回历史里被清理掉的时间线索。"""
    p = _live_plugin()
    p._record_live(_ev_group(text="在吗"))
    ts = p._live["Banyan:GroupMessage:200000002"][0]["ts"]
    lt = time.localtime(ts)
    expect = (f"[{lt.tm_mon:02d}-{lt.tm_mday:02d} "
              f"周{'一二三四五六日'[lt.tm_wday]} "
              f"{lt.tm_hour:02d}:{lt.tm_min:02d}]")
    txt = p._live_text("Banyan:GroupMessage:200000002")
    assert txt.startswith(expect + " 翼龙（100000001）：在吗")


def test_live_text_excludes_current_message():
    """本轮正在处理的发言不应混进"历史流水"，避免模型重复读自己。"""
    p = _live_plugin()
    cur = _ev_group(text="123")
    others = _ev_group(uid="999", name="扶摇风息", text="你在讲道理呢")
    p._record_live(others)
    p._record_live(cur)
    txt = p._live_text("Banyan:GroupMessage:200000002",
                       exclude_uid=cur.uid, exclude_text=cur.text)
    assert "：123" not in txt
    assert "扶摇风息（999）" in txt


def test_live_window_and_limit():
    p = _live_plugin()
    umo = "Banyan:GroupMessage:1"
    for i in range(40):
        p._record_live(_Ev(umo, f"u{i}", f"n{i}", f" 文本{i}"))
    buf = p._live[umo]
    assert len(buf) <= Neo._LIVE_LIMIT, "必须按上限截断"
    # 窗口外的过期记录被丢弃
    old = _Ev(umo, "old", "old", "很老的消息")
    old.ts_patch = time.time() - Neo._LIVE_WINDOW - 60
    buf[0]["ts"] = old.ts_patch
    p._record_live(_Ev(umo, "new", "new", "新消息"))
    assert all(x["ts"] >= time.time() - Neo._LIVE_WINDOW for x in buf)


def test_assembler_injects_live_block():
    """live_speakers 必须进入动态注入块（带说话人标题）。"""
    from core.assembler import H_LIVE, AssemblyInput, PromptAssembler
    from core.persona import PersonaService
    docs = PersonaService().all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    inp = AssemblyInput(doc=docs[0],
                        live_speakers="翼龙（100000001）：在吗\n扶摇风息（999）：你干嘛")
    result = PromptAssembler().build(inp)
    assert H_LIVE in result.dynamic_text
    assert "翼龙（100000001）：在吗" in result.dynamic_text
    # 空流水不产生任何块
    empty = PromptAssembler().build(AssemblyInput(doc=docs[0]))
    assert H_LIVE not in empty.dynamic_text


def test_empty_user_text_guard_injected():
    """纯图片/表情/仅@消息：必须告诉模型"对方没说话"，防把昵称当发言。"""
    from core.assembler import H_NO_TEXT, AssemblyInput, PromptAssembler
    from core.persona import PersonaService
    docs = PersonaService().all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    guarded = PromptAssembler().build(AssemblyInput(doc=docs[0], empty_user_text=True))
    assert H_NO_TEXT in guarded.dynamic_text
    assert "没有文字发言" in guarded.dynamic_text
    normal = PromptAssembler().build(AssemblyInput(doc=docs[0], empty_user_text=False))
    assert H_NO_TEXT not in normal.dynamic_text


def test_identity_block_marks_nickname_as_not_spoken():
    """句子式昵称必须被标注"昵称不是说过的话"，否则模型会把它当发言引用。"""
    from core.inject import build_identity_block
    block = build_identity_block(display_name="星绘", tier="friend",
                                 tier_label="引航者", uid="1762816726",
                                 user_name="睡觉浪费时间喵，别睡了喵")
    assert "是此人的昵称，不是 ta 说过的话" in block
    assert "睡觉浪费时间喵" in block
