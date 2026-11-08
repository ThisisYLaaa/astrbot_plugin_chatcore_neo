# -*- coding: utf-8 -*-
"""persona_list / persona_switch 两个 LLM 工具的行为测试。

工具的核心承诺：
  * list 返回全部人格 + 当前人格 + 简短描述（截断）；
  * switch 仅特权（intimate）可切；找不到给候选；同人格幂等；
  * 真正切换时：会话覆盖即时生效 + 下一轮提醒写入 + 挂起重入任务
    （重入由 `_reenter_after_persona_switch` 在新请求里重建 system prompt，
    见 main.py 注释——这是「工具与 system 打架」问题的解法）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
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


class _Ev:
    """最小事件桩：够两个工具用。"""

    def __init__(self, umo, uid="100000001", name="翼龙", private=False):
        self.umo = umo
        self.uid = uid
        self.name = name
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

    def get_group_id(self):
        return "200000001"

    def get_self_id(self):
        return "300000005"

    @property
    def message_str(self):
        return ""


def _tool_plugin():
    """FakePlugin + 绑定真实实现（工具及其依赖）。"""
    p = _plugin()
    for name in ("_persona_for", "_not_found", "_apply_persona_switch",
                 "_maybe_set_group_card", "_poke_sender",
                 "_reenter_after_persona_switch",
                 # 2026-10-28 人格池加了实例授权（instances/<id>/personas.json
                 # 的 grants）之后，persona_switch_tool 在真正切换前会多过一道
                 # `self._persona_ok()` / `self._persona_denied()`。两者都是真实
                 # 类上的普通方法（main.py），工具通过 self 调它们；夹具逐方法
                 # 绑定真实实现的模式必须跟着补，否则 AttributeError 会被
                 # 工具兜成 {"error": "internal"}。
                 "_persona_ok", "_persona_denied",
                 "persona_list_tool", "persona_switch_tool"):
        setattr(p, name, getattr(Neo, name).__get__(p))
    # staticmethod：直接赋函数（不绑定 self）
    p._sender_id = getattr(Neo, "_sender_id")
    # 桩 _spawn：只记录被挂起的协程（不执行），并把协程关闭以免告警
    p._spawned = []

    def _spawn(coro):
        p._spawned.append(coro)
        coro.close()

    p._spawn = _spawn
    return p


def _run(coro):
    return asyncio.run(coro)


def _bind_start(p, umo):
    """给会话显式绑一个起始人格（CFG 的 persona_current 是空串）。"""
    first = p.personas.all()[0]
    p._session_persona.set(umo, first.id)
    return first


def test_persona_list_returns_all_with_desc():
    p = _tool_plugin()
    ev = _Ev("Banyan:GroupMessage:200000001")
    out = json.loads(_run(p.persona_list_tool(ev)))
    assert out["success"] is True
    assert out["count"] == len(p.personas.all()) > 0
    for it in out["personas"]:
        assert it["id"] and it["name"]
        assert len(it["desc"]) <= 81, "desc 应截断到 80 字符内"


def test_persona_list_reports_current_and_filters():
    p = _tool_plugin()
    umo = "Banyan:GroupMessage:200000001"
    ev = _Ev(umo)
    first = _bind_start(p, umo)
    out = json.loads(_run(p.persona_list_tool(ev)))
    assert out["current"] == first.id
    # 用 id 当关键词 → 至少命中它、且少于全量
    hit = json.loads(_run(p.persona_list_tool(ev, keyword=first.id)))
    ids = [x["id"] for x in hit["personas"]]
    assert first.id in ids
    assert 0 < len(ids) < out["count"]


def test_persona_switch_denies_non_privileged():
    p = _tool_plugin()
    umo = "Banyan:GroupMessage:200000001"
    ev = _Ev(umo, uid="999", name="路人")
    target = p.personas.all()[1]
    out = json.loads(_run(p.persona_switch_tool(ev, persona=target.id)))
    assert out["success"] is False and out["error"] == "permission_denied"
    assert p._session_persona.get(umo) is None, "拒绝时不能写会话覆盖"
    assert not p._spawned, "拒绝时不能重入"


def test_persona_switch_not_found_lists_candidates():
    p = _tool_plugin()
    ev = _Ev("Banyan:GroupMessage:200000001")
    out = json.loads(_run(p.persona_switch_tool(
        ev, persona="绝对不存在的角色xyz")))
    assert out["success"] is False and out["error"] == "not_found"
    assert "candidates" in out
    assert not p._spawned


def test_persona_switch_same_persona_is_noop():
    p = _tool_plugin()
    umo = "Banyan:GroupMessage:200000001"
    ev = _Ev(umo)
    first = _bind_start(p, umo)
    out = json.loads(_run(p.persona_switch_tool(ev, persona=first.id)))
    assert out["success"] is True and out.get("already_current") is True
    assert not p._spawned, "同人格不重入"


def test_persona_switch_applies_and_reenters():
    p = _tool_plugin()
    umo = "Banyan:GroupMessage:200000001"
    ev = _Ev(umo)
    old = _bind_start(p, umo)
    target = next(d for d in p.personas.all() if d.id != old.id)
    out = json.loads(_run(p.persona_switch_tool(ev, persona=target.id)))
    assert out["success"] is True
    assert out["switched_to"] == target.id
    # 会话覆盖即时生效
    assert p._persona_for(umo).id == target.id
    # 下一轮提醒已写入（新人格名 + 旧人格名）
    notice = p._persona_switch_notice.get(umo, "")
    assert target.display_name in notice and old.display_name in notice
    # 重入任务已挂起（而不是在当前轮里硬回）
    names = [c.cr_code.co_name for c in p._spawned]
    assert "_reenter_after_persona_switch" in names


def test_persona_switch_private_counts_as_privileged():
    p = _tool_plugin()
    umo = "Banyan:FriendMessage:100000001"
    ev = _Ev(umo, uid="100000001", private=True)
    old = _bind_start(p, umo)
    target = next(d for d in p.personas.all() if d.id != old.id)
    out = json.loads(_run(p.persona_switch_tool(ev, persona=target.id)))
    assert out["success"] is True
    assert p._persona_for(umo).id == target.id


def test_persona_switch_refuses_ungranted_persona():
    """人格池按实例授权：目标人格不在本实例 grants 里 → not_granted，且不改状态。

    `test_admin_api` 的夹具池是「不做授权过滤」的宽松子类（绝大多数用例
    关心的是"有几个人格"），所以 `_persona_ok` 这条闸门在别处是走过场。
    这里显式换一个**真实** PersonaPool，把新引入的授权判定真正跑一遍：
    拒绝时既不能写会话覆盖，也不能挂重入任务。
    """
    from core.persona_pool import PersonaPool  # noqa: PLC0415

    p = _tool_plugin()
    umo = "Banyan:GroupMessage:200000001"
    ev = _Ev(umo)
    old = _bind_start(p, umo)
    target = next(d for d in p.personas.all() if d.id != old.id)

    pool = PersonaPool(Path(tempfile.mkdtemp()))
    pool.set_grants("Banyan", [old.id])          # 只授权当前人格
    p._persona_pool = pool

    out = json.loads(_run(p.persona_switch_tool(ev, persona=target.id)))
    assert out["success"] is False and out["error"] == "not_granted", out
    assert p._session_persona.get(umo) == old.id, "拒绝时不能改写会话人格"
    assert not p._spawned, "拒绝时不能重入"


def test_nsfw_not_suppressed_on_persona_switch_reenter():
    """切人格的重入是「用户对话的延续」，不该被强制关 NSFW。

    回归：`persona_switch` 的重入事件带 simulated 标记，曾被
    `if proactive: eff_nsfw = False` 误伤 —— 会话明明开着 NSFW，
    换人格那一轮的正向规范却整段消失（表现为"像被禁了"）。
    规则：只有 bot **自己发起**的合成事件（主动消息 / 戳一戳回应）才压。
    """
    from core import compat  # noqa: PLC0415

    suppress = Neo._nsfw_suppressed

    real = _Ev("Banyan:FriendMessage:100000001", private=True)
    assert suppress(real) is False, "真实消息不该压 NSFW"

    for src in ("proactive", "poke"):
        ev = _Ev("Banyan:FriendMessage:100000001", private=True)
        compat.mark_simulated(ev, source=src)
        assert suppress(ev) is True, f"{src} 是 bot 自己发起，应压掉 NSFW"

    ev = _Ev("Banyan:FriendMessage:100000001", private=True)
    compat.mark_simulated(ev, source="persona_switch")
    assert suppress(ev) is False, (
        "人格切换的重入是用户对话的延续，NSFW 必须跟会话参数走")


# ───────────────────────────────────────────── 实例授权：空表 = 不限制

def test_persona_pool_empty_grants_means_unrestricted():
    """**空授权表 = 不限制**（2026-10-28 定的口径）。

    新接进来的实例还没配 `instances/<id>/personas.json`，
    不能让它一个人格都不可用（否则 `/persona list` 全空、
    组人格全部回退，表现为"这个实例的人设全是错的"）。
    """
    from core.persona_pool import PersonaPool  # noqa: PLC0415

    pool = PersonaPool(Path(tempfile.mkdtemp()))
    docs = [type("D", (), {"id": f"p{i}"})() for i in range(3)]
    assert pool.is_available("NewInst", "p0") is True, "空表必须放行"
    assert len(pool.available_docs("NewInst", docs)) == 3
    assert pool.available_ids("NewInst") == set(), "空表本身仍返回空集合"


def test_persona_pool_nonempty_grants_still_filters():
    """有授权表时照旧过滤（别把闸门整个取消）。"""
    from core.persona_pool import PersonaPool  # noqa: PLC0415

    pool = PersonaPool(Path(tempfile.mkdtemp()))
    pool.set_grants("Inst", ["p1"])
    docs = [type("D", (), {"id": f"p{i}"})() for i in range(3)]
    assert pool.is_available("Inst", "p1") is True
    assert pool.is_available("Inst", "p0") is False
    assert [d.id for d in pool.available_docs("Inst", docs)] == ["p1"]


def test_not_found_candidates_filtered_by_instance():
    """「找不到人格」的候选只能列**本实例可用**的。

    回归：`personas.suggest()` 是全池模糊匹配，不过滤授权时，
    Sui 里打错字会提示出一堆只有 Banyan 授权的人格名 ——
    用户点了又被 not_granted 拒（提示能点、点了不行）。
    """
    from core.persona_pool import PersonaPool  # noqa: PLC0415

    p = _tool_plugin()
    umo = "Banyan:GroupMessage:200000001"
    granted, denied = p.personas.all()[0], p.personas.all()[-1]
    assert granted.id != denied.id

    p._persona_pool = PersonaPool(Path(tempfile.mkdtemp()))
    p._persona_pool.set_grants("Banyan", [granted.id])

    # 用被禁人格的显示名去查：它自己的名字必然是最强候选
    msg = p._not_found(denied.display_name, umo)
    cands = msg.split("你是不是想找：", 1)[-1] if "你是不是想找：" in msg else ""
    ids = [c.strip() for c in cands.split("、") if c.strip()]
    assert denied.id not in ids, f"未授权的人格不该出现在候选里：{msg!r}"
    assert all(p._persona_pool.is_available("Banyan", i) for i in ids), \
        f"候选里混进了未授权人格：{ids}"
    # 反向确认：池子放开后，同一个查询会把被禁的那个也列出来
    p._persona_pool.set_grants("Banyan", [denied.id])
    msg2 = p._not_found(denied.display_name, umo)
    assert denied.id in msg2, f"授权后应能作为候选出现：{msg2!r}"


def test_apply_group_to_session_skips_ungranted_persona():
    """组人格未授权给本实例时：不改会话人格、不写切换提醒、不改群名片。

    回归：`_persona_for` 取人格时本来就会过滤，但 `apply_group_to_session`
    以前不拦 —— 会先把群名片改成那个人格名、并给模型写一条"你现在扮演 X"
    的提醒，下一轮又回退到别的人格，表现为「群名片叫 A、说话是 B」。
    """
    from core.persona_pool import PersonaPool  # noqa: PLC0415

    p = _tool_plugin()
    p.apply_group_to_session = Neo.apply_group_to_session.__get__(p)
    umo = "Banyan:GroupMessage:200000001"
    granted, denied = p.personas.all()[0], p.personas.all()[-1]
    p._session_persona.set(umo, granted.id)

    p._persona_pool = PersonaPool(Path(tempfile.mkdtemp()))
    p._persona_pool.set_grants("Banyan", [granted.id])

    cards = []

    async def _card(u, doc, bot=None):
        cards.append(getattr(doc, "id", ""))

    p._set_group_card = _card
    p._persona_switch_notice = {}

    p.groups.create("G-denied", persona=denied.id, instance="Banyan")
    p.groups.assign(umo, "G-denied")
    p.apply_group_to_session(umo)

    assert p._session_persona.get(umo) == granted.id, "未授权时不能改写会话人格"
    assert not p._persona_switch_notice, "未授权时不能写切换提醒"
    assert not cards, f"未授权时不能改群名片，实际写了 {cards}"
    assert p.groups.meta("G-denied").get("instance") == "Banyan"

    # 换成已授权的人格 → 正常应用
    p.groups.create("G-ok", persona=granted.id, instance="Banyan")
    p.groups.assign(umo, "G-ok")
    p._session_persona.set(umo, "")
    p.apply_group_to_session(umo)
    assert p._session_persona.get(umo) == granted.id, "已授权时应正常应用"
