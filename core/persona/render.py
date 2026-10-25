# -*- coding: utf-8 -*-
"""装配渲染：框架加载 + 旧形态复现 + 新 L1/L2 稳定段。

为什么保留一个 `legacy_shape()`
-------------------------------
它是 W1 验收的手段：如果新数据布局能用新代码**逐字节**复现旧
`_compose_persona_rules` 的全部 120 种输出，就证明结构化改造
没有丢任何内容。它同时是过渡期的兜底渲染路径。

新形态 `build_stable_segment()` 才是在线路径：L1（身份）+ L2（说话风格），
字节稳定、可缓存、不含任何时间戳。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..paths import FRAMEWORK_DIR
from .model import PersonaDoc

# 发言风格装配表（相对于 FRAMEWORK_DIR）。
# 键 = (是否私聊, 生效发言模式)；值 = 需拼接的 slice（相对路径，缺文件自动跳过）。
#
# 分层：纪律 → 风格 → 场景 → 格式 → 状态。每层只写自己那份规则，同一份提示里
# 绝不出现两遍。两处刻意拆分：
#   * 说话风格按场景分两份（style.md 面向聊天、style_narrative.md 面向当面相处），
#     两份里完全相同的"通用底线"抽到 style_common.md —— 场景各得其所，又不重复；
#   * 格式拆成"通用底线（common）"与"场景专属（plain/narrative）"，这样群聊开
#     剧情向时不必用「本提示优先于…」去豁免"禁用括号"，从根上消除冲突。
#   (私, 1 剧情向) = 面对面 + 当面相处风格 + 通用底线 + 括号规范
#   (私, 2 纯信息) = 隔屏   + 聊天风格     + 通用底线 + 不换行/禁括号
#   (群, 1 剧情向) = QQ群(剧情向) + 聊天风格 + 通用底线 + 允许括号
#   (群, 2 纯信息) = QQ群(日常)   + 聊天风格 + 通用底线 + 不换行/禁括号
_STYLE_TABLE: dict[tuple[bool, int], tuple[str, ...]] = {
    (True, 1): ("core.md", "style_narrative.md", "style_common.md",
                "understand.md", "scene/private.md",
                "format/common.md", "format/narrative.md", "state/common.md"),
    (True, 2): ("core.md", "style.md", "style_common.md",
                "understand.md", "scene/private_phone.md",
                "format/common.md", "format/plain.md", "state/common.md"),
    (False, 1): ("core.md", "style.md", "style_common.md",
                 "understand.md", "scene/group_narrative.md",
                 "format/common.md", "format/narrative_group.md",
                 "state/common.md"),
    (False, 2): ("core.md", "style.md", "style_common.md",
                 "understand.md", "scene/group.md", "format/common.md",
                 "format/plain.md", "state/common.md"),
}

# NSFW 规则表：(是否私聊, 生效发言模式) -> slice 列表。会话级 rules=["nsfw"] 时追加。
# 群聊走「群内策略 + 无括号描写」（与旧实现 func_sex_in_group + rules_nsfw_nobracket 一致）。
_NSFW_TABLE: dict[tuple[bool, int], tuple[str, ...]] = {
    (True, 1): ("nsfw/policy_private.md",),
    (True, 2): ("nsfw/policy_nobracket.md",),
    (False, 1): ("nsfw/policy_group.md", "nsfw/policy_nobracket.md"),
    (False, 2): ("nsfw/policy_group.md", "nsfw/policy_nobracket.md"),
}


def load_framework(is_private: bool = False, msg_mode: int = 1) -> str:
    """按 (会话类型, 发言模式) 查装配表并拼接 slice（与旧实现同序、同缓存）。"""
    slices = _STYLE_TABLE.get((bool(is_private), int(msg_mode)))
    if not slices:
        slices = _STYLE_TABLE[(bool(is_private), 2)]
    return _read_and_join(slices)


def load_nsfw_body(is_private: bool, msg_mode: int) -> str:
    """NSFW 规则文本（按场景查表，可多文件拼接；与旧实现同源）。"""
    names = _NSFW_TABLE.get((bool(is_private), int(msg_mode)),
                            ("nsfw/policy_nobracket.md",))
    parts = [read_text_cached(FRAMEWORK_DIR / n) for n in names]
    return "\n\n".join(p for p in parts if p)

# 旧装配的分区标题（保持原样，兼容路径必须一字不差）
SEP = "\n\n---\n\n"
H_FRAMEWORK = "# 通用框架规则"
H_SKILL = "# 角色特有设定（{persona}）"
H_TIER = "# 与当前用户的关系与行为（等级 {tier}）"
H_NSFW = "# NSFW 内容规则（仅对不受制约用户生效）"
H_LORE = "# 角色背景剧情（lore.md）"
H_DIALOG = "# 角色台词风格参考（dialog.md）"


def read_text_cached(path: Path) -> str:
    """读文本并 strip；缺失返回空串。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_and_join(names: tuple[str, ...] | list[str]) -> str:
    parts = []
    for name in names:
        p = FRAMEWORK_DIR / name
        if p.exists():
            t = read_text_cached(p)
            if t:
                parts.append(t)
    return SEP.join(parts)


