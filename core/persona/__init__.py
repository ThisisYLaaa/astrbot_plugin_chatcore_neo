# -*- coding: utf-8 -*-
"""人格层：注册表 / 大纲 / 文档模型 / 装配 / 校验 / 撰写 / 服务。"""

from . import authoring, doctor, render
from .authoring import (add_section, edit_section, new_persona, preview,
                        remove_section, set_inject, todo_report)
from .doctor import DoctorReport, Issue, run as run_doctor
from .model import (KNOWN_TIERS, SCHEMA_VERSION, TIER_LABEL, TIER_LEGACY,
                    LoreData, PersonaDoc, iter_persona_dirs, load_all)
from .outline import (Outline, SectionNode, parse_outline, read_skill_file,
                      render_node, render_selection, split_frontmatter,
                      strip_frontmatter)
from .registry import (FRAMEWORK_NOTICE, INJECT_POLICIES, LAYERS, SectionSpec,
                       all_specs, get_spec, normalize_title, register_section,
                       resolve_or_custom, resolve_title, slugify,
                       unregister_section)
from .render import (build_appearance, build_l1, build_l2, build_stable_segment,
                     build_world_hint, legacy_shape, load_framework,
                     load_nsfw_body, load_tier_body, resolve_lore_section)
from .service import PersonaService

__all__ = [
    # model
    "PersonaDoc", "LoreData", "SCHEMA_VERSION", "KNOWN_TIERS", "TIER_LABEL",
    "TIER_LEGACY", "iter_persona_dirs", "load_all",
    # outline
    "Outline", "SectionNode", "parse_outline", "read_skill_file",
    "render_node", "render_selection", "split_frontmatter", "strip_frontmatter",
    # registry
    "SectionSpec", "register_section", "unregister_section", "all_specs",
    "get_spec", "resolve_title", "resolve_or_custom", "normalize_title",
    "slugify", "LAYERS", "INJECT_POLICIES", "FRAMEWORK_NOTICE",
    # render
    "build_l1", "build_l2", "build_appearance", "build_stable_segment",
    "build_world_hint", "legacy_shape", "load_framework", "load_nsfw_body",
    "load_tier_body", "resolve_lore_section",
    # doctor
    "run_doctor", "DoctorReport", "Issue",
    # authoring
    "new_persona", "edit_section", "add_section", "remove_section",
    "set_inject", "preview", "todo_report",
    # service
    "PersonaService",
]
