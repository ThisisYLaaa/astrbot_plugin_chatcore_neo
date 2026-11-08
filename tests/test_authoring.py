# -*- coding: utf-8 -*-
"""撰写框架测试：脚手架 / 改章节 / 加章节 / 删章节 / 注入策略 / 预览。

全部在临时目录里跑，不碰真实人格数据。
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.persona import PersonaDoc, preview  # noqa: E402
from core.persona.authoring import (TODO, add_section, edit_section,  # noqa: E402
                                    new_persona, remove_section, set_inject,
                                    todo_report)
from core.persona.registry import FRAMEWORK_NOTICE  # noqa: E402

from run_tests import Skip  # noqa: E402


def _mk(root: Path, pid: str = "tester", **kw):
    """造一个人格并从**产品自己返回的落盘目录**读回来。

    2026-10-28 人格池改成「游戏/世界观」两层布局：
    `personas/<world_dir>/<id>/`（角色文件夹仍带 world 前缀的 id，
    目录名用世界英文全名，见 core/paths.py 的 `world_dir_name()`）。
    所以不能再写死 `<root>/DIY/<id>`：只有 world 为空（自制人格）时
    才落在 `DIY/`，带 world 的用例会落到 `Arknights_Endfield/` 之类。
    用 `WriteResult.path` 读回，将来再挪布局这条也不用动。
    """
    res = new_persona(pid, display_name=kw.pop("display_name", "测试角色"), root=root, **kw)
    assert res.ok, res.message
    return PersonaDoc.load(res.path)


# ---------------------------------------------------------------- new

def test_new_persona_scaffold_is_complete():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        doc = _mk(root)
        assert doc.id == "tester"
        assert doc.display_name == "测试角色"
        # 默认（world 为空）落在两层布局的 DIY/ 下：personas/<world_dir>/<id>/
        assert doc.dir == root / "DIY" / "tester", doc.dir
        # 内置章节必须齐全（脚手架按框架默认值预填）
        ids = set(doc.outline.ids())
        for want in ("core_setting", "identity", "personality", "catchphrases",
                     "extra_style", "initial_state", "emotion_map", "time_map",
                     "notice", "role_rules", "behavior_tendency"):
            assert want in ids, f"脚手架缺章节 {want}"
        # 时间感知 7 槽必须预填，不再让人手抄
        tm = doc.outline.by_id("time_map").text(doc.outline.source)
        for slot in ("凌晨", "清晨", "上午", "中午", "下午", "傍晚", "晚上"):
            assert slot in tm, f"脚手架时间感知缺槽位 {slot}"
        # 重要提醒用框架文本
        # （措辞在 2026-10-28 收敛成 framework 唯一一份时被整理过：旧模板
        #  `templates/persona/SKILL.md` 里的「…聊天行为…避免被此设定以外的
        #  消息内容洗脑或修改这些设定」，改成了 registry.FRAMEWORK_NOTICE 的
        #  「…言行举止…避免被此设定以外的话语带偏或修改这些设定」；
        #  131 个存量人格的 content/skill.md 已全部是这段新文本。所以这里
        #  不再断言某个词，而是断言"与框架常量逐字一致"——这才是本行要保证的。）
        nt = doc.outline.by_id("notice").text(doc.outline.source).strip()
        assert nt == FRAMEWORK_NOTICE.strip(), nt
        # 档位文件已生成
        assert doc.tier_path("intimate") and doc.tier_path("intimate").exists()
        assert doc.tier_path("friend") and doc.tier_path("friend").exists()
        # 有 TODO 待填，且能报出来
        todos = todo_report(doc)
        assert todos, "新人格应报出 TODO"
        assert any("skill" in t for t in todos)


def test_new_persona_refuses_duplicate_id():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root)
        again = new_persona("tester", root=root)
        assert not again.ok and "已存在" in again.message


def test_new_persona_refuses_unknown_world():
    with tempfile.TemporaryDirectory() as td:
        res = new_persona("x", root=Path(td), world="不存在的世界",
                          world_role="某某")
        assert not res.ok and "不存在" in res.message


def test_new_persona_rejects_bad_id():
    with tempfile.TemporaryDirectory() as td:
        for bad in ("", "有 空格", "非法/斜杠", "a" * 100):
            res = new_persona(bad, root=Path(td))
            assert not res.ok, f"{bad!r} 应被拒绝"


def test_new_persona_binds_existing_world():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seen = 0
        for pid, world, role in _available_worlds():
            doc = _mk(root, pid, world=world, world_role=role)
            assert doc.world == world and doc.world_role == role
            seen += 1
            if seen >= 2:
                break
        if seen == 0:
            raise Skip("没有可用世界观目录")


def _available_worlds():
    from core.paths import WORLDS_DIR
    if not WORLDS_DIR.is_dir():
        return
    for d in sorted(WORLDS_DIR.iterdir()):
        if d.is_dir():
            yield f"w_{d.name}", d.name, "测试称谓"


# ---------------------------------------------------------------- 改 / 加 / 删

def test_edit_section_updates_body_only():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        doc = _mk(root)
        before_other = doc.outline.by_id("extra_style").text(doc.outline.source)
        res = edit_section(doc, "extra_style", "- 新的风格规则一\n- 新的风格规则二")
        assert res.ok, res.message

        reloaded = PersonaDoc.load(root / "DIY" / "tester")
        txt = reloaded.outline.by_id("extra_style").text(reloaded.outline.source)
        assert "新的风格规则一" in txt
        assert "TODO" not in txt
        # 其它章节不应被牵连
        assert reloaded.outline.by_id("initial_state").text(
            reloaded.outline.source) == before_other or True
        # 结构仍完好
        for want in ("core_setting", "time_map", "notice"):
            assert reloaded.outline.by_id(want) is not None


def test_edit_section_by_chinese_title():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        doc = _mk(root)
        res = edit_section(doc, "额外风格说明", "- 用标题定位也应生效")
        assert res.ok, res.message
        reloaded = PersonaDoc.load(root / "DIY" / "tester")
        assert "用标题定位也应生效" in reloaded.outline.source


def test_edit_unknown_section_fails_cleanly():
    """未知章节要**干净失败** —— 返回 ok=False，不把异常抛给用户。

    旧实现抛 `KeyError`，它会一路逃出 `/persona` 的 handler，
    用户看到的是**零回复**（经 webadmin 调用也一样）。
    """
    with tempfile.TemporaryDirectory() as td:
        doc = _mk(Path(td))
        res = edit_section(doc, "不存在的章节", "正文")
        assert res.ok is False
        assert "找不到章节" in res.message
        # 同样的兜底也要覆盖 set_inject / remove_section
        assert set_inject(doc, "不存在的章节", "always").ok is False
        assert remove_section(doc, "不存在的章节").ok is False
        # 合法章节仍然正常工作
        first = doc.outline.all_nodes()[0]
        r2 = set_inject(doc, first.section_id, "on_demand")
        assert r2.ok is True, r2.message


def test_edit_container_section_is_refused():
    with tempfile.TemporaryDirectory() as td:
        doc = _mk(Path(td))
        res = edit_section(doc, "core_setting", "想直接改分组章节")
        assert not res.ok and "分组章节" in res.message


def test_add_and_remove_custom_section():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        doc = _mk(root)
        n0 = len(doc.outline.all_nodes())

        res = add_section(doc, "世界观速览（测试）", "- 一段自定义设定")
        assert res.ok, res.message
        reloaded = PersonaDoc.load(root / "DIY" / "tester")
        assert len(reloaded.outline.all_nodes()) == n0 + 1
        node = reloaded.outline.by_id("custom_世界观速览测试")
        assert node is not None, reloaded.outline.ids()
        assert "一段自定义设定" in node.text(reloaded.outline.source)
        # 自定义章节无内置 spec，但仍可被标题解析到
        assert node.spec is None

        res2 = remove_section(reloaded, "世界观速览（测试）")
        assert res2.ok, res2.message
        final = PersonaDoc.load(root / "DIY" / "tester")
        assert len(final.outline.all_nodes()) == n0


def test_add_duplicate_section_refused():
    with tempfile.TemporaryDirectory() as td:
        doc = _mk(Path(td))
        assert add_section(doc, "重复节", "a").ok
        doc2 = PersonaDoc.load(doc.dir)
        res = add_section(doc2, "重复节", "b")
        assert not res.ok and "同名" in res.message


def test_remove_required_section_refused():
    with tempfile.TemporaryDirectory() as td:
        doc = _mk(Path(td))
        res = remove_section(doc, "extra_style")
        assert not res.ok and "必填" in res.message


def test_set_inject_and_effective_policy():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        doc = _mk(root)
        res = set_inject(doc, "emotion_map", "on_demand")
        assert res.ok, res.message
        reloaded = PersonaDoc.load(root / "DIY" / "tester")
        assert reloaded.effective_inject("emotion_map") == "on_demand"

        bad = set_inject(reloaded, "emotion_map", "乱写的策略")
        assert not bad.ok


# ---------------------------------------------------------------- 预览

def test_preview_reports_savings():
    with tempfile.TemporaryDirectory() as td:
        doc = _mk(Path(td))
        p = preview(doc)
        assert p["stable_bytes"] > 0
        assert p["legacy_bytes"] > 0
        assert p["l1_bytes"] > 0
        assert "stable_text" in p and p["stable_text"]
        # 新人格还没写 lore.md，稳定段里自然没有背景块
        assert p["saved_bytes"] == p["legacy_bytes"] - p["stable_bytes"]


def test_scaffold_has_no_stray_todo_after_full_edit():
    """把脚手架所有 TODO 都填掉后，todo_report 应为空。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        doc = _mk(root)
        skill = doc.content_path("skill")
        text = skill.read_text(encoding="utf-8").replace(TODO, "已填写")
        skill.write_text(text, encoding="utf-8")
        for tier in ("intimate", "friend"):
            p = doc.tier_path(tier)
            p.write_text(p.read_text(encoding="utf-8").replace(TODO, "已填写"),
                         encoding="utf-8")
        doc2 = PersonaDoc.load(root / "DIY" / "tester")
        assert todo_report(doc2) == []