def load_tier_body(doc: PersonaDoc, tier: str) -> str:
    """按档位读关系行为文案。

    `tier` 接受新命名（intimate/friend）与旧编号（"1"/"2"）两种写法，
    迁移期两边的数据都能读。
    """
    name = {"1": "intimate", "2": "friend"}.get(str(tier), str(tier))
    p = doc.tier_path(name)
    return read_text_cached(p) if p else ""


def legacy_shape(doc: PersonaDoc, *,
                 include_dialog: bool = True,
                 include_nsfw: bool = False,
                 is_private: bool = False,
                 msg_mode: int = 1,
                 tier: str = "intimate") -> tuple[str, dict]:
    """复现旧 `_compose_persona_rules` 的输出形态（逐字节可比）。

    唯一的差别是数据来源：正文来自 `personas/<id>/content/`，
    框架来自 `data/skills/_pfm_framework/`。
    """
    if not doc.skill_body:
        return "", {"found": False, "persona": doc.id}

    framework = load_framework(is_private=is_private, msg_mode=msg_mode)
    tier_body = load_tier_body(doc, tier)
    lore_body = doc.read_content("lore")
    dialog_body = doc.read_content("dialog") if include_dialog else ""
    nsfw_body = load_nsfw_body(is_private, msg_mode) if include_nsfw else ""

    merged = (
        f"{H_FRAMEWORK}\n\n{framework}\n\n"
        f"---\n\n"
        f"{H_SKILL.format(persona=doc.legacy_dir or doc.id)}\n\n{doc.skill_body}"
    )
    if tier_body:
        num = {"intimate": "1", "friend": "2"}.get(str(tier), str(tier))
        merged += f"{SEP}{H_TIER.format(tier=num)}\n\n{tier_body}"
    if nsfw_body:
        merged += f"{SEP}{H_NSFW}\n\n{nsfw_body}"
    if lore_body:
        merged += f"{SEP}{H_LORE}\n\n{lore_body}"
    if dialog_body:
        merged += f"{SEP}{H_DIALOG}\n\n{dialog_body}"

    info = {
        "found": True, "persona": doc.id, "tier": tier,
        "tier_rules_loaded": bool(tier_body),
        "lore_loaded": bool(lore_body),
        "dialog_loaded": bool(dialog_body),
        "nsfw_rules_loaded": bool(nsfw_body),
        "total_length": len(merged),
    }
    return merged, info


# ---------------------------------------------------------------- 新形态

# 世界观能力提示（方案 9.9.4：每轮只一行，且仅在存在世界观资料时注入）
WORLD_HINT = (
    "# 世界观资料\n"
    "需要设定细节时用 world_search 检索、world_read 读取，不要凭印象编造。"
)


# 人格正文里"把场景写死"的样板句标记。旧模板统一是
# 「你现在正在QQ群聊中和群友聊天，你是一个普通的群友。」——
# 场景该由框架按会话（群聊/私聊·面对面/私聊·隔屏）决定，人格里不该写死。
_SCENE_HARDCODE_MARKERS = ("QQ群聊", "QQ 群聊", "正在QQ群", "群聊中和群友")


def strip_stale_boilerplate(text: str) -> str:
    """剥掉人格正文里的历史样板 —— **兜底安全网**，不是主要机制。

    2026-09-23 起，人格文件里的这两类样板已由
    `migrations/strip_persona_boilerplate.py` **直接清掉**（源头干净）。
    这里保留一道幂等的规范化，只防两种回流：
      * 迁移脚本从旧环境重新导入人格数据；
      * 有人照旧模板手写新人格。

    三类，都是"框架该管却写进了人格"的东西：

    1. `> **框架引用：** …需通过 read_persona_skill 工具获取完整规则后遵循`
       —— 本插件**没有**这个工具，等于叫模型去调用不存在的东西；
    2. 任何提到 `read_persona_skill` 的句子；
    3. **场景硬编码句**：旧模板统一写成「你现在正在QQ群聊中和群友聊天，你是一个
       普通的群友。」—— 私聊（尤其面对面剧情向）里这句直接和场景规则打架。
       场景由框架按会话决定，这里只摘掉这一句，同一行其余内容保留
       （如「你的网名是「香奈美」，请根据以下信息进行角色扮演。」）。

    留着它们只会让模型以为"场景/规则要去别处取"，正是要消除的混乱来源。
    """
    if not text:
        return ""
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(">") and ("框架引用" in s or "read_persona_skill" in s):
            continue
        if "read_persona_skill" in s:
            continue
        if any(m in ln for m in _SCENE_HARDCODE_MARKERS):
            # 按句切分，只丢含硬编码的那句，同行其余句子原样保留
            kept = [p for p in re.split(r"(?<=[。！？])", ln)
                    if not any(m in p for m in _SCENE_HARDCODE_MARKERS)]
            ln = "".join(kept).strip()
            if not ln:
                continue
        out.append(ln)
    joined = "\n".join(out)
    while "\n\n\n" in joined:                    # 去掉被删行留下的空档
        joined = joined.replace("\n\n\n", "\n\n")
    return joined.strip()


