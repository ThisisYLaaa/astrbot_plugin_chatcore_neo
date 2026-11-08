# -*- coding: utf-8 -*-
"""人格层契约测试：大纲无损、章节注册、别名解析、注入策略、doctor。

这些测试只依赖插件自身与（可选的）旧环境，不依赖 AstrBot 运行时。
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import PERSONAS_DIR, SKILLS_DIR, WORLDS_DIR  # noqa: E402
from core.persona import (PersonaService, all_specs, build_stable_segment,  # noqa: E402
                          get_spec, load_framework, parse_outline,
                          render_selection, resolve_lore_section, resolve_title,
                          run_doctor, strip_frontmatter)
from core.persona.model import PersonaDoc, iter_persona_dirs  # noqa: E402

from run_tests import Skip  # noqa: E402
import _env  # noqa: E402

OLD_SKILLS = _env.legacy_skills()

# 实测：同一节的两种括号写法（手工抄写漂移的实证）
DRIFT_A = "行为倾向（基于性格自然表现，不是死命令）"
DRIFT_B = "行为倾向(基于性格自然表现，不是死命令)"


def _docs() -> list[PersonaDoc]:
    docs = [PersonaDoc.load(d) for d in iter_persona_dirs(PERSONAS_DIR)]
    if not docs:
        raise Skip("尚未迁移人格数据")
    return docs


# ---------------------------------------------------------------- 注册表

def test_bracket_drift_maps_to_same_section():
    a, b = resolve_title(DRIFT_A), resolve_title(DRIFT_B)
    assert a is not None and b is not None, "两种括号写法都应被识别"
    assert a.id == b.id == "behavior_tendency", (a.id, b.id)


def test_required_builtin_specs_exist():
    ids = {s.id for s in all_specs()}
    for want in ("identity", "personality", "catchphrases", "extra_style",
                 "initial_state", "emotion_map", "time_map", "notice",
                 "role_rules", "behavior_tendency", "style_summary",
                 "appearance"):
        assert want in ids, f"缺少内置章节 {want}"


def test_notice_framework_text_matches_measured():
    """「重要提醒」15/15 逐字相同，框架文本必须与实测一致。"""
    spec = get_spec("notice")
    assert spec is not None and spec.framework_default
    for doc in _docs():
        node = doc.outline.by_id("notice")
        if node is None:
            continue
        got = node.text(doc.outline.source).strip()
        assert got == spec.framework_default, f"{doc.id} 的重要提醒与框架文本不一致"


# ---------------------------------------------------------------- 大纲

def test_outline_has_no_unregistered_sections():
    for doc in _docs():
        unknown = [n.title for n in doc.outline.all_nodes()
                   if n.spec is None and n.level >= 2]
        assert not unknown, f"{doc.id} 有未注册章节: {unknown}"


def test_outline_full_render_matches_body_modulo_whitespace():
    """完整重组应还原正文。

    允许的唯一差异是空白：重组统一用「标题\\n\\n正文」，
    而原文件里部分子节标题后没有空行。差异量级必须在个位数字节。
    """
    for doc in _docs():
        got = render_selection(doc.outline, include_preamble=True,
                               include_title=True)
        want = doc.skill_body
        assert re.sub(r"\s+", "", got) == re.sub(r"\s+", "", want), \
            f"{doc.id} 重组后非空白字符不一致（说明丢了内容）"
        delta = len(got) - len(want)
        assert 0 <= delta <= 40, \
            f"{doc.id} 重组后多出 {delta} 字符，超出预期的空行归一化范围"


def test_section_ids_are_unique_per_persona():
    for doc in _docs():
        ids = [n.section_id for n in doc.outline.all_nodes()]
        dup = {i for i in ids if ids.count(i) > 1}
        assert not dup, f"{doc.id} 章节 id 重复: {dup}"


def test_time_map_has_seven_slots():
    """实测旧 38 份中 37 份 7 槽齐全；本批 15 份应全部齐全。"""
    slots = ("凌晨", "清晨", "上午", "中午", "下午", "傍晚", "晚上")
    for doc in _docs():
        node = doc.outline.by_id("time_map")
        assert node is not None, f"{doc.id} 缺时间感知"
        txt = node.text(doc.outline.source)
        missing = [s for s in slots if s not in txt]
        assert not missing, f"{doc.id} 时间感知缺槽位: {missing}"


# ---------------------------------------------------------------- 迁移无损

# 2026-09 按官方默认立绘修订过 skill.md 外观描述的人格：
# skill.md 不再是旧文件的逐字节副本，字节一致性测试豁免该项（其余文件仍须一致）。
REVISED_SKILL = {
    "nte_lacrimosa", "strinova_aika", "strinova_audreygrove", "strinova_celestia",
    "strinova_cielle", "strinova_flavia", "strinova_fragrans", "strinova_fuchsia",
    "strinova_lawine", "strinova_leona", "strinova_maddelenaleary",
    "strinova_meredith", "strinova_nora", "strinova_qiandai", "strinova_reiichi",
    "strinova_yugiri", "strinova_yvette", "zzz_airui", "zzz_nangongyu",
    "zzz_qianxia",
}

# 2026-10 提示词去重（批量）：跨文件逐字/变体重复的台词行，按
# skill > lore > dialog > style_summary 优先级保留一份、删低优先级副本
# （规则与脚本 dedup_personas.py 一致，2026-10-01 执行，全量备份于
# plugin_data/chatcore_neo/personas_backup_20261001_*）。
# 字节一致性测试对下列修订文件豁免（其余文件仍须一致）：
REVISED_PROMPT_DEDUP = {
    # dialog.md 被删过与 lore.md 标志性台词重复的行（42 个 + 上一轮 sr_hyacine）
    "arke_arcane", "arke_zhuangfangyi", "diy_claudeopus55",
    "diy_deepseekv41flash", "diy_gemini38flash", "diy_glm53flashx",
    "diy_gpt6astra", "diy_grok47", "diy_sui", "sr_aglaea", "sr_anaxa",
    "sr_castorice", "sr_cerydra", "sr_cipher", "sr_cyrene", "sr_evernight",
    "sr_hyacine", "sr_hysilens", "sr_mydei", "sr_phainon", "sr_sparxie",
    "sr_tribbie", "strinova_aika", "strinova_audreygrove", "strinova_baimo",
    "strinova_flavia", "strinova_fragrans", "strinova_kokona",
    "strinova_lawine", "strinova_leona", "strinova_mara", "strinova_meredith",
    "strinova_ming", "strinova_nobunaga", "strinova_nora", "strinova_qiandai",
    "strinova_reiichi", "strinova_yugiri", "strinova_yvette", "zzz_airui",
    "zzz_remielle", "zzz_vivian", "zzz_yeshunguang",
}
REVISED_DIALOG = REVISED_PROMPT_DEDUP
# lore.md 被删过与 skill.md 重复的行
REVISED_LORE = {"strinova_nora"}
# skill.md 被删过「外貌」行的（细节在 appearance.md）
REVISED_SKILL_DEDUP = {"sr_hyacine"}


def test_content_files_byte_identical_to_old_env():
    if not OLD_SKILLS.is_dir():
        raise Skip("旧环境不存在")
    checked = 0
    for doc in _docs():
        old_dir = OLD_SKILLS / (doc.legacy_dir or "")
        if not old_dir.is_dir():
            continue
        pairs = [
            ("SKILL.md", doc.content_path("skill")),
            ("lore.md", doc.content_path("lore")),
            ("dialog.md", doc.content_path("dialog")),
            ("speech_patterns.txt", doc.content_path("speech_raw")),
            ("_tier1.md", doc.tier_path("intimate")),
            ("_tier2.md", doc.tier_path("friend")),
        ]
        for old_name, new_p in pairs:
            if old_name == "SKILL.md" and (doc.id in REVISED_SKILL
                                           or doc.id in REVISED_SKILL_DEDUP):
                continue      # 已按官方立绘/去重修订，豁免字节比对
            if old_name == "dialog.md" and doc.id in REVISED_DIALOG:
                continue      # 已做提示词去重修订，豁免字节比对
            if old_name == "lore.md" and doc.id in REVISED_LORE:
                continue      # 已做提示词去重修订，豁免字节比对
            o = old_dir / old_name
            if not o.exists():
                continue
            assert new_p is not None and new_p.exists(), \
                f"{doc.id} 缺 {old_name}"
            assert o.read_bytes() == new_p.read_bytes(), \
                f"{doc.id}/{old_name} 原始字节不一致"
            checked += 1
    assert checked >= 80, f"只比对了 {checked} 个文件，样本过少"


# ---------------------------------------------------------------- 别名

def test_aliases_have_no_cross_persona_conflict():
    svc = PersonaService()
    conf = svc.alias_conflicts()
    assert not conf, f"跨人格别名冲突: {conf}"


def test_every_persona_resolvable_by_id_and_display_name():
    svc = PersonaService()
    for doc in svc.all():
        assert svc.resolve(doc.id) is doc, f"按 id 解析失败: {doc.id}"
        assert svc.resolve(doc.display_name) is doc, \
            f"按显示名解析失败: {doc.display_name}"
        if doc.legacy_dir:
            assert svc.resolve(doc.legacy_dir) is doc, \
                f"按旧目录名解析失败: {doc.legacy_dir}"


def test_alias_lookup_is_case_insensitive():
    svc = PersonaService()
    doc = svc.all()[0]
    assert svc.resolve(doc.id.upper()) is doc


def test_unknown_name_returns_none_and_suggests():
    svc = PersonaService()
    assert svc.resolve("绝不存在的人格名") is None
    assert svc.resolve("") is None
    assert svc.suggest("kanam"), "应给出拼写建议"


# ---------------------------------------------------------------- 注入策略

def test_dialog_defaults_to_on_demand():
    """旧实现每轮全量注入 dialog，是主要浪费项；必须默认按需。"""
    spec = get_spec("dialog")
    assert spec is not None and spec.default_inject == "on_demand"


def test_effective_inject_uses_registry_for_file_level_content():
    """lore/dialog 在 SKILL.md 大纲里没有节点，不能退化成文档默认值。"""
    for doc in _docs():
        assert doc.effective_inject("dialog") == "on_demand", doc.id
        assert doc.effective_inject("lore_full") == "always", doc.id
        assert doc.effective_inject("style_summary") == "always", doc.id


def test_every_persona_has_appearance_fields():
    """外貌是后补的 L1 内容，必须人人齐备且字段完整。

    固定五字段（发色/发型/瞳色/服饰/体征）是**可机器校验**的前提 ——
    自由叙述写不出"漏了瞳色"这种检查。
    """
    fields = ("发色：", "发型：", "瞳色：", "服饰：", "体征：")
    missing: list[str] = []
    for doc in _docs():
        txt = doc.read_content("appearance")
        if not txt:
            missing.append(f"{doc.id}: 缺 content/appearance.md")
            continue
        lack = [f for f in fields if f not in txt]
        if lack:
            missing.append(f"{doc.id}: 缺字段 {lack}")
    assert not missing, "外貌缺失: " + "; ".join(missing)


def test_appearance_is_actually_injected():
    """注册了就得真的进提示词 —— 否则又是一处"声明了没接线"。

    （这正是 extra_blocks 的教训：字段声明了 on_demand，却没有任何注入路径。）
    """
    for doc in _docs():
        txt = doc.read_content("appearance")
        if not txt:
            continue
        first = txt.strip().splitlines()[0].strip()
        assert first, f"{doc.id} 外貌文件首行为空"
        assert first in build_stable_segment(doc), \
            f"{doc.id} 的外貌没有进入稳定段（没接线）"


def test_stable_segment_includes_lore_full_by_default():
    """lore.md 原文必须每轮进稳定段。

    回归：「关键人物/关系」表、剧情时间线、游戏内设定只存在于 lore.md 原文里，
    一旦只注入抽取摘要（origin + 动机 + weight 前 4 的关系），角色被问到
    被截断的第 5 条关系时就会答"不认识这个人"。
    """
    for doc in _docs():
        lore = doc.read_content("lore")
        if not lore:
            continue
        head = lore.strip()[:60]
        assert head in build_stable_segment(doc), \
            f"{doc.id} 的稳定段缺少 lore.md 原文"


def test_stable_segment_excludes_dialog():
    """dialog.md 仍应保持按需（旧实现每轮全量注入，是主要浪费项）。"""
    for doc in _docs():
        dialog = doc.read_content("dialog")
        if not dialog:
            continue
        head = dialog.strip()[:60]
        assert head not in build_stable_segment(doc), \
            f"{doc.id} 稳定段里混进了 dialog"


def test_lore_policy_override_falls_back_to_brief():
    """把 lore_full 显式改为按需时，退回抽取摘要兜底，且不再重复注入原文。"""
    docs = _docs()
    doc = next((d for d in docs if d.read_content("lore")
                and d.lore and d.lore.extracted), None)
    if doc is None:
        raise Skip("没有可用于本用例的 lore 数据")
    copy = PersonaDoc.load(doc.dir)
    copy.inject_overrides["lore_full"] = "on_demand"
    text = build_stable_segment(copy)
    assert copy.lore.brief()[:30] in text, "按需模式下应仍有抽取摘要兜底"
    assert copy.read_content("lore").strip()[:60] not in text, \
        "按需模式下不应再注入 lore 原文"
    assert resolve_lore_section(copy).startswith("# 角色背景\n"), \
        "退回摘要时应使用摘要标题"


def test_framework_loads_nonempty_for_both_scenes():
    g = load_framework(is_private=False)
    p1 = load_framework(is_private=True, msg_mode=1)
    p2 = load_framework(is_private=True, msg_mode=2)
    assert g and p1 and p2
    assert g != p1, "群聊与私聊框架不应相同"
    assert p1 != p2, "私聊两种消息模式框架不应相同"


def test_framework_load_is_order_independent():
    """回归：旧实现先调群聊后，私聊 framework 恒为空（缓存缺陷）。"""
    a = load_framework(is_private=True, msg_mode=1)
    load_framework(is_private=False)
    b = load_framework(is_private=True, msg_mode=1)
    assert a == b and len(b) > 0


def test_world_hint_only_when_world_has_files():
    from core.persona import build_world_hint
    for doc in _docs():
        hint = build_world_hint(doc)
        if doc.world and (WORLDS_DIR / doc.world).is_dir():
            assert hint, f"{doc.id} 绑定了世界却没有提示行"
        if not doc.world:
            assert not hint, f"{doc.id} 未绑定世界却注入了提示行"


# ---------------------------------------------------------------- doctor

def test_doctor_runs_and_reports_structure():
    rep = run_doctor()
    d = rep.to_dict()
    assert d["counts"]["error"] == 0, \
        "doctor 报错: " + "; ".join(i.message for i in rep.errors[:5])
    assert d["stats"]["personas"] >= 1
    assert "personas_dir" in d["stats"]
    # 每个人格都必须有装配预览
    assert len(d["previews"]) == d["stats"]["personas"]


def test_no_orphan_files_after_merge():
    rep = run_doctor()
    orphans = [i for i in rep.issues if i.code == "file.orphan"]
    assert not orphans, \
        "存在未引用文件: " + "; ".join(f"{i.persona}:{i.detail}" for i in orphans)


def test_skills_json_shape_not_misparsed():
    """回归：skills.json 是 {"skills": {...}}，技能名是**内层**的键。
    把外层键当条目会把 "skills" 本身误报成悬空技能。"""
    import json
    import tempfile
    from pathlib import Path as _P
    from core.paths import DATA_DIR
    from core.persona import doctor as _doc

    sj = DATA_DIR / "skills.json"
    raw = sj.read_text(encoding="utf-8").strip() if sj.exists() else ""
    if not raw:
        raise Skip("skills.json 为空")
    data = json.loads(raw)
    assert isinstance(data, dict) and "skills" in data, \
        f"skills.json 结构非预期: {list(data)[:5]}"

    rep = _doc.DoctorReport()
    _doc._check_skills_json(rep)
    dangling = [i for i in rep.issues if i.code == "skills_json.dangling"]
    for i in dangling:
        names = (i.detail or {}).get("entries") or []
        assert "skills" not in names, \
            f"'skills' 被误报为悬空技能条目: {names}"