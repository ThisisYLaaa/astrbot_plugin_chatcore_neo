# -*- coding: utf-8 -*-
"""跨会话记忆组测试。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.groups import MemoryGroups  # noqa: E402


def _mk(data: dict | None = None) -> MemoryGroups:
    td = Path(tempfile.mkdtemp())
    p = td / "groups.json"
    if data is not None:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return MemoryGroups(p)


def test_ungrouped_session_keys_to_itself():
    g = _mk()
    assert g.group_of("Banyan:GroupMessage:1") == "Banyan:GroupMessage:1"
    assert g.stats()["groups"] == 0


def test_load_existing_groups():
    g = _mk({"groups": {"G1": ["a", "b"], "G2": ["c"]}})
    assert g.group_of("a") == "G1"
    assert g.group_of("b") == "G1"
    assert g.group_of("c") == "G2"
    assert g.group_of("d") == "d"
    assert g.members("G1") == ["a", "b"]
    assert g.stats() == {"groups": 2, "members": 3, "path": str(g.path)}


def test_assign_moves_between_groups():
    g = _mk({"groups": {"G1": ["BotA:GroupMessage:1"]}})
    g.assign("BotA:GroupMessage:1", "G2")
    assert g.group_of("BotA:GroupMessage:1") == "G2"
    # v2 起「组」是带人格/副人格/实例的配置对象：最后一个人退出后组本身保留
    # （管理页把这种组渲染成「（空组）」），否则成员一走光人设配置就跟着没了。
    # 要清掉空组得显式调 drop_empty()，assign/remove 不再顺手删组。
    assert g.members("G1") == []
    assert "G1" in g.all_groups(), "空组应保留"
    assert g.drop_empty() == 1, "drop_empty 是清理空组的唯一入口"
    assert "G1" not in g.all_groups()


def test_assign_new_and_idempotent():
    g = _mk()
    g.assign("x", "GN")
    g.assign("x", "GN")
    assert g.members("GN") == ["x"], "重复归组不该产生重复项"


def test_assign_empty_group_removes_membership():
    g = _mk({"groups": {"G1": ["BotA:GroupMessage:1"]}})
    # v2 里组名是必填的：空串一律当调用方漏传（webadmin.create_group 同样口径），
    # 所以「退出组」要走 remove()（/groups/remove），不能靠 assign(umo, "")。
    try:
        g.assign("BotA:GroupMessage:1", "")
    except ValueError as e:
        assert "组名不能为空" in str(e), e
    else:
        raise AssertionError("空组名应被拒绝")
    assert g.group_of("BotA:GroupMessage:1") == "G1", "拒绝后不该动成员关系"
    g.remove("BotA:GroupMessage:1")
    assert g.group_of("BotA:GroupMessage:1") == "BotA:GroupMessage:1"


def test_remove():
    g = _mk({"groups": {"G1": ["BotA:GroupMessage:1", "BotA:GroupMessage:2"]}})
    g.remove("BotA:GroupMessage:1")
    assert g.members("G1") == ["BotA:GroupMessage:2"]
    g.remove("BotA:GroupMessage:2")
    # 与 assign 同一条 v2 语义：成员走光后组仍保留（组上的人设/实例配置不能丢），
    # 空组的清理是显式维护动作 drop_empty()。
    assert g.members("G1") == []
    assert "G1" in g.all_groups(), "空组应保留"
    assert g.drop_empty() == 1
    assert g.all_groups() == {}


def test_merge_creates_shared_group():
    """`.merge(umoA, umoB)` 已被删掉：合并 = 把同一实例的一批会话 assign 到同一个组。

    旧 API 让 MemoryGroups 自己造组名，与 v2「组是带人格/实例的具名配置对象」
    冲突；现在组名由调用方给（管理页 `/groups/assign` 传 umos + group）。
    """
    g = _mk()
    name = "G"
    added = g.assign_many(["BotA:GroupMessage:1", "BotA:GroupMessage:2"], name)
    assert added == ["BotA:GroupMessage:1", "BotA:GroupMessage:2"]
    assert g.group_of("BotA:GroupMessage:1") == name
    assert g.group_of("BotA:GroupMessage:2") == name
    assert set(g.members(name)) == {"BotA:GroupMessage:1", "BotA:GroupMessage:2"}


def test_merge_absorbs_existing_group():
    """「并入已有组」在 v2 有两条路径，取代了旧的 `merge(umo, umo)`。

    * 会话并入组：`assign` / `assign_many` 指定已存在的组名；
    * 整组并入组：`rename` 撞上已存在的组名（webadmin.rename_group 的文档口径
      "目标组已存在时等价于并入"）。
    """
    g = _mk({"version": 2, "groups": {
        "G1": {"persona": "", "costars": [],
               "members": ["BotA:GroupMessage:1", "BotA:GroupMessage:2"],
               "instance": "BotA"},
        "G2": {"persona": "strinova_kanami", "costars": [],
               "members": ["BotA:GroupMessage:3"], "instance": "BotA"},
    }})
    # ① 会话并入已有组：落在 G1 上，不新建组
    g.assign("BotA:GroupMessage:3", "G1")
    assert g.group_of("BotA:GroupMessage:3") == "G1"
    assert set(g.members("G1")) == {"BotA:GroupMessage:1",
                                    "BotA:GroupMessage:2",
                                    "BotA:GroupMessage:3"}
    # ② 整组并入：G2 → G1，成员全并过来、源组消失
    g.assign("BotA:GroupMessage:4", "G2")
    merged = g.rename("G2", "G1")
    assert merged == "G1"
    assert "G2" not in g.names()
    assert set(g.members("G1")) == {"BotA:GroupMessage:1", "BotA:GroupMessage:2",
                                    "BotA:GroupMessage:3", "BotA:GroupMessage:4"}
    # 目标组本来没设人格，合并时源组的人格补过来
    assert g.persona_of("G1") == "strinova_kanami"


def test_rename_rejects_cross_instance_merge():
    """跨实例把两个组合并 = 违反「一组一实例」，必须拒绝且两边数据原封不动。

    最容易写错的点：校验必须在 `pop(old)` **之前**，否则抛错时源组已经被删掉，
    用户会看到「报错了但组没了」。这里用 all_groups() 快照做全量对比。
    """
    g = _mk({"version": 2, "groups": {
        "GA": {"persona": "sr_cyrene", "costars": ["strinova_kanami"],
               "members": ["BotA:GroupMessage:1"], "instance": "BotA"},
        "GB": {"persona": "diy_sui", "costars": ["sr_robin_qingge"],
               "members": ["BotB:GroupMessage:2"], "instance": "BotB"},
    }})
    before = g.all_groups()          # meta() 返回副本，快照不会被后续改动带着变
    try:
        g.rename("GB", "GA")
    except ValueError as e:
        msg = str(e)
        assert "跨实例" in msg and "BotB" in msg and "BotA" in msg, msg
    else:
        raise AssertionError("跨实例合并应被拒绝")
    assert g.all_groups() == before, "拒绝后两组必须原封不动（pop 不能发生在校验前）"
    assert MemoryGroups(g.path).all_groups() == before, "拒绝后磁盘上也不该被改写"
    assert g.members("GA") == ["BotA:GroupMessage:1"]
    assert g.members("GB") == ["BotB:GroupMessage:2"]
    assert g.meta("GA")["instance"] == "BotA"
    assert g.meta("GB")["instance"] == "BotB"


def test_rename_merges_costars_without_dupes():
    """同实例合并要把源组的副人格并进目标组（去重），不能再静默丢掉。"""
    g = _mk({"version": 2, "groups": {
        "G1": {"persona": "", "costars": ["strinova_kanami"],
               "members": ["BotA:GroupMessage:1"], "instance": "BotA"},
        "G2": {"persona": "", "costars": ["strinova_kanami", "sr_robin_qingge"],
               "members": ["BotA:GroupMessage:2"], "instance": "BotA"},
    }})
    g.rename("G2", "G1")
    cs = g.costars_of("G1")
    assert cs == ["strinova_kanami", "sr_robin_qingge"], cs
    assert len(cs) == len(set(cs)), "副人格不该出现重复项"
    assert set(g.members("G1")) == {"BotA:GroupMessage:1", "BotA:GroupMessage:2"}


def test_rename_inherits_instance_from_source():
    """目标组没声明实例、源组能推出实例时，合并后目标组继承源组实例。

    源组的 instance 留空、靠成员 UMO 前缀推（`_instance_of_meta` 的兜底分支）；
    否则合并出来的组实例为空，后续 assign 又会把它当「无主组」。
    """
    g = _mk({"version": 2, "groups": {
        "G1": {"persona": "", "costars": [], "members": [], "instance": ""},
        "G2": {"persona": "", "costars": [],
               "members": ["BotA:GroupMessage:2"], "instance": ""},
    }})
    g.rename("G2", "G1")
    assert g.meta("G1")["instance"] == "BotA"
    assert g.members("G1") == ["BotA:GroupMessage:2"]


def test_persistence_roundtrip():
    g = _mk()
    # umo 必须是 `实例:类型:id`：v2 用第一段判实例，裸 "a"/"b" 会被当成两个不同
    # 实例，第二个 assign 就会因「跨实例不允许共享记忆」被拒。
    g.assign("BotA:GroupMessage:1", "G")
    g.assign("BotA:GroupMessage:2", "G")
    g2 = MemoryGroups(g.path)
    assert set(g2.members("G")) == {"BotA:GroupMessage:1", "BotA:GroupMessage:2"}
    assert g2.group_of("BotA:GroupMessage:1") == "G"
    assert g2.meta("G")["instance"] == "BotA", "实例归属要一起落盘"


def test_corrupt_file_starts_empty():
    td = Path(tempfile.mkdtemp())
    p = td / "groups.json"
    p.write_text("{不是 JSON", encoding="utf-8")
    g = MemoryGroups(p)
    assert g.all_groups() == {}


def test_wrong_shape_starts_empty():
    g = _mk({"groups": "这不是字典"})
    assert g.all_groups() == {}
    g2 = _mk({"other": 1})
    assert g2.all_groups() == {}


def test_real_migrated_file_if_present():
    """迁移过来的真实 groups.json 应能被正确读取。"""
    from core.paths import GROUPS_JSON
    if not GROUPS_JSON.exists():
        from run_tests import Skip
        raise Skip("未迁移 groups.json")
    g = MemoryGroups()
    st = g.stats()
    assert st["groups"] >= 1, st
    # v2 的组值是 dict（persona/costars/members/instance/updated_at），成员在
    # meta["members"] 里；旧测试把组值当成员列表直接迭代，取到的是 "persona" 这个键名。
    groups = g.all_groups()
    members = [u for meta in groups.values() for u in meta["members"]]
    assert members, "迁移文件里应至少有一个入组会话"
    any_member = members[0]
    assert g.group_of(any_member) in groups
    # 迁移产物还得满足 v2 的「一组一实例」：每个成员都落在该组声明的实例上
    for name, meta in groups.items():
        assert meta["instance"], f"组 {name} 缺实例"
        assert all(g.inst_of(u) == meta["instance"] for u in meta["members"]), name

# ────────────────────────────── 记忆组近况：uid 归属（2026-10-29 回归）

def test_journal_matches_across_sessions_by_uid():
    """同一 uid 在**别的会话**里说过的话，能在本会话被读到；空 uid 一律不匹配。

    `GroupJournal.recent()` 有一道 `if not uid: return []` 的硬守卫 ——
    所以调用方必须传「真实发送者 id」。私聊里如果把被特权逻辑置空的 uid
    传进来，就会永远查不到，表现为"在群里刚聊完，私聊里 bot 完全不知道"。
    """
    from core.group_journal import GroupJournal  # noqa: PLC0415

    j = GroupJournal(Path(tempfile.mkdtemp()) / "_recent.jsonl")
    j.add("G", "BotA:GroupMessage:2", "777", "在群里说的", "嗯，记住了")

    hits = j.recent("G", "777", exclude_umo="BotA:FriendMessage:1")
    assert len(hits) == 1 and hits[0]["q"] == "在群里说的"
    # 排除自己所在会话
    assert j.recent("G", "777", exclude_umo="BotA:GroupMessage:2") == []
    # 空 uid：永远查不到（这条守卫就是私聊 bug 的根因，别被人"顺手放宽"）
    assert j.recent("G", "", exclude_umo="BotA:FriendMessage:1") == []


def test_memory_uid_is_not_blanked_in_private_chat():
    """静态守卫：记忆/近况必须用 `mem_uid`，不能用被置空的 `uid`。

    回归 2026-10-29：私聊里 `uid` 被强制置空（特权判定用），
    结果 `_group_recent_block` 与 `_remember` 也拿到空 uid ——
    记忆组近况在私聊侧彻底失效，而且私聊写入的记忆 uid 为空，
    会让别人的群聊召回把它们带出来（跨用户泄漏）。
    """
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert 'mem_uid = (self._sender_id(event) or "") if is_private else uid' in src, \
        "私聊必须单独算一个记忆归属 uid"
    assert "_group_recent_block(umo, mem_uid)" in src, "近况读取不能用置空的 uid"
    assert "umo, pid, mem_uid, user_msg" in src, "记忆写入不能用置空的 uid"
    assert "persona=doc.id, uid=mem_uid" in src, "记忆召回不能用置空的 uid"

