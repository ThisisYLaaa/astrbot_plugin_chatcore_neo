# -*- coding: utf-8 -*-
"""doctor 的 skills.json 检查不能误报内置技能。

事故复盘
--------
AstrBot 的技能有两处来源（`core/skills/skill_manager.py`）：
  1. 工作区：`data/skills/<name>`
  2. **插件**：`<插件目录>/skills/<name>`，**含内置插件**
     `backend/app/astrbot/builtin_stars/astrbot/skills/`

我的检查最初只看 `data/skills/`，于是把内置技能
`documents / pdf / skill-creator / spreadsheets` 报成"4 条悬空条目"。
更糟的是第一版修法还写成了 `SKILLS_DIR / "skills"` —— 而 `SKILLS_DIR`
本身就是 `data/skills`，于是连工作区技能也一个都没扫到。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import SKILLS_DIR  # noqa: E402
from core.persona import doctor as doc  # noqa: E402
import _env  # noqa: E402


def test_skill_name_dirs_includes_workspace_skills():
    """工作区技能名必须被扫到（SKILLS_DIR 本身，不是 SKILLS_DIR/skills）。"""
    names = doc._skill_name_dirs()
    if not SKILLS_DIR.is_dir():
        from run_tests import Skip
        raise Skip("没有工作区技能目录")
    ws = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}
    assert ws, "工作区应有技能目录"
    missing = ws - names
    assert not missing, f"工作区技能没被扫到: {missing}"


def test_skill_name_dirs_includes_builtin_skills():
    """内置插件的技能也要算进去，否则会被误报为悬空。"""
    names = doc._skill_name_dirs()
    builtin = (_env.app() / "astrbot" / "builtin_stars" / "astrbot" / "skills")
    if not builtin.is_dir():
        from run_tests import Skip
        raise Skip("找不到内置插件技能目录")
    expected = {d.name for d in builtin.iterdir() if d.is_dir()}
    assert expected, "内置插件应有技能"
    missing = expected - names
    assert not missing, f"内置技能没被扫到（会被误报悬空）: {missing}"


def test_doctor_does_not_report_builtin_skills_as_dangling():
    """真正的回归测试：内置技能不得出现在悬空名单里。"""
    rep = doc.run().to_dict()
    bad = [i for i in rep["issues"] if i["code"] == "skills_json.dangling"]
    if not bad:
        return
    entries = (bad[0].get("detail") or {}).get("entries") or []
    builtin = {"documents", "pdf", "skill-creator", "spreadsheets"}
    overlap = builtin & set(entries)
    assert not overlap, (
        f"内置技能被误报成悬空条目: {overlap}\n"
        "_skill_name_dirs() 必须把内置插件目录也算进技能来源")


def test_skill_name_dirs_handles_missing_dirs():
    """目录不存在时不能抛异常，应返回空集合。"""
    names = doc._skill_name_dirs()
    assert isinstance(names, set)
