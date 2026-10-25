# -*- coding: utf-8 -*-
"""人格文档模型 —— `personas/<id>/persona.yaml` 的读写与校验。

存储契约
--------
`persona.yaml` **只放真正结构化的东西**：身份外键、别名、关系档位文件引用、
注入策略覆盖、以及 lore 的抽取产物。

SKILL.md / lore.md / dialog.md 的正文一律以**逐字节文件**保存在
`content/` 下，不塞进 YAML 大字符串 —— 理由见方案 9.5：
「一刀切把 lore.md 塞进 YAML 大字符串只会让 diff 变脏」。
这样 persona.yaml 保持人可读、diff 干净，而正文的无损性由
「文件就是原文件」直接保证。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise ImportError("chatcore_neo 需要 PyYAML") from e

from .outline import Outline, parse_outline, read_skill_file, split_frontmatter

SCHEMA_VERSION = 1

# 关系档位：intimate=特权(T1) / friend=普通(T2)。加 stranger/admin 只需加键 + 加文件。
KNOWN_TIERS = ("intimate", "friend")
TIER_LEGACY = {"intimate": "_tier1.md", "friend": "_tier2.md"}
TIER_LABEL = {"intimate": "等级 1（特权）", "friend": "等级 2（普通）"}


@dataclass
class LoreData:
    """lore.md 的**抽取**产物（不是搬运）—— 见方案 9.5.1。"""
    origin: str = ""
    motivation: str = ""
    themes: list[str] = field(default_factory=list)
    signature_lines: list[str] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)
    extra_blocks: list[dict[str, Any]] = field(default_factory=list)
    extracted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "extracted": self.extracted,
            "origin": self.origin,
            "motivation": self.motivation,
            "themes": list(self.themes),
            "signature_lines": list(self.signature_lines),
            "relations": list(self.relations),
            "timeline": list(self.timeline),
            "extra_blocks": list(self.extra_blocks),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LoreData":
        d = d or {}
        return cls(
            origin=d.get("origin", "") or "",
            motivation=d.get("motivation", "") or "",
            themes=list(d.get("themes") or []),
            signature_lines=list(d.get("signature_lines") or []),
            relations=list(d.get("relations") or []),
            timeline=list(d.get("timeline") or []),
            extra_blocks=list(d.get("extra_blocks") or []),
            extracted=bool(d.get("extracted")),
        )

    def brief(self) -> str:
        """每轮注入的短背景（origin + motivation + 主要关系）。"""
        parts: list[str] = []
        if self.origin:
            parts.append(f"- 来历：{self.origin}")
        if self.motivation:
            parts.append(f"- 核心动机：{self.motivation}")
        rels = [r for r in self.relations if r.get("who")]
        if rels:
            rels = sorted(rels, key=lambda r: -int(r.get("weight") or 0))[:4]
            parts.append("- 重要关系：" + "；".join(
                f"{r.get('who')}（{r.get('rel', '')}）" for r in rels))
        if self.themes:
            parts.append("- 主题：" + "、".join(str(t) for t in self.themes[:6]))
        return "\n".join(parts)


@dataclass
class PersonaDoc:
    id: str
    display_name: str
    display_name_en: str = ""
    aliases: list[str] = field(default_factory=list)
    world: str = ""
    world_role: str = ""
    supersedes: list[str] = field(default_factory=list)
    legacy_dir: str = ""
    active: bool = True

    skill_frontmatter: dict[str, str] = field(default_factory=dict)
    default_inject: str = "always"
    inject_overrides: dict[str, str] = field(default_factory=dict)

    content: dict[str, str] = field(default_factory=dict)
    tiers: dict[str, str] = field(default_factory=dict)

    lore: LoreData = field(default_factory=LoreData)

    # ---- 运行时派生（不写入 yaml）----
    dir: Path | None = None
    skill_body: str = ""
    outline: Outline | None = None
    loaded_errors: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 读取
    @classmethod
    def load(cls, persona_dir: Path) -> "PersonaDoc":
        yml = persona_dir / "persona.yaml"
        if not yml.exists():
            raise FileNotFoundError(f"缺少 persona.yaml: {yml}")
        raw = yaml.safe_load(yml.read_text(encoding="utf-8-sig")) or {}

        doc = cls(
            id=raw.get("id") or persona_dir.name,
            display_name=raw.get("display_name") or persona_dir.name,
            display_name_en=raw.get("display_name_en") or "",
            aliases=list(raw.get("aliases") or []),
            world=(raw.get("world") or "").strip(),
            world_role=(raw.get("world_role") or "").strip(),
            supersedes=list(raw.get("supersedes") or []),
            legacy_dir=raw.get("legacy_dir") or "",
            active=bool(raw.get("active", True)),
            default_inject=(raw.get("skill") or {}).get("default_inject", "always"),
            inject_overrides=dict(raw.get("inject_overrides") or {}),
            content=dict(raw.get("content") or {}),
            tiers=dict((raw.get("relationship") or {}).get("tiers") or {}),
            lore=LoreData.from_dict(raw.get("lore")),
            dir=persona_dir,
        )
        doc.skill_frontmatter = dict((raw.get("skill") or {}).get("frontmatter") or {})

        # 派生：正文 + 大纲（永远从文件现算）
        skill_path = persona_dir / doc.content.get("skill", "content/skill.md")
        if skill_path.exists():
            fm, body, outline = read_skill_file(skill_path)
            doc.skill_body = body
            doc.outline = outline
            if not doc.skill_frontmatter:
                doc.skill_frontmatter = fm
        else:
            doc.loaded_errors.append(f"缺少正文文件: {skill_path}")
            doc.outline = parse_outline("")
        return doc

    # ------------------------------------------------------------------ 写入
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "id": self.id,
            "display_name": self.display_name,
        }
        if self.display_name_en:
            d["display_name_en"] = self.display_name_en
        d["aliases"] = list(self.aliases)
        d["world"] = self.world
        d["world_role"] = self.world_role
        d["supersedes"] = list(self.supersedes)
        if self.legacy_dir:
            d["legacy_dir"] = self.legacy_dir
        if not self.active:
            d["active"] = False
        d["skill"] = {
            "frontmatter": dict(self.skill_frontmatter),
            "default_inject": self.default_inject,
        }
        d["inject_overrides"] = dict(self.inject_overrides)
        d["content"] = dict(self.content)
        d["relationship"] = {"tiers": dict(self.tiers)}
        d["lore"] = self.lore.to_dict()
        return d

    def save(self, persona_dir: Path | None = None) -> Path:
        target = persona_dir or self.dir
        if target is None:
            raise ValueError("未指定人格目录")
        target.mkdir(parents=True, exist_ok=True)
        yml = target / "persona.yaml"
        text = yaml.safe_dump(
            self.to_dict(), allow_unicode=True, sort_keys=False, width=1000,
            default_flow_style=False,
        )
        # 原子写：先写临时文件再替换，避免半截 YAML
        tmp = yml.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(yml)
        return yml

    # ------------------------------------------------------------------ 便捷
    def content_path(self, key: str) -> Path | None:
        rel = self.content.get(key)
        if not rel or self.dir is None:
            return None
        return self.dir / rel

    def read_content(self, key: str) -> str:
        p = self.content_path(key)
        if p is None or not p.exists():
            return ""
        return p.read_text(encoding="utf-8-sig").strip()

    def tier_path(self, tier: str) -> Path | None:
        rel = self.tiers.get(tier)
        if not rel or self.dir is None:
            return None
        return self.dir / rel

    def effective_inject(self, section_id: str) -> str:
        """章节的实际注入策略：覆盖 > 注册表默认 > 文档默认。

        注意：`lore` / `dialog` / `style_summary` 这类**文件级**内容在
        `content/skill.md` 的大纲里没有对应节点，因此不能依赖大纲查找——
        必须先按 id 查注册表，否则它们会全部退化到文档默认值 `always`，
        于是"dialog 改为按需注入"这条收益会被静默抵消。
        """
        if section_id in self.inject_overrides:
            return self.inject_overrides[section_id]
        from .registry import get_spec
        spec = get_spec(section_id)
        if spec is not None:
            return spec.default_inject
        if self.outline is not None:
            node = self.outline.by_id(section_id)
            if node is not None and node.spec is not None:
                return node.spec.default_inject
        return self.default_inject

    def section_titles(self) -> list[str]:
        if self.outline is None:
            return []
        return [t for _lvl, t in self.outline.titles()]


def iter_persona_dirs(personas_root: Path) -> list[Path]:
    if not personas_root.exists():
        return []
    return sorted(p for p in personas_root.iterdir()
                  if p.is_dir() and (p / "persona.yaml").exists())


def load_all(personas_root: Path) -> dict[str, PersonaDoc]:
    out: dict[str, PersonaDoc] = {}
    for d in iter_persona_dirs(personas_root):
        try:
            doc = PersonaDoc.load(d)
        except Exception as e:  # 单个人格坏掉不应拖垮整棵树
            continue
        out[doc.id] = doc
    return out


__all__ = [
    "PersonaDoc", "LoreData", "SCHEMA_VERSION", "KNOWN_TIERS",
    "TIER_LEGACY", "TIER_LABEL", "iter_persona_dirs", "load_all",
    "split_frontmatter",
]
