# -*- coding: utf-8 -*-
"""多人格同台：存取、渲染、装配分区、闸门。

覆盖三条纪律：
  1. 副人格规则进**动态段**（extra_user_content_parts + temp），不进稳定段；
  2. 只会话级存取，且与主人格重名时自动剔除；
  3. 渲染含「多人同台发言格式」的【角色名】前缀约束。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parent))
import _env  # noqa: E402
_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))

from core.assembler import (AssemblyInput, H_COSTARS,  # noqa: E402
                            PromptAssembler)
from core.costars import CoStarStore, build_block  # noqa: E402


class FakeDoc:
    """够 build_block / build_l1(include_framework=False) / build_l2 用。"""

    def __init__(self, pid, name, body="人设正文", style="说话习惯"):
        self.id = pid
        self.display_name = name
        self.skill_body = body
        self.outline = None
        self.content = {}
        self.dir = None
        self.lore = None
        self.world = ""
        self._style = style

    def read_content(self, key):
        return self._style if key == "style_summary" else ""

    def tier_path(self, tier):
        return None

    def effective_inject(self, section_id):
        return "never"


# ---------------------------------------------------------------- 存取

def test_store_roundtrip():
    d = tempfile.mkdtemp(prefix="ccn-costars-")
    st = CoStarStore(Path(d) / "_session_costars.json")
    umo = "Banyan:GroupMessage:1"
    assert st.cast_of(umo) == []
    st.add(umo, "sr_cyrene")
    st.add(umo, "sr_ciphey")
    st.add(umo, "sr_cyrene")            # 去重
    assert st.cast_of(umo) == ["sr_cyrene", "sr_ciphey"]
    st.remove(umo, "sr_cyrene")
    assert st.cast_of(umo) == ["sr_ciphey"]
    st.clear(umo)
    assert st.cast_of(umo) == []
    assert st.all() == {}


def test_store_persists_and_reloads():
    d = tempfile.mkdtemp(prefix="ccn-costars-")
    p = Path(d) / "_session_costars.json"
    st = CoStarStore(p)
    st.set("umoA", ["a", "b"])
    st2 = CoStarStore(p)                # 从磁盘重读
    assert st2.cast_of("umoA") == ["a", "b"]


def test_store_all_skips_empty():
    d = tempfile.mkdtemp(prefix="ccn-costars-")
    st = CoStarStore(Path(d) / "c.json")
    st.set("umoA", ["a"])
    st.set("umoB", [])                  # 空列表应被清理，不进 all()
    assert set(st.all()) == {"umoA"}


# ---------------------------------------------------------------- 渲染

def test_build_block_has_format_rule_and_bodies():
    main = FakeDoc("main", "香奈美")
    co1 = FakeDoc("sr_cyrene", "赛琳", body="我是赛琳的设定")
    co2 = FakeDoc("sr_hyacine", "风堇", body="我是风堇的设定")
    blk = build_block(main, [co1, co2], msg_mode=1, is_private=True)
    assert "同时扮演" in blk and "香奈美" in blk
    assert "赛琳" in blk and "风堇" in blk
    assert "【" in blk and "】" in blk, "必须带【角色名】发言格式约束"
    assert "我是赛琳的设定" in blk and "我是风堇的设定" in blk
    assert "说话习惯" in blk, "副人格应带说话习惯摘要"


def test_build_block_empty():
    assert build_block(FakeDoc("m", "主"), []) == ""
    assert build_block(FakeDoc("m", "主"), None) == ""


def test_build_block_no_framework_repeat():
    """副人格不重复注入通用框架（避免与主装配打架、省 token）。"""
    blk = build_block(FakeDoc("m", "主"), [FakeDoc("c", "副")])
    assert "# 通用框架规则" not in blk


# ---------------------------------------------------------------- 装配分区

def test_assembler_costars_in_dynamic_not_stable():
    inp = AssemblyInput(doc=FakeDoc("m", "主"),
                        costars="# 多人格同台\n\n你同时扮演……")
    res = PromptAssembler(framework=False).build(inp)
    assert H_COSTARS in res.dynamic_text, "同台块必须落在动态段"
    assert H_COSTARS not in res.system_suffix, "同台块不得进稳定段/system_prompt"
    assert res.parts.get("costars"), "动态段应记下 costars 分区字节数"


def test_assembler_without_costars_has_no_block():
    inp = AssemblyInput(doc=FakeDoc("m", "主"))
    res = PromptAssembler(framework=False).build(inp)
    assert H_COSTARS not in res.dynamic_text


# ---------------------------------------------------------------- WebUI builder

def test_webadmin_costars_builder():
    """管理接口要用真实运行时对象跑通（含写操作）。"""
    from test_admin_api import _plugin
    from core import webadmin

    p = _plugin()
    # FakePlugin 是薄替身：build_costars 对取不到的方法都会优雅降级，
    # 这里补一个真实的 _persona_for 以便断言「主角」被带出。
    p._persona_for = lambda umo: p.personas.all()[0]
    umo = "Banyan:GroupMessage:1"
    pid = p.personas.ids()[0]
    p.costars.set(umo, [pid])

    d = webadmin.build_costars(p)
    assert d["count"] == 1
    row = d["rows"][0]
    assert row["umo"] == umo
    assert row["costars"] and row["costars"][0]["id"] == pid
    assert row["main_name"], "应带出主人格显示名"

    webadmin.remove_costar(p, umo, pid)
    assert webadmin.build_costars(p)["count"] == 0


# ---------------------------------------------------------------- 输出层：名牌归位

def test_speaker_headers_move_forward():
    """段中/段尾的【角色名】必须归位到它自己那段的开头。

    复现线上翻车：香奈美说完台词后紧跟的【艾卡】被切进香奈美那一段，
    用户看到的是"香奈美说【艾卡】"。
    """
    from core.output import (clean_segments, normalize_speaker_segments,
                             split_narrative)
    text = (
        "【香奈美】\n"
        "（香奈美把被子往上拢了拢，画笔往床沿一搁）\n"
        "喏，画笔让给艾卡了，香奈美乐得清闲。引航者你看着点时间，别又熬到天亮。\n"
        "【艾卡】\n"
        "（艾卡刚从柯西街区巡逻回来，人先愣住）\n"
        "诶——引、引航者？？香奈美小姐也在啊！\n"
        "（她慌忙把面具往下扒拉，声音一下没了底气）\n"
        "什、什么姿势呀你们在说什么……不、不用不用！"
    )
    segs = clean_segments(normalize_speaker_segments(
        split_narrative(text), ["香奈美", "艾卡"]))
    assert segs == [
        "【香奈美】（香奈美把被子往上拢了拢，画笔往床沿一搁）",
        "喏，画笔让给艾卡了，香奈美乐得清闲。引航者你看着点时间，别又熬到天亮",
        "【艾卡】（艾卡刚从柯西街区巡逻回来，人先愣住）",
        "诶——引、引航者？？香奈美小姐也在啊！",
        "（她慌忙把面具往下扒拉，声音一下没了底气）",
        "什、什么姿势呀你们在说什么……不、不用不用！",
    ]
    assert all(s.count("】") == s.count("【") for s in segs), "名牌不得只出现在某一段"


def test_speaker_headers_split_within_segment():
    """名牌出现在段中间 → 从这里断开；孤名段并入下一段。"""
    from core.output import normalize_speaker_segments
    assert normalize_speaker_segments(
        ["香奈美说完了。【艾卡】", "（艾卡接话）"], ["香奈美", "艾卡"]) == \
        ["香奈美说完了。", "【艾卡】（艾卡接话）"]
    # 段尾孤名（模型没写内容）：保留，绝不吞字
    assert normalize_speaker_segments(
        ["香奈美说完了。【艾卡】"], ["香奈美", "艾卡"]) == \
        ["香奈美说完了。", "【艾卡】"]


def test_speaker_headers_noop_and_conserved():
    """没给名字集合 → 原样返回；给了名字 → 拼回去等于原文（仅去空白）。"""
    from core.output import normalize_speaker_segments
    segs = ["【公告】今晚维护", "（笑）你好"]
    assert normalize_speaker_segments(segs, []) == segs
    assert normalize_speaker_segments(segs, None) == segs
    assert normalize_speaker_segments(segs, ["   "]) == segs

    raw = "【香奈美】\n（动作）\n台词。【艾卡】\n（动作2）\n台词2"
    norm = normalize_speaker_segments([raw], ["香奈美", "艾卡"])
    assert len(norm) > 1
    flat = lambda s: "".join(s.split())          # 只去空白，不比字
    assert flat("".join(norm)) == flat(raw)


def test_speaker_header_re_prefers_long_name():
    """名字是前缀关系时（「艾」/「艾卡」）不得把名字吃短。"""
    from core.output import speaker_header_re
    rx = speaker_header_re(["艾", "艾卡"])
    m = rx.search("【艾卡】来了")
    assert m is not None and m.group(0) == "【艾卡】"


# ---------------------------------------------------------------- 指令入口

def _costar_plugin():
    """真实方法 + 临时目录属性（同 `test_decision_chain._neo()` 的做法）。

    `FakePlugin`（test_admin_api 的中枢夹具，Lead 所有）只有**属性**，
    没有 main.py 的方法面。旧写法是逐个 `Neo.X.__get__` 补绑，但
    2026-10-28 的多实例重构把同台链路又拆出了 `_costars_for` /
    `_persona_ok` / `_set_costars` 三个内部方法，于是：

      * `_speaker_names()` 里 `_costar_docs → _costars_for` 抛
        AttributeError，被外层 `except` 兜住 → **静默返回 []**
        （表现成"没开同台"，最难查的那种失败）；
      * `_handle_costar_command()` 直接 AttributeError 崩掉。

    改用 `_LightNeo.__new__` 造一个壳、把 `FakePlugin.__dict__` 搬过去：
    真实方法全部可直接调用，数据仍然全落在 `_plugin()` 的临时目录里
    （不碰线上 state / 人格数据）。
    """
    from test_admin_api import _plugin
    from test_decision_chain import _LightNeo

    obj = _LightNeo.__new__(_LightNeo)
    obj.__dict__.update(_plugin().__dict__)
    # 夹具里没有真人会话参数表 / 人格选择记录，这两条是同台生效的**前置
    # 条件**，显式给定（测试关心的是同台链路，不是这两处取值）：
    obj._persona_for = lambda umo: obj.personas.all()[0]   # 主角 = all()[0]
    obj._costar_mode_ok = lambda umo, ip: True             # 本会话是剧情模式
    return obj


def test_speaker_names_gate():
    """角色名集合的闸门：剧情模式 **且** 本会话确有副人格才给出。"""
    p = _costar_plugin()
    umo = "Banyan:FriendMessage:100000001"
    assert p._speaker_names(umo, True) == [], "没开同台 → 不处理"

    co = p.personas.ids()[1]
    p.costars.set(umo, [co])
    names = p._speaker_names(umo, True)
    assert p._pname(co) in names, names
    assert p._persona_for(umo).display_name in names, "主角名也算名牌"

    p._costar_mode_ok = lambda umo, ip: False        # 非剧情模式：同台不生效
    assert p._speaker_names(umo, True) == []



def test_command_add_and_remove_notifies_session():
    """/persona cast add|rm 必须产生「xx 已加入/离开会话」提示。"""
    p = _costar_plugin()
    umo = "Banyan:GroupMessage:1"
    ids = p.personas.ids()
    assert len(ids) >= 2, "需要至少两个人格才能测『副人格≠主角』"
    co = ids[1]                                  # ids[0] 是主角（all()[0]）

    msg = p._handle_costar_command(umo, "cast", ["add", co], is_private=False)
    assert "已加入会话" in msg, msg
    assert co in p.costars.cast_of(umo)

    msg2 = p._handle_costar_command(umo, "cast", ["rm", co], is_private=False)
    assert "已离开会话" in msg2, msg2
    assert p.costars.cast_of(umo) == []


def test_command_rejects_add_when_not_narrative():
    p = _costar_plugin()
    p._costar_mode_ok = lambda umo, ip: False   # 非剧情模式
    umo = "Banyan:GroupMessage:1"
    msg = p._handle_costar_command(umo, "cast", ["add", p.personas.ids()[1]],
                                   is_private=False)
    assert "剧情模式" in msg, msg
    assert p.costars.cast_of(umo) == [], "非剧情模式不得写入同台"


def test_command_skips_main_persona():
    p = _costar_plugin()
    umo = "Banyan:GroupMessage:1"
    main = p.personas.ids()[0]                   # = _persona_for 的返回值
    msg = p._handle_costar_command(umo, "cast", ["add", main], is_private=False)
    assert "主角" in msg, msg
    assert p.costars.cast_of(umo) == [], "主角不得被加为副人格"


