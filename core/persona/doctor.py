# -*- coding: utf-8 -*-
"""`doctor` —— 把"看着非常的糟糕"变成可执行的检查项。

设计约束
--------
- **只报告，不修改**。doctor 永不写人格文件。
- **不阻断**。单个坏人格不应让整棵树读不出来，因此每项检查都各自兜异常。
- **估计注入预算**：直接调真实装配函数量字节，而不是另算一套公式
  （两套算法必然漂移）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import (DATA_DIR, PERSONAS_DIR, SKILLS_DIR, WORLDS_DIR,
                     is_migrated)
from .model import KNOWN_TIERS, PersonaDoc, iter_persona_dirs
from .registry import all_specs, get_spec
from . import render as R

# 每轮稳定段预算（字节），**只算人格自有内容**（技能正文 + 风格摘要）。
#
# 框架块（_pfm_framework 的规则文本）是所有人格共用的固定开销，与人格无关，
# 因此分开计量。2026-09-17 slice 化重构后实测：
#   群聊纯信息 ~5.8 KB / 私聊纯信息 ~6.5 KB / 私聊剧情向 ~7.7 KB
#   （叠加会话级露骨规范再 +1.9 KB）
# 重构前同一口径是 ~12.6 KB（群聊 7.8 KB），主要吃在"公共段被复制进多份
# rules_*.md"和"格式规范里的大段示例"上。
#   框架块 ~6~8 KB（固定）+ 人格自有 ~6 KB（本预算）= 每轮约 12~14 KB。
#
# 阈值依据实测：15 份 SKILL.md 正文 4.7~9.7 KB，风格摘要 0.9~1.3 KB，
# 故人格自有内容实际落在 5.6~11 KB。取 warn=9.5 KB / error=13 KB：
# 既不冤枉现有最大的 deepseekv4（9.7 KB 正文），又能在有人塞进
# 一个 5 KB 新章节时报出来。
PERSONA_BUDGET_BYTES = 13_000
PERSONA_BUDGET_WARN = 9_500


@dataclass
class Issue:
    level: str            # error / warn / info
    code: str
    message: str
    persona: str = ""
    detail: Any = None

    def to_dict(self) -> dict:
        d = {"level": self.level, "code": self.code, "message": self.message}
        if self.persona:
            d["persona"] = self.persona
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass
class DoctorReport:
    issues: list[Issue] = field(default_factory=list)
    previews: dict[str, dict] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    def add(self, level: str, code: str, message: str,
            persona: str = "", detail: Any = None) -> None:
        self.issues.append(Issue(level, code, message, persona, detail))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    def to_dict(self) -> dict:
        return {
            "ok": not self.errors,
            "counts": {"error": len(self.errors),
                       "warn": len(self.warnings),
                       "info": len([i for i in self.issues if i.level == "info"])},
            "stats": self.stats,
            "issues": [i.to_dict() for i in self.issues],
            "previews": self.previews,
        }

    def text(self, limit: int = 60) -> str:
        lines = []
        lines.append(f"人格数: {self.stats.get('personas', 0)}   "
                     f"错误: {len(self.errors)}   警告: {len(self.warnings)}")
        for i in self.issues[:limit]:
            tag = {"error": "✗", "warn": "!", "info": "·"}[i.level]
            who = f"[{i.persona}] " if i.persona else ""
            lines.append(f"  {tag} {who}{i.code}: {i.message}")
        if len(self.issues) > limit:
            lines.append(f"  … 另有 {len(self.issues) - limit} 条")
        return "\n".join(lines)


# ---------------------------------------------------------------- 各检查项

def _check_fk(doc: PersonaDoc, rep: DoctorReport) -> None:
    """外键：world 指向存在的世界观目录（方案 9.9.5-2）。"""
    if not doc.world:
        rep.add("info", "world.none",
                f"未绑定世界观（world 为空）—— 该人格不注入世界观提示",
                doc.id)
        return
    wdir = WORLDS_DIR / doc.world
    if not wdir.is_dir():
        rep.add("error", "world.dangling",
                f"world='{doc.world}' 指向不存在的目录 {wdir}",
                doc.id, {"expected": str(wdir)})
        return
    n = sum(1 for _ in wdir.rglob("*.md"))
    if n == 0:
        rep.add("warn", "world.empty",
                f"世界观目录 '{doc.world}' 没有任何 .md 文件", doc.id)
    else:
        rep.add("info", "world.ok",
                f"世界观 '{doc.world}' 有 {n} 个文件", doc.id)


# 迁移期的暂存文件：被合并进 persona.yaml 后应删除，不算孤儿
STAGING_FILES = {"content/lore_extracted.yaml"}


def _check_refs(doc: PersonaDoc, rep: DoctorReport) -> dict:
    """文件引用完整性 + 孤儿文件检测。"""
    referenced: set[str] = set()
    for key, rel in doc.content.items():
        p = doc.dir / rel if doc.dir else None
        if p is None or not p.exists():
            # skill 缺失已由 loader 记为 error；name_txt 可有可无
            if key not in ("skill", "name_txt"):
                rep.add("warn", "content.missing",
                        f"content.{key} 指向的文件不存在: {rel}", doc.id)
            else:
                referenced.add(Path(rel).as_posix())
            continue
        referenced.add(Path(rel).as_posix())
    for tier, rel in doc.tiers.items():
        p = doc.dir / rel if doc.dir else None
        if p is None or not p.exists():
            rep.add("warn", "tier.missing",
                    f"档位 {tier} 的文件不存在: {rel}（该档位不注入行为文案）",
                    doc.id)
        else:
            referenced.add(Path(rel).as_posix())
    # lore.extra_blocks[].file 也是引用：抽取出来的长尾正文落在
    # content/sections/<id>.md，由 persona.yaml 的 lore 段指向它们。
    # 漏掉这一处会把所有 extra_block 正文误报成孤儿文件。
    for b in (doc.lore.extra_blocks or []):
        rel = (b or {}).get("file")
        if not rel:
            continue
        p = doc.dir / rel if doc.dir else None
        if p is None or not p.exists():
            rep.add("warn", "lore.block_missing",
                    f"lore.extra_blocks「{b.get('id')}」指向的文件不存在: {rel}",
                    doc.id)
        else:
            referenced.add(Path(rel).as_posix())

    unexpected = [t for t in doc.tiers if t not in KNOWN_TIERS]
    if unexpected:
        rep.add("info", "tier.custom",
                f"使用了非内置档位名 {unexpected}（合法，属自由扩展）", doc.id)

    # 孤儿文件：目录里存在但 yaml 未引用
    orphans: list[str] = []
    if doc.dir:
        for p in doc.dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(doc.dir).as_posix()
            if rel == "persona.yaml" or rel in referenced:
                continue
            if rel in STAGING_FILES:
                rep.add("info", "file.staging",
                        f"暂存文件 {rel} 尚未合并进 persona.yaml", doc.id)
                continue
            if rel.startswith("content/"):
                orphans.append(rel)
    if orphans:
        rep.add("warn", "file.orphan",
                f"{len(orphans)} 个文件未被 persona.yaml 引用（不注入，纯占位）",
                doc.id, {"files": orphans})
    return {"referenced": sorted(referenced), "orphans": orphans}


def _check_sections(doc: PersonaDoc, rep: DoctorReport) -> dict:
    """章节完整性 + 枚举槽位 + 自定义章节。"""
    outline = doc.outline
    present = set(outline.ids()) if outline else set()
    missing_required = []
    for spec in all_specs():
        if not spec.required:
            continue
        if spec.id in ("style_summary", "appearance", "relation_state",
                       "tier_behavior", "lore_brief", "lore_full", "dialog",
                       "world_hint"):
            continue          # 这些不是 SKILL.md 里的章节
        if spec.id not in present:
            missing_required.append(f"{spec.id}（{spec.title}）")
    if missing_required:
        rep.add("error", "section.required_missing",
                f"缺少必填章节: {'; '.join(missing_required)}", doc.id)

    # 逐节校验器
    if outline:
        for node in outline.all_nodes():
            spec = node.spec
            if spec is None or spec.validator is None:
                continue
            msgs = spec.validator(node.text(outline.source)) or []
            for m in msgs:
                rep.add("warn", f"section.{spec.id}", m, doc.id)

    custom = [n.title for n in outline.all_nodes()
              if n.spec is None and n.level >= 2] if outline else []
    if custom:
        rep.add("info", "section.custom",
                f"{len(custom)} 个自定义章节（自由扩展，合法）: "
                f"{'、'.join(custom[:5])}", doc.id)
    return {"sections": len(outline.all_nodes()) if outline else 0,
            "custom_sections": custom}


def _check_aliases(docs: dict[str, PersonaDoc], rep: DoctorReport) -> dict:
    """别名唯一性（跨人格冲突会让 /persona 切换产生歧义）。"""
    index: dict[str, set[str]] = {}
    for doc in docs.values():
        # 同一人格内的大小写重复先归并，否则会把自己报成"跨人格冲突"
        for a in {x.strip().lower() for x in doc.aliases if x.strip()}:
            index.setdefault(a, set()).add(doc.id)
    conflicts = {k: sorted(v) for k, v in index.items() if len(v) > 1}
    for k, v in sorted(conflicts.items()):
        rep.add("error", "alias.conflict",
                f"别名 '{k}' 被多个人格占用: {', '.join(v)}",
                detail={"alias": k, "personas": v})
    # 同人格内重复（迁移脚本已按不区分大小写去重，这里是防御性检查）
    for doc in docs.values():
        low = [a.strip().lower() for a in doc.aliases if a.strip()]
        dup = sorted({a for a in low if low.count(a) > 1})
        if dup:
            rep.add("warn", "alias.dup_in_persona",
                    f"人格内别名重复（不区分大小写）: {dup}", doc.id)
    return {"aliases_total": sum(len(d.aliases) for d in docs.values()),
            "alias_conflicts": len(conflicts)}


def _check_budget(doc: PersonaDoc, rep: DoctorReport, previews: dict) -> None:
    """注入预算：量真实稳定段字节数。

    预算必须**分开看框架成本与人格成本**：
    框架块（_pfm_framework 的规则文本）是所有人格共用的固定开销，
    把它算进"人格预算"会让每个人格都超预算，告警失去区分度。
    """
    try:
        stable = R.build_stable_segment(doc)
        fw = len(R.load_framework().encode("utf-8"))
        l1 = len(R.build_l1(doc).encode("utf-8"))
        l2 = len(R.build_l2(doc).encode("utf-8"))
        wh = len(R.build_world_hint(doc).encode("utf-8"))
    except Exception as e:
        rep.add("error", "render.failed", f"装配失败: {e!r}", doc.id)
        return
    n = len(stable.encode("utf-8"))
    own = l1 - fw + l2          # 人格自有内容：技能正文 + 风格摘要
    previews[doc.id] = {
        "stable_bytes": n,
        "framework_bytes": fw,
        "persona_bytes": own,
        "l1_bytes": l1, "l2_bytes": l2,
        "world_hint_bytes": wh,
        "est_tokens": round(n / 1.6),      # 中文约 1.6 B/token，仅作量级参考
        "sections": len(doc.outline.all_nodes()) if doc.outline else 0,
        "world": doc.world,
        "dialog_inject": doc.effective_inject("dialog"),
        "lore_inject": doc.effective_inject("lore_full"),
    }
    if own > PERSONA_BUDGET_BYTES:
        rep.add("error", "budget.persona_over",
                f"人格自有内容 {own} B 超预算 {PERSONA_BUDGET_BYTES} B"
                f"（框架 {fw} B 另计）", doc.id)
    elif own > PERSONA_BUDGET_WARN:
        rep.add("warn", "budget.persona_near",
                f"人格自有内容 {own} B 接近预算 {PERSONA_BUDGET_BYTES} B",
                doc.id)


def _check_dialog_not_always(doc: PersonaDoc, rep: DoctorReport) -> None:
    """dialog 不该每轮注入（旧实现每轮全量注入 dialog，是主要浪费之一）。"""
    pol = doc.effective_inject("dialog")
    if pol == "always":
        rep.add("warn", "inject.dialog_always",
                f"dialog 的注入策略是 always（每轮全量注入台词样本，"
                f"建议 on_demand）", doc.id)


def _skill_name_dirs() -> set[str]:
    """收集 AstrBot 实际能解析到的全部技能名。

    技能来自两处（对齐 core/skills/skill_manager.py）：
      * **工作区**：`data/skills/<name>`（就是 SKILLS_DIR 本身，注意别再拼一层 skills）
      * **插件**：`<插件目录>/skills/<name>`，**含内置插件**
        （`backend/app/astrbot/builtin_stars/<插件>/skills/<name>`）

    只查工作区会把内置技能（documents/pdf/skill-creator/spreadsheets）
    误判成"悬空条目"。
    """
    names: set[str] = set()

    # ① 工作区技能：直接就是 SKILLS_DIR 的子目录
    try:
        if SKILLS_DIR.is_dir():
            names.update(x.name for x in SKILLS_DIR.iterdir() if x.is_dir())
    except Exception:
        pass

    # ② 需要扫描 skills/ 子目录的根：用户插件 + 内置插件
    plugin_roots: list[Path] = []
    try:
        plugin_roots.extend(p for p in (DATA_DIR / "plugins").iterdir()
                            if p.is_dir())
    except Exception:
        pass

    builtin: Path | None = None
    try:  # 优先问 AstrBot 自己
        from astrbot.core.utils.astrbot_path import (  # type: ignore
            get_astrbot_builtin_plugin_path)
        builtin = Path(get_astrbot_builtin_plugin_path())
    except Exception:
        pass
    if builtin is None:
        # 兜底：插件在 <root>/Astrbot_Env/.new_env/data/plugins/<self>，
        # 于是 <root>/backend/app/astrbot/builtin_stars 就是内置插件目录
        for cand in (
            Path(__file__).resolve().parents[7] / "backend" / "app"
            / "astrbot" / "builtin_stars",
            DATA_DIR.parent.parent / "backend" / "app" / "astrbot"
            / "builtin_stars",
        ):
            if cand.is_dir():
                builtin = cand
                break
    if builtin is not None and builtin.is_dir():
        try:
            plugin_roots.extend(p for p in builtin.iterdir() if p.is_dir())
        except Exception:
            pass

    for root in plugin_roots:
        d = root / "skills"
        if not d.is_dir():
            continue
        try:
            names.update(x.name for x in d.iterdir() if x.is_dir())
        except Exception:
            continue
    return names


def _check_skills_json(rep: DoctorReport) -> dict:
    """悬空条目：skills.json 中已无对应目录的技能名。"""
    sj = DATA_DIR / "skills.json"
    if not sj.exists():
        rep.add("warn", "skills_json.missing", f"缺少 {sj}")
        return {}
    try:
        raw = sj.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        rep.add("error", "skills_json.invalid",
                f"skills.json 不是合法 JSON（{e.msg} @line {e.lineno}）"
                f"—— 会让 AstrBot skill_manager 启动报错")
        return {}
    # AstrBot 的 skills.json 形如 {"skills": {"<技能名>": {"active": true}}}
    # 技能名是 `data["skills"]` 的**键**，不是 `data` 自己的键 ——
    # 把 data 的键当条目会把 "skills" 本身误报成悬空技能。
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        inner = data.get("skills")
        if isinstance(inner, dict):
            entries = list(inner.keys())
        elif isinstance(inner, list):
            entries = inner
        else:
            entries = []
    else:
        entries = []
    known = _skill_name_dirs()
    dangling = []
    for name in entries:
        if not isinstance(name, str):
            continue
        # 工作区或任一插件（含内置）里有同名目录就不算悬空
        if (SKILLS_DIR / name).is_dir() or name in known:
            continue
        dangling.append(name)
    if dangling:
        rep.add("warn", "skills_json.dangling",
                f"{len(dangling)} 条悬空技能条目（目录已不存在）: "
                f"{', '.join(dangling[:8])}", detail={"entries": dangling})
    return {"skills_json_entries": len(entries), "dangling": dangling}


def _check_worlds(rep: DoctorReport) -> dict:
    """每个世界目录是否非空（无 schema，只做非侵入检查）。"""
    if not WORLDS_DIR.is_dir():
        rep.add("warn", "worlds.missing", f"缺少世界观目录 {WORLDS_DIR}")
        return {}
    out = {}
    for d in sorted(WORLDS_DIR.iterdir()):
        if not d.is_dir():
            continue
        n = sum(1 for _ in d.rglob("*.md"))
        out[d.name] = n
        if n == 0:
            rep.add("warn", "world.dir_empty", f"世界目录 '{d.name}' 为空",
                    detail={"dir": str(d)})
    leftover = [str(p.relative_to(WORLDS_DIR)).replace("\\", "/")
                for p in WORLDS_DIR.rglob("_index.md")]
    if leftover:
        rep.add("info", "worlds.index_leftover",
                f"仍存在 {len(leftover)} 个 _index.md（文件树已取代清单，"
                f"可删）", detail={"files": leftover})
    return out


# ---------------------------------------------------------------- 入口

def run(personas_root: Path | None = None) -> DoctorReport:
    root = personas_root or PERSONAS_DIR
    rep = DoctorReport()

    if not is_migrated():
        rep.add("info", "root.not_migrated",
                f"尚未标记迁移完成（缺少 {root.parent / '.chatcore_neo'}）")

    dirs = iter_persona_dirs(root)
    docs: dict[str, PersonaDoc] = {}
    for d in dirs:
        try:
            doc = PersonaDoc.load(d)
        except Exception as e:
            rep.add("error", "persona.load_failed",
                    f"加载失败 {d.name}: {e!r}")
            continue
        if doc.id in docs:
            rep.add("error", "persona.duplicate_id",
                    f"id 重复: {doc.id}（{d.name} 与 {docs[doc.id].dir.name}）")
        docs[doc.id] = doc
        for err in doc.loaded_errors:
            rep.add("error", "persona.file_missing", err, doc.id)

    total_sections = 0
    for doc in docs.values():
        _check_fk(doc, rep)
        _check_refs(doc, rep)
        s = _check_sections(doc, rep)
        total_sections += s["sections"]
        _check_dialog_not_always(doc, rep)
        _check_budget(doc, rep, rep.previews)

    alias_stats = _check_aliases(docs, rep)
    sj = _check_skills_json(rep)
    worlds = _check_worlds(rep)

    rep.stats = {
        "personas": len(docs),
        "personas_dir": str(root),
        "sections_total": total_sections,
        **alias_stats, **sj,
        "worlds": worlds,
        "persona_budget_bytes": PERSONA_BUDGET_BYTES,
        "framework_bytes": next(iter(rep.previews.values()))["framework_bytes"] if rep.previews else 0,
    }
    return rep