def build_l1(doc: PersonaDoc, *, include_framework: bool = True,
             is_private: bool = False, msg_mode: int = 1,
             exclude_sections: set[str] | None = None) -> str:
    """L1 身份段：框架 + 角色设定（SKILL.md 正文）+ 外貌。字节稳定，进 system_prompt。

    **不再套外层标题**（`# 通用框架规则` / `# 角色特有设定（x）`）：框架 slice 与
    人格正文各自都带标题，再套一层只会产生"标题套标题"的噪音与同名标题。

    `exclude_sections` 非空时才做章节裁剪（走规范化重组）；
    为空时直接用逐字节正文，保证与旧 L1 完全一致。

    外貌是**文件级**内容（`content/appearance.md`），不是 SKILL.md 的章节 ——
    它走 `read_content` 而非大纲，理由是要保持 skill.md 的逐字节副本属性。
    """
    from .outline import render_selection

    parts: list[str] = []
    if include_framework:
        fw = load_framework(is_private=is_private, msg_mode=msg_mode)
        if fw:
            parts.append(fw)
    if doc.skill_body:
        if exclude_sections:
            body = render_selection(doc.outline, exclude=exclude_sections,
                                    include_preamble=True, include_title=True)
        else:
            body = doc.skill_body
        body = strip_stale_boilerplate(body)
        if body:
            parts.append(body)
    ap = build_appearance(doc)
    if ap:
        parts.append(ap)
    return SEP.join(p for p in parts if p.strip())


def build_appearance(doc: PersonaDoc) -> str:
    """L1 外貌段：由立绘判读出的发色/发型/瞳色/服饰/体征。

    单独成文件而非并入 SKILL.md，是为了不让"逐字节副本"这条迁移证据失效；
    与 `style_summary` 一样属后补的新数据。
    """
    s = doc.read_content("appearance")
    if not s:
        return ""
    return f"# 外貌\n\n{s}"


def build_l2(doc: PersonaDoc) -> str:
    """L2 说话习惯段：由 speech_patterns.txt 提炼的摘要。

    标题刻意不用「说话风格」——框架里已有一份硬性底线叫「说话风格」，
    两份同名会让模型不知道该听哪个。
    """
    s = doc.read_content("style_summary")
    if not s:
        return ""
    return f"# 这个角色的说话习惯\n\n{s}"


def resolve_lore_section(doc: PersonaDoc) -> str:
    """L4 背景段：策略为 always 时注入 lore.md 原文，否则退回抽取的短摘要。

    为什么默认走原文：lore.md 的「关键人物/关系」表、剧情时间线、游戏内设定
    都只存在于原文里，`LoreData.brief()` 只保留 origin/motivation +
    weight 最高的 4 条关系 —— 角色被问到被截断的第 5 条关系时只能答"不认识"。
    摘要仅作为把 `lore_full` 显式改为按需/关闭时的兜底，避免重复注入。
    """
    if doc.effective_inject("lore_full") == "always":
        full = doc.read_content("lore")
        if full:
            return full                          # 原文自带标题，不再套壳
    brief = doc.lore.brief() if doc.lore and doc.lore.extracted else ""
    if not brief:
        return ""
    return f"# 角色背景\n\n{brief}"


def build_world_hint(doc: PersonaDoc) -> str:
    """一行世界观能力提示；无世界观资料时为空串。"""
    from ..paths import WORLDS_DIR
    if not doc.world:
        return ""
    wdir = WORLDS_DIR / doc.world
    if not wdir.is_dir() or not any(wdir.rglob("*.md")):
        return ""
    return WORLD_HINT


def build_stable_segment(doc: PersonaDoc, *, include_framework: bool = True,
                         is_private: bool = False, msg_mode: int = 1,
                         exclude_sections: set[str] | None = None,
                         nsfw: bool = False) -> str:
    """稳定段 = L1 + L2 + L4 背景 + 世界观提示行。均不含时间戳，可缓存。

    `nsfw` 与会话级开关一致：开着才把 NSFW 规则拼进来，预览才等于实况。
    """
    parts = [build_l1(doc, include_framework=include_framework,
                      is_private=is_private, msg_mode=msg_mode,
                      exclude_sections=exclude_sections)]
    l2 = build_l2(doc)
    if l2:
        parts.append(l2)
    if nsfw:
        body = load_nsfw_body(is_private, msg_mode)
        if body:
            parts.append(body)
    lore = resolve_lore_section(doc)
    if lore:
        parts.append(lore)
    wh = build_world_hint(doc)
    if wh:
        parts.append(wh)
    return SEP.join(p for p in parts if p.strip())
