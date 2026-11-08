# -*- coding: utf-8 -*-
"""管理页「群名/昵称」修复的回归测试。

背景（用户报的两个现象）
------------------------
1. 群会话的标签显示的不是群名 —— 旧 `_session_label` 没有群名数据，
   就把「小明、小红 等 3 人」摆在群名的位置，看着像群名就叫这个。
2. 印象页的用户下拉是一堆光秃秃的 QQ 号：既没昵称（迁移过来的历史
   用户从来没记过），又把 271 个 `{"msg_count": 1}` 空壳也铺了进去。

这里锁住的是修复后的**契约**，不是实现细节：
  * 群名取不到时宁可写"未取到群名"，也不能拿成员昵称冒充；
  * 印象下拉默认只给有正文的用户；
  * 平台给的 `"N/A"` 绝不能当群名存下来；
  * 私聊印象要落到对方 QQ 上，不能再写成 `<umo>.json`。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from core import webadmin  # noqa: E402
from core.sessionmap import JsonMap  # noqa: E402

from test_admin_api import _plugin  # noqa: E402
import _env  # noqa: E402


# ---------------------------------------------------------------- 事件替身

class _Group:
    def __init__(self, name):
        self.group_name = name


class _MsgObj:
    def __init__(self, name):
        self.group = _Group(name)


class _Ev:
    """够 `_note_identity` / `_impression_uid` 用的最小事件。"""

    def __init__(self, *, umo="Banyan:GroupMessage:1", gid="1",
                 gname="星绘的秘密基地", uid="100000001", name="江一楠"):
        self.unified_msg_origin = umo
        self.message_obj = _MsgObj(gname)
        self._gid, self._uid, self._name = gid, uid, name

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return self._name


class _NameStub:
    """只带两张映射表的假 self：让 main.py 的**真实方法**能被直接调用。"""

    def __init__(self, root: Path):
        self._user_names = JsonMap(root / "_user_names.json")
        self._group_names = JsonMap(root / "_group_names.json")

    def _note_group_name(self, event):
        # `_note_identity` 会回头调它，转发到真实现上
        return _neo()._note_group_name(self, event)

    def _sender_id(self, event):
        # 同样转发到真实现，别在测试里复写一遍逻辑
        return _neo()._sender_id(event)


def _neo():
    """拿到真的 `ChatCoreNeoPlugin` 类（只要能 import，不实例化）。"""
    sys.path.insert(0, str(PLUGIN.parent))
    _APP = _env.app()
    if _APP.is_dir():
        sys.path.insert(0, str(_APP))
    import importlib
    try:
        mod = importlib.import_module(f"{PLUGIN.name}.main")
    except Exception as e:
        from run_tests import Skip
        raise Skip(f"无法导入插件入口：{e!r}")
    return mod.ChatCoreNeoPlugin


# ---------------------------------------------------------------- 会话标签

def test_group_label_uses_real_group_name():
    """标签顺序按用户要求：实例 → 群聊/私聊 → 名字。"""
    names = {"100000001": "江一楠"}
    members = {"p1": ["100000001", "100000002"]}
    names["100000002"] = "星绘"
    lab = webadmin._session_label("Banyan:GroupMessage:100000006", members,
                                  names, {"Banyan:GroupMessage:100000006":
                                          "星绘的秘密基地"})
    assert lab == "Banyan · 群聊 · 星绘的秘密基地", lab


def test_session_label_parts_are_structured():
    """前端要靠拆开的字段做对齐（实例补齐 + 等宽），不能只有一个字符串。"""
    lp = webadmin.session_label_parts(
        "Sui:GroupMessage:100000004", {}, {},
        {"Sui:GroupMessage:100000004": "AR-26710-1｜星晨伴月の极光夜空（壹）"})
    assert lp["instance"] == "Sui" and lp["kind"] == "群聊"
    assert lp["sid"] == "100000004"
    assert lp["name"] == "AR-26710-1｜星晨伴月の极光夜空（壹）"
    assert lp["label"] == "Sui · 群聊 · AR-26710-1｜星晨伴月の极光夜空（壹）"
    pv = webadmin.session_label_parts("Banyan:FriendMessage:100000001", {},
                                      {"100000001": "想抱着艾卡睡觉"})
    assert pv["instance"] == "Banyan" and pv["kind"] == "私聊"
    assert pv["label"] == "Banyan · 私聊 · 想抱着艾卡睡觉"
    # 认不出来的 umo 原样返回，不炸
    assert webadmin.session_label_parts("junk")["label"] == "junk"


def test_group_label_never_fakes_name_with_member_nicks():
    """取不到群名时必须**明说**，不能把成员昵称摆在名字的位置。

    旧实现产出的是 `Banyan|群聊|100000006|江一楠、星绘 等2人` ——
    用户看到的就是"群昵称显示错误"。
    """
    names = {"100000001": "江一楠", "100000002": "星绘"}
    members = {"p1": ["100000001", "100000002"]}
    lab = webadmin._session_label("Banyan:GroupMessage:100000006", members,
                                  names, {})
    assert "江一楠" not in lab and "星绘" not in lab, lab
    assert lab == "Banyan · 群聊 · 群 100000006（未取到群名）", lab


def test_private_label_uses_nickname():
    lab = webadmin._session_label("Banyan:FriendMessage:100000001", {},
                                  {"100000001": "★Leaked.翼龙☆"}, {})
    assert lab == "Banyan · 私聊 · ★Leaked.翼龙☆", lab
    # 没有昵称也不能崩，退回 QQ 号
    assert webadmin._session_label("Banyan:FriendMessage:100000001", {}, {}, {}) \
        == "Banyan · 私聊 · 100000001"


def test_group_name_survives_in_sessions_payload():
    p = _plugin()
    umo = "Sui:GroupMessage:1"
    st = p.state.load(umo, "p1", "")
    p.state.save(umo, "p1", "", st)
    p._group_names.set(umo, "汐的小窝")
    d = webadmin.build_sessions(p)
    assert d["group_names"].get(umo) == "汐的小窝"
    entry = [s for s in d["sessions"] if s["umo"] == umo][0]
    assert entry["group_name"] == "汐的小窝", entry
    assert "汐的小窝" in d["labels"][umo], d["labels"][umo]


# ---------------------------------------------------------------- 印象下拉

def _imp(root: Path, uid: str, **kw) -> None:
    d = root / "_impressions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uid}.json").write_text(json.dumps(kw, ensure_ascii=False),
                                   encoding="utf-8")


def test_users_with_impression_excludes_empty_stubs():
    """314 个用户里 271 个是空壳 —— 下拉默认不能把这些也列出来。"""
    p = _plugin()
    umo = "Sui:GroupMessage:1"
    p.state.save(umo, "p1", "42", p.state.load(umo, "p1", "42"))
    _imp(p.state.root, "42", sfw="喜欢聊游戏", msg_count=19, updated=100)
    _imp(p.state.root, "43", sfw="", nsfw="", msg_count=1, updated=0)
    _imp(p.state.root, "44", sfw_custom="人工写的", msg_count=2, updated=300)

    d = webadmin.build_sessions(p)
    assert set(d["users_with_impression"]) == {"42", "44"}, d["users_with_impression"]
    # 按更新时间降序
    assert d["users_with_impression"] == ["44", "42"]
    # 空壳仍在「全部用户」里（切开关后能看到、能编辑）
    assert "43" in {u["uid"] for u in d["users"]}
    meta = {u["uid"]: u for u in d["users"]}
    assert meta["42"]["has_impression"] is True
    assert meta["43"]["has_impression"] is False
    assert meta["42"]["msg_count"] == 19


def test_junk_impression_files_still_listed_as_stale():
    """归不到人的印象文件不进索引，但必须还能在残留里看到、能清掉。

    群号不是用户（`..._GroupMessage_<群号>`），没法算成谁的印象 ——
    这类才是真残留；私聊形态的归对方本人（另有专门用例）。
    """
    p = _plugin()
    _imp(p.state.root, "Banyan_GroupMessage_100000006", msg_count=3)
    _imp(p.state.root, "42", sfw="有正文", updated=1)
    d = webadmin.build_sessions(p)
    assert "Banyan_GroupMessage_100000006" in {
        u["uid"] for u in d["stale_users"]}, d["stale_users"]
    assert "Banyan_GroupMessage_100000006" not in {
        u["uid"] for u in d["users"]}
    assert d["users_with_impression"] == ["42"]


def test_users_expose_names_for_the_dropdown():
    p = _plugin()
    _imp(p.state.root, "42", sfw="有正文", updated=5)
    p._user_names.set("42", "小明")
    d = webadmin.build_sessions(p)
    u = [x for x in d["users"] if x["uid"] == "42"][0]
    assert u["name"] == "小明", u


def test_nickname_equal_to_qq_counts_as_unknown():
    """平台偶尔把 QQ 号当昵称返回；显示成「100000005（100000005）」很蠢。"""
    assert webadmin._nick({"1": "1"}, "1") == ""
    assert webadmin._nick({"1": " 1 "}, "1") == ""
    assert webadmin._nick({"1": "小明"}, "1") == "小明"
    assert webadmin._nick({}, "1") == ""
    p = _plugin()
    p._user_names.set("100000005", "100000005")
    d = webadmin.build_sessions(p)
    u = [x for x in d["users"] if x["uid"] == "100000005"][0]
    assert u["name"] == "", u
    lab = webadmin._session_label("Banyan:FriendMessage:100000005", {},
                                  p._user_names.as_dict(), {})
    assert lab == "Banyan · 私聊 · 100000005", lab


# ---------------------------------------------------------------- 补名

class _Bot:
    def __init__(self, *, fail=(), group_list=()):
        self.calls: list[tuple] = []
        self.fail = set(fail)
        self.group_list = list(group_list)

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        if action in self.fail:
            raise RuntimeError("boom")
        if action == "get_group_list":
            return [{"group_id": g, "group_name": n}
                    for g, n in self.group_list]
        if action == "get_group_info":
            return {"group_name": "星绘的秘密基地"}
        if action == "get_group_member_list":
            return [{"user_id": 42, "nickname": "小明", "card": "小明明"},
                    {"user_id": 43, "nickname": "小红", "card": ""},
                    {"user_id": 44, "nickname": "44"},
                    {"user_id": 0}]
        if action == "get_friend_list":
            return [{"user_id": 100000001, "nickname": "翼龙",
                     "remark": "江一楠"}]
        return None


class _Inst:
    def __init__(self, bot):
        self.bot = bot


class _Ctx:
    def __init__(self, insts):
        self.insts = insts

    def get_platform_inst(self, pid):
        return self.insts.get(pid)


def test_refresh_names_fills_group_names_and_members():
    with tempfile.TemporaryDirectory() as td:
        p = _plugin()
        p.state.root = Path(td)
        umo = "Banyan:GroupMessage:100000006"
        p.state.save(umo, "p1", "", p.state.load(umo, "p1", ""))
        bot = _Bot()
        p.context = _Ctx({"Banyan": _Inst(bot)})
        r = asyncio.run(webadmin.refresh_names(p))
        assert r["group_names"] == 1, r
        assert r["users"] >= 3, r
        assert p._group_names.get(umo) == "星绘的秘密基地"
        # 群名片优先，其次账号昵称；没有名字 / 名字就是 QQ 的成员不写
        assert p._user_names.get("42") == "小明明", p._user_names.as_dict()
        assert p._user_names.get("43") == "小红"
        assert "44" not in p._user_names.as_dict(), p._user_names.as_dict()
        assert "0" not in p._user_names.as_dict()
        assert p._user_names.get("100000001") == "江一楠"    # 好友备注优先
        assert not r["errors"], r["errors"]
        # 群列表没给 → 回退到逐群 get_group_info
        assert any(a == "get_group_info" for a, _ in bot.calls), bot.calls
        # 建好的会话标签应该已经用上群名
        assert "星绘的秘密基地" in webadmin.build_sessions(p)["labels"][umo]


def test_refresh_names_nicknames_fill_missing_only():
    """群名片常是玩笑话 —— 默认不许覆盖已有的账号昵称，force=True 才覆盖。"""
    with tempfile.TemporaryDirectory() as td:
        p = _plugin()
        p.state.root = Path(td)
        p.state.save("Banyan:GroupMessage:1", "p1", "",
                     p.state.load("Banyan:GroupMessage:1", "p1", ""))
        p._user_names.set("42", "好昵称")      # 已有的不许动
        bot = _Bot()
        p.context = _Ctx({"Banyan": _Inst(bot)})
        r = asyncio.run(webadmin.refresh_names(p))
        assert p._user_names.get("42") == "好昵称", p._user_names.as_dict()
        assert p._user_names.get("43") == "小红"          # 缺的补上
        assert r["users"] >= 1, r
        # force=True → 按平台现状刷一遍（42 的群名片是「小明明」）
        r2 = asyncio.run(webadmin.refresh_names(p, groups=False, friends=False,
                                                force=True))
        assert p._user_names.get("42") == "小明明", p._user_names.as_dict()
        assert r2["users"] >= 1, r2


def test_refresh_names_prefers_group_list_over_per_group_calls():
    """`get_group_list` 一次就带回全部群名，不该再逐群问一遍。"""
    with tempfile.TemporaryDirectory() as td:
        p = _plugin()
        p.state.root = Path(td)
        for gid in (1, 2, 3):
            umo = f"Banyan:GroupMessage:{gid}"
            p.state.save(umo, "p1", "", p.state.load(umo, "p1", ""))
        bot = _Bot(group_list=[(1, "一号群"), (2, "二号群"), (3, "三号群")])
        p.context = _Ctx({"Banyan": _Inst(bot)})
        r = asyncio.run(webadmin.refresh_names(p))
        assert r["groups"] == 3 and r["group_names"] == 3, r
        assert not any(a == "get_group_info" for a, _ in bot.calls), bot.calls
        assert p._group_names.get("Banyan:GroupMessage:2") == "二号群"


def test_refresh_names_uses_get_client_when_present():
    """适配器取法用 `get_client()`（qqadmin 走的就是这条，本机验证过）。"""

    class _Inst2:
        def __init__(self, bot):
            self.bot = bot

        def get_client(self):
            return self.bot

    with tempfile.TemporaryDirectory() as td:
        p = _plugin()
        p.state.root = Path(td)
        p.state.save("Banyan:GroupMessage:1", "p1", "",
                     p.state.load("Banyan:GroupMessage:1", "p1", ""))
        p.context = _Ctx({"Banyan": _Inst2(_Bot())})
        r = asyncio.run(webadmin.refresh_names(p))
        assert r["group_names"] == 1, r


def test_refresh_names_degrades_gracefully():
    """没有平台实例 / 适配器不给调 API / 某个调用失败 —— 都不能抛，要如实汇报。"""
    with tempfile.TemporaryDirectory() as td:
        p = _plugin()
        p.state.root = Path(td)
        p.state.save("Banyan:GroupMessage:1", "p1", "",
                     p.state.load("Banyan:GroupMessage:1", "p1", ""))
        # ① 完全没有平台实例
        p.context = _Ctx({})
        r = asyncio.run(webadmin.refresh_names(p))
        assert r["groups"] == 1 and r["group_names"] == 0, r
        assert not r["errors"], r
        # ② 适配器不允许直接调 API
        p.context = _Ctx({"Banyan": _Inst(object())})
        r = asyncio.run(webadmin.refresh_names(p))
        assert r["group_names"] == 0 and r["users"] == 0, r
        assert any("适配器" in s for s in r["skipped"]), r
        # ③ 调用抛错 → 列进 errors，但函数正常返回
        p.context = _Ctx({"Banyan": _Inst(_Bot(fail=("get_group_info",
                                                     "get_group_member_list",
                                                     "get_friend_list")))})
        r = asyncio.run(webadmin.refresh_names(p))
        assert r["group_names"] == 0 and r["users"] == 0, r
        assert len(r["errors"]) >= 2, r


def test_refresh_names_respects_time_budget():
    """总预算用完就收工 —— 页面桥只有 60s，卡住的平台不能把整个请求拖死。"""
    with tempfile.TemporaryDirectory() as td:
        p = _plugin()
        p.state.root = Path(td)
        p.state.save("Banyan:GroupMessage:1", "p1", "",
                     p.state.load("Banyan:GroupMessage:1", "p1", ""))
        bot = _Bot()
        p.context = _Ctx({"Banyan": _Inst(bot)})
        old = webadmin._API_DEADLINE
        webadmin._API_DEADLINE = -1.0          # 一开始就已超时
        try:
            r = asyncio.run(webadmin.refresh_names(p))
        finally:
            webadmin._API_DEADLINE = old
        assert r["deadline_hit"] is True, r
        assert not bot.calls, bot.calls
        assert r["groups"] == 1, r             # 仍然如实汇报涉及多少会话


def test_refresh_names_needs_context():
    p = _plugin()
    p.context = None
    try:
        asyncio.run(webadmin.refresh_names(p))
    except ValueError:
        return
    raise AssertionError("没有上下文时应当报错，而不是静默什么都不做")


# ---------------------------------------------------------------- main 侧

def test_note_group_name_rejects_placeholders():
    """`N/A` 是框架适配器在平台没带群名时填的占位符，绝不能存成群名。"""
    Neo = _neo()
    with tempfile.TemporaryDirectory() as td:
        stub = _NameStub(Path(td))
        umo = "Banyan:GroupMessage:1"
        assert Neo._note_group_name(stub, _Ev(umo=umo, gname="N/A")) == ""
        assert Neo._note_group_name(stub, _Ev(umo=umo, gname="n/a")) == ""
        assert Neo._note_group_name(stub, _Ev(umo=umo, gname="1")) == ""
        assert Neo._note_group_name(stub, _Ev(umo=umo, gname="  ")) == ""
        assert stub._group_names.as_dict() == {}, stub._group_names.as_dict()
        # 真名照收，重复也不重写
        assert Neo._note_group_name(stub, _Ev(umo=umo,
                                              gname="星绘的秘密基地")) == "星绘的秘密基地"
        assert stub._group_names.get(umo) == "星绘的秘密基地"
        # 非群消息直接跳过
        assert Neo._note_group_name(stub, _Ev(umo="Banyan:FriendMessage:1",
                                              gid="", gname="x")) == ""


def test_note_identity_records_user_and_group():
    Neo = _neo()
    with tempfile.TemporaryDirectory() as td:
        stub = _NameStub(Path(td))
        Neo._note_identity(stub, _Ev())
        assert stub._user_names.get("100000001") == "江一楠"
        assert stub._group_names.get("Banyan:GroupMessage:1") == "星绘的秘密基地"


def test_impression_uid_private_falls_back_to_sender():
    """私聊 uid 为空，印象要落到对方 QQ 上 —— 不能再生成 `<umo>.json`。"""
    Neo = _neo()
    with tempfile.TemporaryDirectory() as td:
        stub = _NameStub(Path(td))
        ev = _Ev(umo="Banyan:FriendMessage:100000001", gid="", uid="100000001")
        assert Neo._impression_uid(stub, ev, "", ev.unified_msg_origin) == "100000001"
        # 群聊有 uid 时原样用
        assert Neo._impression_uid(stub, ev, "42", "u") == "42"
        # 连 sender 都取不到才退回 umo（合成事件）
        ev2 = _Ev(umo="Banyan:FriendMessage:1", gid="", uid="")
        assert Neo._impression_uid(stub, ev2, "", "Banyan:FriendMessage:1") \
            == "Banyan:FriendMessage:1"


def test_update_many_writes_once():
    """补几百个昵称不能逐个落盘（O(n²) 的磁盘 IO）。"""
    with tempfile.TemporaryDirectory() as td:
        m = JsonMap(Path(td) / "m.json")
        saves = []
        orig = m.save

        def spy():
            saves.append(1)
            orig()

        m.save = spy                      # type: ignore[assignment]
        n = m.update_many({f"u{i}": f"n{i}" for i in range(200)})
        assert n == 200 and len(saves) == 1, (n, len(saves))
        assert m.update_many({"u0": "n0"}) == 0     # 值没变就不写
        assert len(saves) == 1


# ---------------------------------------------------------------- 旧环境补缺

def _legacy_cache(tmp: Path, body: dict) -> Path:
    """造一个假的旧环境 `_name_cache.json`，返回它的路径。"""
    d = tmp / "legacy" / "plugin_data" / "lili_state"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "_name_cache.json"
    f.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return f


def _paths_mod(Neo):
    """拿到 main.py **真正**用的那个 `core.paths` 模块对象。

    插件在 sys.path 上有两个可导入名（`core.paths` 与
    `astrbot_plugin_chatcore_neo.core.paths`），是两个不同的模块对象；
    照着 `Neo.__module__` 取才改得对地方。
    """
    return sys.modules[Neo.__module__].paths


def test_import_legacy_names_fills_missing_only():
    """旧 lili_state 缓存里的群名/成员昵称，只补缺、绝不覆盖新数据。"""
    Neo = _neo()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _legacy_cache(tmp, {
            "g:Banyan:1": ["旧群名", 1788346263.0],
            "g:Banyan:2": ["只有旧环境有的群", 1788346263.0],
            "ml:Banyan:1": [{"42": "小明", "43": "小红", "44": "44"}],
            "u:Banyan:100000001": ["★Leaked.翼龙☆", 1788346263.0],
        })
        stub = _NameStub(tmp)
        # 实时已经拿到的：不许被旧的覆盖
        stub._group_names.set("Banyan:GroupMessage:1", "新群名")
        stub._user_names.set("42", "新昵称")

        paths_mod = _paths_mod(Neo)
        orig = paths_mod.LEGACY_NAME_CACHE
        paths_mod.LEGACY_NAME_CACHE = f
        try:
            r = Neo._import_legacy_names(stub)
        finally:
            paths_mod.LEGACY_NAME_CACHE = orig

        assert r["groups"] == 1, r          # 只补了群 2
        assert r["users"] == 2, r           # 43 与 100000001；42 已知道，44 的名字=QQ
        assert stub._group_names.get("Banyan:GroupMessage:1") == "新群名"
        assert stub._group_names.get("Banyan:GroupMessage:2") == "只有旧环境有的群"
        assert stub._user_names.get("42") == "新昵称"      # 没被覆盖
        assert stub._user_names.get("43") == "小红"
        assert stub._user_names.get("44") is None          # 昵称=QQ 不算昵称
        assert stub._user_names.get("100000001") == "★Leaked.翼龙☆"


def test_import_legacy_names_is_self_healing_and_fail_open():
    """每次加载都补缺 —— 不能被"导过了"的标记挡住（踩过这个坑）。

    这两张表在运行期是**整份内存快照回写**的：离线导入的几千条会被在线实例
    的旧内存版本覆盖。若再按源文件签名跳过，数据就永远回不来了。
    """
    Neo = _neo()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _legacy_cache(tmp, {"g:Banyan:9": ["九号群", 1.0],
                                "u:Banyan:777": ["小七", 1.0]})
        stub = _NameStub(tmp)
        paths_mod = _paths_mod(Neo)
        orig = paths_mod.LEGACY_NAME_CACHE
        paths_mod.LEGACY_NAME_CACHE = f
        try:
            r1 = Neo._import_legacy_names(stub)
            assert r1["groups"] == 1 and r1["users"] == 1, r1
            assert stub._group_names.get("Banyan:GroupMessage:9") == "九号群"
            # 没东西可补了 → 报 skipped（而不是"因为导过所以跳过"）
            r2 = Neo._import_legacy_names(stub)
            assert r2["skipped"] is True and r2["groups"] == 0, r2
            # 模拟一次"被在线实例覆盖"：抹掉两条，下次加载要能自愈
            stub._group_names.pop("Banyan:GroupMessage:9")
            stub._user_names.pop("777")
            r3 = Neo._import_legacy_names(stub)
            assert r3["groups"] == 1 and r3["users"] == 1, r3
            assert stub._group_names.get("Banyan:GroupMessage:9") == "九号群"
            assert stub._user_names.get("777") == "小七"
            # 源文件不存在 / 是坏 JSON → 跳过而不是抛，也不破坏已有数据
            paths_mod.LEGACY_NAME_CACHE = tmp / "nope.json"
            r4 = Neo._import_legacy_names(stub)
            assert r4["skipped"] is True and r4["groups"] == 0, r4
            bad = tmp / "bad.json"
            bad.write_text("{ not json", encoding="utf-8")
            paths_mod.LEGACY_NAME_CACHE = bad
            r5 = Neo._import_legacy_names(stub)
            assert r5["groups"] == 0, r5
            assert stub._group_names.get("Banyan:GroupMessage:9") == "九号群"
        finally:
            paths_mod.LEGACY_NAME_CACHE = orig


def test_legacy_cache_path_is_derived_not_hardcoded():
    """旧环境路径要从本插件位置反推，不能写死绝对路径。"""
    from core import paths as paths_mod
    assert paths_mod.LEGACY_DATA_DIR.name == "data"
    assert paths_mod.LEGACY_DATA_DIR.parent.name == ".astrbot"
    assert paths_mod.LEGACY_DATA_DIR.parent.parent == paths_mod.DATA_DIR.parent.parent
    assert paths_mod.LEGACY_NAME_CACHE.name == "_name_cache.json"
    assert "lili_state" in str(paths_mod.LEGACY_NAME_CACHE)
