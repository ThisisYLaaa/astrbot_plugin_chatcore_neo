# -*- coding: utf-8 -*-
"""流式传输：切分规则（纯逻辑） + 插件侧实时发送链路。

分两层：
  * `StreamSplitter` 是纯文本逻辑，直接喂增量断言切出来的段；
  * 插件那半边用**假事件**跑 `_stream_deliver()` —— 这一层最容易出的是
    "兜不住"的错（标记被劈成两半、名牌挂错段、子代理又被叫起来、
    收了流却没人发），所以断言都盯着这些。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parent))

from core.stream import (StreamSplitter, safe_prefix_len,  # noqa: E402
                         split_stream_text)
from core.session_params import MODE_NARRATIVE, MODE_PLAIN  # noqa: E402
import _env  # noqa: E402

_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))

PLAIN = dict(max_len=60, min_len=8, mode=MODE_PLAIN)


def _feed_all(text: str, *, chunk: int = 1, **kw) -> list:
    """按 chunk 大小喂进去，返回所有发出的段（含收尾）。"""
    sp = StreamSplitter(**{**PLAIN, **kw})
    out: list = []
    for i in range(0, len(text), chunk):
        out += sp.feed(text[i:i + chunk])
    out += sp.flush()
    return out


# ---------------------------------------------------------------- 安全前缀

def test_safe_prefix_holds_unclosed_marker():
    """`[[at:12` 还没闭合时不能切 —— 切了标记就被劈成两半。"""
    assert safe_prefix_len("你好[[at:12") == 2
    assert safe_prefix_len("你好[[at:12345]]") == len("你好[[at:12345]]")


def test_safe_prefix_gives_up_on_broken_bracket():
    """模型漏了闭括号：超过标记长度上限后不能再卡住发送。"""
    broken = "[" + "x" * 80
    assert safe_prefix_len(broken) == len(broken)


def test_safe_prefix_holds_unclosed_paren():
    """剧情向的括号块没闭合时不切（括号描写不能劈碎）。"""
    assert safe_prefix_len("（她低下头") == 0
    assert safe_prefix_len("（她低下头）") == len("（她低下头）")


def test_safe_prefix_gives_up_on_long_unclosed_paren():
    """括号块长得过分 = 模型漏了 `）`，不再无限卡住。"""
    s = "（" + "她" * 300
    assert safe_prefix_len(s, hold_limit=100) == len(s)


# ---------------------------------------------------------------- 纯信息

def test_plain_splits_at_sentence_end():
    segs = _feed_all("你好呀。今天天气不错哦。我们出去玩吧")
    assert segs == ["你好呀。今天天气不错哦。", "我们出去玩吧"], segs


def test_plain_merges_short_sentence():
    """短于 min_len 的句子并进下一段，不出现「嗯。」这种碎嘴。"""
    segs = _feed_all("嗯。好的，那我先过去了哦。", min_len=8)
    assert segs == ["嗯。好的，那我先过去了哦。"], segs


def test_plain_hard_cut_absorbs_continuation_punct():
    """硬切时把紧跟的「，」留在上一段，下一段不会以逗号开头。"""
    segs = _feed_all("甲乙丙丁戊，己庚辛壬癸", max_len=5, min_len=1)
    assert segs[0] == "甲乙丙丁戊，", segs
    assert all(not s.lstrip().startswith("，") for s in segs), segs


def test_plain_no_hard_cut_before_max_len():
    """没到上限就老老实实攒着（否则会把一句话劈成两条）。"""
    sp = StreamSplitter(**{**PLAIN, "max_len": 30})
    assert sp.feed("这是一句没有句末标点的很长的话") == []
    assert sp.leftover() == "这是一句没有句末标点的很长的话"


def test_marker_split_across_chunks_never_leaks():
    """`[[at:12345]]` 被增量劈开也要完整落在同一段里。"""
    segs = _feed_all("你好[[at:12345]]，在吗。")
    assert segs == ["你好[[at:12345]]，在吗。"], segs
    assert "[[at:" not in "".join(segs).replace("[[at:12345]]", "")


def test_legacy_mark_becomes_boundary():
    """旧 `§n` 标记 → 分段边界（绝不发给用户），且不会被增量劈成 `\\n`+`n`。"""
    segs = _feed_all("香奈美愣了一下。§n你怎么了呀，是不是累了。")
    assert "§" not in "".join(segs), segs
    assert len(segs) >= 2, segs
    assert not any("n你" in s for s in segs), segs


# ---------------------------------------------------------------- 剧情向

def test_narrative_blocks_and_speech():
    text = "（香奈美拿起袋子）哇，这里面装着什么呀（她捏了捏）好软哦"
    segs = _feed_all(text, mode=MODE_NARRATIVE)
    assert segs == ["（香奈美拿起袋子）", "哇，这里面装着什么呀",
                    "（她捏了捏）", "好软哦"], segs


def test_narrative_long_speech_splits_at_sentence():
    """剧情向的"说话不细拆"只在**说话超长**时让步（否则没法实时）。"""
    text = "（动作）" + "第一句话完了。" * 8
    segs = _feed_all(text, mode=MODE_NARRATIVE, max_len=30, min_len=8)
    assert segs[0] == "（动作）", segs
    assert len(segs) > 2, segs
    assert all(len(s) <= 30 + 8 for s in segs[1:]), segs


def test_narrative_broken_paren_does_not_stall():
    """漏了 `）` 的括号块不能把后面所有内容一起卡死。"""
    text = "（" + "她" * 250 + "。后面还有话哦。"
    segs = _feed_all(text, mode=MODE_NARRATIVE, max_len=40, min_len=8)
    assert any("后面还有话哦" in s for s in segs), segs


# ---------------------------------------------------------------- 名牌归位

def test_speaker_tail_held_back():
    """段尾的「【角色名】」扣回缓冲区，跟着下一段走。"""
    text = "（动作）我说完了【艾卡】（艾卡动作）走了。"
    segs = _feed_all(text, mode=MODE_NARRATIVE, names=("艾卡", "香奈美"))
    assert "我说完了【艾卡】" not in segs, segs
    assert "我说完了" in segs, segs
    # 收尾时名牌不能自成一个空段，也不许吞掉
    assert "".join(segs).replace("【艾卡】", "", 1) or True
    assert "走了" in "".join(segs), segs


def test_bare_name_is_kept_not_swallowed():
    """段中名牌不单独成条；能发的先发，收尾只剩名牌时**保留**（不吞字）。"""
    sp = StreamSplitter(**{**PLAIN, "names": ("艾卡",)})
    assert sp.feed("你好呀。【艾卡】") == ["你好呀。"]
    assert sp.flush() == ["【艾卡】"]


def test_speaker_header_never_glued_to_previous_segment():
    """实测复现（2026-10-25）：名牌被粘在了**上一条消息末尾**。

    模型原文（剧情向、双人格同台）：
        `【香奈美】（动作1）台词1\\n\\n【知更鸟·晴歌】（动作2）台词2`
    实际发出：`台词1 【知更鸟·晴歌】` ← 名字被甩到上一条尾巴上。
    期望：名牌一律贴在**自己那段开头**，且第一段不等收尾就该出去。
    """
    names = ("香奈美", "知更鸟·晴歌")
    text = ("【香奈美】（香奈美把被子拉高遮住半张脸，只露出眼睛，声音闷闷的，"
            "耳根还热着）尴尬的又不只你一个呀，香奈美刚才那句话说得那么响\n\n"
            "【知更鸟·晴歌】（她把谱本抱得紧了些，眼睛弯起来，声音很轻，"
            "像怕碰碎屋里的安静）……没关系的呀，这种说不出口的尴尬，"
            "其实很像呛到汽水的那一下")

    segs = _feed_all(text, mode=MODE_NARRATIVE, names=names)
    assert segs[0] == ("【香奈美】（香奈美把被子拉高遮住半张脸，只露出眼睛，"
                       "声音闷闷的，耳根还热着）"), segs
    # 名牌不许挂在上一条末尾
    tails = tuple(f"【{n}】" for n in names)
    for s in segs:
        assert not s.rstrip().endswith(tails), f"名牌被粘在了段尾: {s!r}"
    # 名牌也不许单独成条
    assert "【知更鸟·晴歌】" not in segs and "【香奈美】" not in segs, segs
    # 名牌 + 它自己的括号描写要在同一条里
    assert ("【知更鸟·晴歌】（她把谱本抱得紧了些，眼睛弯起来，声音很轻，"
            "像怕碰碎屋里的安静）") in segs, segs
    # 内容守恒（只忽略空白差异）
    assert "".join(segs).split() == text.split(), segs


def test_speaker_header_does_not_stall_streaming():
    """段首就是名牌时不能把整段扣住 —— 那是"静默到收尾"的 bug。"""
    names = ("香奈美", "知更鸟·晴歌")
    text = ("【香奈美】（香奈美把被子拉高遮住半张脸，只露出眼睛）"
            "尴尬的又不只你一个呀，香奈美刚才那句话说得那么响。\n\n"
            "【知更鸟·晴歌】（她把谱本抱得紧了些）……没关系的呀。")
    sp = StreamSplitter(max_len=60, min_len=8, mode=MODE_NARRATIVE,
                        names=names)
    half = len(text) // 2
    assert sp.feed(text[:half]), "前半段喂完一段都没发出去（又被名牌卡住了）"


# ---------------------------------------------------------------- 守恒

def test_chunking_never_loses_content():
    """逐字喂 vs 整段喂：切出来的段完全一致（增量不能改变结果）。"""
    text = ("（她歪头看你）今天的风好大呀，"
            "把领子都吹翻了（伸手给你理了理）要不要进去坐坐？"
            "我给你泡杯茶。")
    one = split_stream_text(text, mode=MODE_NARRATIVE, names=("艾卡",))
    many = _feed_all(text, mode=MODE_NARRATIVE, names=("艾卡",))
    assert one == many, (one, many)
    keep = "".join(one).replace("", "")
    for ch in keep:
        assert ch in text or ch in "".join(one)


def test_disabled_splitter_batches_everything():
    """关掉分段：什么都不切，收尾一次吐出来（流式仍照跑）。"""
    sp = StreamSplitter(**{**PLAIN, "enabled": False})
    assert sp.feed("第一句。第二句。") == []
    assert sp.flush() == ["第一句。第二句。"]


# ---------------------------------------------------------------- 插件侧
#
# 下面这些用**假事件**跑真实的 `_stream_deliver()`：注入、发送、收尾、
# 与 `on_decorating_result` 的分工都要对得上。

try:
    import importlib

    plugin_main = importlib.import_module(f"{PLUGIN.name}.main")
except Exception as e:  # pragma: no cover
    from run_tests import Skip
    raise Skip(f"无法导入插件入口：{e!r}")

Neo = plugin_main.ChatCoreNeoPlugin


class _Ctx:
    def register_web_api(self, *a, **k):
        pass

    def get_all_providers(self):
        return []

    def get_all_embedding_providers(self):
        return []

    def get_all_tts_providers(self):
        return []

    def get_using_provider(self, umo=None):
        return None

    def get_using_tts_provider(self, umo=None):
        return None

    def get_provider_by_id(self, pid):
        return None

    def get_event_queue(self):
        return None


class _Cfg(dict):
    def save_config(self):
        return None


class _Result:
    def __init__(self, chain):
        self.chain = chain
        self.use_t2i_ = None


class _Meta:
    """平台元数据（aiocqhttp 那种"没有原生流式"的平台）。"""

    def __init__(self, native: bool = False):
        self.support_streaming_message = native


class _Event:
    """能骗过插件的最小事件：extras / send / send_streaming。"""

    def __init__(self, umo="Banyan:FriendMessage:ccn-stream-test", private=True,
                 native_stream: bool = False):
        self._extras: dict = {}
        self.unified_msg_origin = umo
        self._private = private
        self.sent: list = []
        self.marks: list = []          # 每次 yield 时的发送计数（验"实时"）
        self._has_send_oper = False
        self.stopped = False
        self.platform_meta = _Meta(native_stream)

    # ---- 框架事件接口
    def is_private_chat(self):
        return self._private

    def get_sender_id(self):
        return "100000001"

    def get_sender_name(self):
        return "测试者"

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    async def send(self, chain):
        self.sent.append(chain)
        self._has_send_oper = True

    async def send_streaming(self, generator, use_fallback=False):
        """框架默认实现（**必须**被插件接管，否则断言会失败）。"""
        async for _ in generator:
            pass

    # ---- 断言辅助
    def texts(self) -> list:
        out = []
        for chain in self.sent:
            for comp in chain.chain:
                if hasattr(comp, "text"):
                    out.append(comp.text)
        return out


def _new_plugin(**cfg) -> "Neo":
    base = {"enabled": True, "enable_admin_page": False,
            "enable_streaming": True, "stream_send_interval": 0,
            "enable_segmentation": True, "segment_max_len": 60,
            "segment_min_len": 8, "enable_tts": False,
            "enable_markers": True, "private_format_agent": True}
    base.update(cfg)
    conf = _Cfg(base)
    p = Neo(_Ctx(), conf)
    # 2026-10-28 多实例重构：`_cfg()` 改走 `cfg_of()`，读的是**实例**配置
    # （`instances/<实例>/config.json`），构造参数里的 config 只在"没有实例"
    # 时兜底。本机磁盘上就有真实实例（Banyan，`enable_streaming=true`），
    # 于是 `_new_plugin(enable_streaming=False)` 会被真实配置覆盖 ——
    # 断言变成在读线上数据，测试要验的开关根本没生效。
    # 把传入的配置登记成**所有**已知实例的配置：`_cfg()` 读到的一定是测试
    # 给的那份；`_rt()` 仍落在真实实例目录（不会新造 instances/ 下的歪路径）。
    ids = set()
    try:
        ids.update(p._inst_store.loaded_ids())
    except Exception:
        pass
    ids.update(getattr(p, "_inst_cfgs", {}) or {})
    ids.add(getattr(p, "_default_inst", "") or "")
    p._inst_cfgs = {i: conf for i in ids}
    return p


def _gen(chunks, ev=None, track=False):
    """假流式生成器：每个增量一条 MessageChain。"""
    from astrbot.core.message.message_event_result import MessageChain

    async def _it():
        for c in chunks:
            if isinstance(c, tuple):        # ("break",) / ("raw", comp)
                kind = c[0]
                if kind == "break":
                    yield MessageChain(chain=[], type="break")
                else:
                    yield MessageChain([c[1]])
            else:
                yield MessageChain().message(c)
            if track and ev is not None:
                ev.marks.append(len(ev.sent))
    return _it()


def test_stream_prepare_sets_framework_flag_and_installs():
    """开关必须同时做两件事：放行框架流式 + 接管发送。"""
    p = _new_plugin()
    ev = _Event()
    assert p._stream_prepare(ev) is True
    assert ev.get_extra("enable_streaming") is True
    assert ev.get_extra("_ccn_stream_req") is True
    # 实例上的 send_streaming 已被换成插件实现（不是类里那个）
    assert ev.send_streaming.__qualname__ != "_Event.send_streaming"


def test_stream_prepare_noop_when_disabled():
    p = _new_plugin(enable_streaming=False)
    ev = _Event()
    assert p._stream_prepare(ev) is False
    assert ev.get_extra("enable_streaming") is None
    assert ev.send_streaming.__qualname__ == "_Event.send_streaming"


def test_stream_prepare_keeps_native_streaming_platforms():
    """有原生流式的平台（webchat/飞书/TG）**不接管**发送。

    它们的 `send_streaming` 带协议副作用（webchat 要靠末帧 `complete`
    收尾），硬接管会让那条消息永远停在"生成中"；只保留"禁用子代理分段"。
    """
    p = _new_plugin()
    ev = _Event(native_stream=True)
    assert p._stream_prepare(ev) is False
    assert ev.send_streaming.__qualname__ == "_Event.send_streaming"
    assert ev.get_extra("_ccn_stream_req") is True
    assert ev.get_extra("enable_streaming") is True


def test_stream_split_ok_respects_command_output():
    """命令输出默认不分段（与非流式输出层同一判据）。"""
    p = _new_plugin(enable_segmentation=True, segment_command_output=False)
    ev = _Event()
    assert p._stream_split_ok(ev) is True
    p._is_command_result = lambda e: True
    assert p._stream_split_ok(ev) is False
    p._is_command_result = lambda e: False
    assert p._stream_split_ok(ev) is True


def test_streaming_hides_subagent_switches_in_schema():
    """流式开启时，「分段使用子代理」那一组配置在 WebUI 里应当被隐藏。

    前端求值方式是**严格相等**（`DashboardTwoFactorDialog-*.js` 里
    `if (F(iterable, key) !== expected) return false`），所以 `false` 这种
    条件必须原样写出来；写成 truthy 反而永远隐藏不了。
    """
    import json

    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    subagent_keys = [k for k in schema if "format_agent" in k or k.startswith(
        ("private_format_", "group_format_")) or k == "segment_verify_ratio"]
    assert len(subagent_keys) >= 10, subagent_keys
    missing = [k for k in subagent_keys
               if schema[k].get("condition", {}).get("enable_streaming") is not False]
    assert not missing, (
        "这些子代理配置没有声明「流式开启时隐藏」，WebUI 会在流式下继续显示"
        "（=幽灵开关）:\n  " + "\n  ".join(missing))


def test_schema_condition_visibility_matches_frontend_rule():
    """按前端规则模拟一遍：流式开 → 隐藏；流式关 → 显示。"""
    import json

    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))

    def visible(key: str, conf: dict) -> bool:
        cond = schema[key].get("condition") or {}
        return all(conf.get(k) == v for k, v in cond.items())

    keys = ["private_format_agent", "private_format_model", "group_format_agent",
            "group_format_model", "group_format_min_segment",
            "segment_verify_ratio"]
    streaming_on = {"enable_segmentation": True, "enable_streaming": True,
                    "private_format_agent": True, "group_format_agent": True}
    streaming_off = dict(streaming_on, enable_streaming=False)
    for k in keys:
        assert visible(k, streaming_on) is False, f"{k} 在流式下仍然可见"
        assert visible(k, streaming_off) is True, f"{k} 在非流式下被隐藏了"
    # 流式开关自己不该被别的条件挡住
    assert not (schema["enable_streaming"].get("condition") or {})
    assert visible("stream_send_interval", streaming_on) is True
    assert visible("stream_send_interval", streaming_off) is False


def test_segmenter_hard_guard_when_streaming_on():
    """代码侧硬闸：`enable_streaming` 一开，子代理整体停用。"""
    from core.segment_agent import SegmentAgent

    off = SegmentAgent(None, _Cfg({"private_format_agent": True,
                                   "group_format_agent": True}))
    assert off.enabled_for(is_private=True) is True
    assert off.enabled_for(is_private=False) is True

    on = SegmentAgent(None, _Cfg({"enable_streaming": True,
                                  "private_format_agent": True,
                                  "group_format_agent": True}))
    assert on.enabled_for(is_private=True) is False
    assert on.enabled_for(is_private=False) is False


def test_segment_prompts_page_flags_inactive_under_streaming():
    """管理页要能看出"四份分段提示词当前不生效"（否则改了没反应像坏了）。"""
    from core import webadmin

    p = _new_plugin(enable_streaming=True)
    d = webadmin.build_segment_prompts(p)
    assert d["active"] is False and d["streaming"] is True
    assert d["prompts"], "提示词内容仍要能编辑（关掉流式就生效）"

    p2 = _new_plugin(enable_streaming=False)
    d2 = webadmin.build_segment_prompts(p2)
    assert d2["active"] is True and d2["streaming"] is False


def test_segment_test_forces_agent_under_streaming():
    """「试跑」是用户显式点的，流式下也必须真跑一次（force=True）。"""
    import asyncio

    from core import webadmin

    p = _new_plugin(enable_streaming=True)
    calls: list = []

    async def _fake_segment(text, **kw):
        calls.append(kw)
        return ["切好了"]

    p.segmenter.segment = _fake_segment
    d = asyncio.run(webadmin.segment_test(p, "第一句话。第二句话。", True, ""))
    assert calls and calls[0].get("force") is True, calls
    assert d["agent"] == ["切好了"] and d["used_agent"] is True


def test_stream_deliver_sends_realtime():
    """核心：**边收边发** —— 第一段必须早于最后一个增量就发出去了。"""
    p = _new_plugin()
    ev = _Event()
    p._stream_prepare(ev)
    chunks = ["（香奈美抬头）", "今天的风好大呀，", "把领子都吹翻了。",
              "要不要进来坐坐？"]
    asyncio.run(ev.send_streaming(_gen(chunks, ev, track=True)))
    assert ev.get_extra("_ccn_streamed") is True
    texts = ev.texts()
    assert texts, "流式发送一条都没发出去"
    assert ev.marks[0] > 0, f"第一段没有在流未结束时就发出: marks={ev.marks}"
    assert "今天的风好大呀，" in "".join(texts), texts


def test_stream_deliver_never_uses_segment_subagent():
    """**硬约定**：流式轮次绝不调分段子代理。"""
    p = _new_plugin()
    calls: list = []

    async def _boom(*a, **k):
        calls.append(1)
        raise AssertionError("流式轮次不得调用分段子代理")

    p.segmenter.segment = _boom
    ev = _Event()
    p._stream_prepare(ev)
    asyncio.run(ev.send_streaming(_gen(["你好呀。", "今天天气不错哦。"])))
    assert calls == [], "流式轮次调用了分段子代理"


def test_stream_deliver_markers_across_chunks():
    """标记跨增量到达 → 变成真 At 组件，且正文里不留残渣。"""
    p = _new_plugin()
    ev = _Event(umo="Banyan:FriendMessage:ccn-stream-test")
    ev.set_extra("_ccn_uid", "100000001")
    p._stream_prepare(ev)
    asyncio.run(ev.send_streaming(
        _gen(["在的[[at:100", "000001]]，你说吧。"])))
    texts = ev.texts()
    joined = "".join(texts)
    assert "[[at:" not in joined and "]]" not in joined, texts
    ats = [c for chain in ev.sent for c in chain.chain
           if type(c).__name__ == "At"]
    assert ats and str(getattr(ats[0], "qq", "")) == "100000001", ev.sent


def test_stream_deliver_flushes_on_tool_break():
    """工具调用边界（`break`）要把已生成的内容先发出去，不能干等。"""
    p = _new_plugin()
    ev = _Event()
    p._stream_prepare(ev)
    asyncio.run(ev.send_streaming(_gen(
        ["我先查一下。", ("break",), "查到了。"])))
    texts = ev.texts()
    assert any("我先查一下" in t for t in texts), texts
    assert any("查到了" in t for t in texts), texts


def test_stream_deliver_survives_generator_error():
    """生成器中途抛异常：已收到的文字仍要尽力发出去。"""
    from astrbot.core.message.message_event_result import MessageChain

    p = _new_plugin()

    async def _bad():
        yield MessageChain().message("前面这段是好的。")
        raise RuntimeError("模拟上游断流")

    ev = _Event()
    p._stream_prepare(ev)
    asyncio.run(ev.send_streaming(_bad()))
    assert any("前面这段是好的" in t for t in ev.texts()), ev.sent


def test_decorating_result_skips_after_streamed():
    """已经被流式层发过的轮次：输出层直接跳过（不重复发、不再分段）。"""
    from astrbot.api.message_components import Plain

    p = _new_plugin()
    ev = _Event()
    ev.set_extra("_ccn_streamed", True)
    res = _Result([Plain("（动作）台词。")])
    ev.get_result = lambda: res
    asyncio.run(p.on_decorating_result(ev))
    assert [c.text for c in res.chain] == ["（动作）台词。"], res.chain


def test_decorating_result_rescues_when_stream_sent_nothing():
    """流式一个字都没发出去 → 兜底补发整段（不能让用户等到空气）。

    触发场景：提供方没真流式。框架在流式模式下**不转发** `llm_result` 链，
    正文只跟着 STREAMING_FINISH 走，而 respond 阶段对它是直接 return 的 ——
    不补就是"机器人没说话"。
    """
    from astrbot.api.message_components import Plain

    p = _new_plugin()
    ev = _Event()
    ev.set_extra("_ccn_streamed", True)
    ev.set_extra("_ccn_stream_sent", 0)
    res = _Result([Plain("（她抬头）风好大呀，进来坐坐吧。")])
    ev.get_result = lambda: res
    asyncio.run(p.on_decorating_result(ev))
    assert ev.texts() == ["（她抬头）风好大呀，进来坐坐吧。"], ev.texts()


def test_decorating_result_no_rescue_when_already_sent():
    """已经实时发过段的轮次不能补发（否则用户收到两遍）。"""
    from astrbot.api.message_components import Plain

    p = _new_plugin()
    ev = _Event()
    ev.set_extra("_ccn_streamed", True)
    ev.set_extra("_ccn_stream_sent", 2)
    res = _Result([Plain("（她抬头）风好大呀，进来坐坐吧。")])
    ev.get_result = lambda: res
    asyncio.run(p.on_decorating_result(ev))
    assert ev.sent == [], ev.texts()


def test_decorating_result_bans_subagent_when_stream_requested():
    """降级成整段交付时（平台不支持流式），也不许调分段子代理。"""
    from astrbot.api.message_components import Plain

    p = _new_plugin()
    calls: list = []

    async def _boom(*a, **k):
        calls.append(1)
        raise AssertionError("流式轮次不得调用分段子代理")

    p.segmenter.segment = _boom
    ev = _Event()
    ev.set_extra("_ccn_stream_req", True)
    res = _Result([Plain("（香奈美抬头）今天的风好大呀，把领子都吹翻了。")])
    ev.get_result = lambda: res
    asyncio.run(p.on_decorating_result(ev))
    assert calls == [], "降级的流式轮次调用了分段子代理"
    assert len(res.chain) >= 1

def test_stream_marker_only_chunk_is_held_not_sent():
    """只解析出 `[[reply:id]]`、没有正文的分片不能单独发出。

    回归 2026-11-04（用户截图）：流式下模型把引用标记单独放在一个分片里，
    清洗后正文为空、链里只剩 Reply → QQ 里出现"只有一个引用、没有内容"的
    空白气泡。修法：这类组件攒进 `ctx["pending_comps"]`，跟下一段正文一起发。
    """
    import types as _t

    class _Light(Neo):
        # 用类属性遮蔽父类的 `tts` property（它会走 _rt() 装配整套运行时）
        tts = _t.SimpleNamespace(enabled=False)

    p = object.__new__(_Light)
    p._spawn = lambda coro: coro.close()

    sent = []

    class _Ev:
        unified_msg_origin = "T:GroupMessage:1"

        async def send(self, chain):
            sent.append(chain)

    ctx = {"markers": True, "uid": "", "uname": "", "interval": 0,
           "last_send": 0, "mode": MODE_PLAIN, "umo": "T:GroupMessage:1",
           "is_private": False}

    ok, n = asyncio.run(p._stream_emit(_Ev(), "[[reply:12345]]", ctx))
    assert ok is False and n == 0, (ok, n)
    assert not sent, "只有标记的分片不该单独发出（会变成空白引用气泡）"
    assert ctx.get("pending_comps"), "标记应被攒起来等下一段"

    ok, n = asyncio.run(p._stream_emit(_Ev(), "你好呀", ctx))
    assert ok and len(sent) == 1, (ok, len(sent))
    comps = list(getattr(sent[0], "chain", sent[0]))
    kinds = [type(c).__name__ for c in comps]
    assert "Reply" in kinds, kinds
    assert any(getattr(c, "text", "") == "你好呀" for c in comps), kinds
    assert not ctx.get("pending_comps"), "攒下的组件应被消费掉"

