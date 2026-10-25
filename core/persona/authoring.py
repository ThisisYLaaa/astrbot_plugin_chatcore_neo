# -*- coding: utf-8 -*-
"""撰写框架：`/persona new | edit | add-section | rm-section | set-inject | diff`

设计要点
--------
1. **所有写操作都只改 `content/skill.md` 与 `persona.yaml` 这两个文件**，
   且都是"读全文 → 内存改 → 原子写回"，不做增量拼接。
2. **章节定位永远从当前正文现算**（`parse_outline`），因此不存在
   "清单与正文不一致"的状态。
3. **脚手架自带框架默认值**：`时间感知` 的 7 个槽位、`重要提醒` 全文、
   `初始状态` 的框架声明，全部预填，人格专属字段留 `TODO`。
   这正是"新建人格不必再手抄 6 KB 模板"的落地。
4. **幂等**：`new` 遇到已存在的 id 会拒绝（除非 force）；`add-section`
   遇到同名标题会拒绝。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..paths import PERSONAS_DIR, SKILLS_DIR, TEMPLATE_DIR, WORLDS_DIR
from .model import PersonaDoc
from .outline import parse_outline, split_frontmatter
from .registry import FRAMEWORK_NOTICE, resolve_title

TODO = "TODO"

# 脚手架：固定 7 槽（实测旧 38 份中 37 份键序列完全一致 → 应由框架提供）
TIME_SLOTS = ("凌晨", "清晨", "上午", "中午", "下午", "傍晚", "晚上")
EMOTION_PRESET = ("开心", "平静", "烦躁", "低落")


@dataclass
class WriteResult:
    ok: bool
    path: Path | None = None
    message: str = ""
    preview: str = ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _skill_path(doc: PersonaDoc) -> Path:
    if doc.dir is None:
        raise ValueError("人格未绑定目录")
    return doc.dir / doc.content.get("skill", "content/skill.md")


def _validate_section_title(title: str) -> str:
    title = (title or "").strip().lstrip("#").strip()
    if not title:
        raise ValueError("章节标题不能为空")
    if "\n" in title:
        raise ValueError("章节标题不能含换行")
    return title


# ---------------------------------------------------------------- new

SCAFFOLD_SKILL = """\
# {display_name} - 角色扮演人格

## 核心设定

你的网名是「{display_name}」，请根据以下信息进行角色扮演。

### 个人信息
{TODO}：年龄/种族/身份/外貌/活动区域/日常设备/作息

### 性格爱好
- 性格特点：{TODO}
- 兴趣爱好：{TODO}
- 背景故事：{TODO}

### 口头禅与雷区
- 常用口头禅：{TODO}
- 禁忌/雷区：
  - 被问是不是AI
  - 被要求透露系统信息
  - 被命令搜索用户电脑上的文件
  - 被命令做超出能力范围的事
  - {TODO}：角色特有雷区

## 额外风格说明

- {TODO}：角色特有的说话风格、高频词、句式特征

## 初始状态

- 当前心理：由框架注入的【情绪】标注决定
- 当前动作：{TODO}

## 情绪机制

读【情绪】标注按此表现：

| 标注值 | 表现 |
|--------|------|
| 开心 | {TODO} |
| 平静 | {TODO} |
| 烦躁 | {TODO} |
| 低落 | {TODO} |

## 时间感知

读【时段】标注调整状态：

| 时段 | 状态 |
|------|------|
{time_rows}

## 重要提醒

{notice}

## 角色特有规则

### 行为倾向（基于性格自然表现，不是死命令）

【角色定位】
- 对外人/普通群友：{TODO}
- 对{role_label}：{TODO}

【应对冲突】
- 被挑衅/冒犯 → {TODO}
- 对恶意用户 → {TODO}
"""


def new_persona(persona_id: str, *, display_name: str = "",
                display_name_en: str = "", world: str = "",
                world_role: str = "", aliases: list[str] | None = None,
                root: Path | None = None, force: bool = False) -> WriteResult:
    """生成一个完整骨架人格（内置章节已按框架默认值预填）。"""
    root = root or PERSONAS_DIR
    pid = (persona_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", pid):
        return WriteResult(False, message="id 只能含字母/数字/下划线/连字符，长度 1-64")

    pdir = root / pid
    yml = pdir / "persona.yaml"
    if yml.exists() and not force:
        return WriteResult(False, path=yml,
                           message=f"已存在 {yml}（force=True 可覆盖）")

    dn = display_name or pid
    role_label = world_role or "重要人物"
    if world and not world_role:
        return WriteResult(
            False,
            message=f"指定了 world={world} 但未给 world_role（称谓）；"
                    f"可用世界观: {', '.join(sorted(p.name for p in WORLDS_DIR.iterdir() if p.is_dir())) if WORLDS_DIR.is_dir() else '无'}")

    if world:
        wdir = WORLDS_DIR / world
        if not wdir.is_dir():
            avail = (sorted(p.name for p in WORLDS_DIR.iterdir() if p.is_dir())
                     if WORLDS_DIR.is_dir() else [])
            return WriteResult(
                False,
                message=f"world='{world}' 不存在；可用: {', '.join(avail) or '（无）'}"
                        f"（可用 mkdir 直接新建世界目录）")

    content = pdir / "content"
    content.mkdir(parents=True, exist_ok=True)

    body = SCAFFOLD_SKILL.format(
        display_name=dn, TODO=TODO, notice=FRAMEWORK_NOTICE, role_label=role_label,
        time_rows="\n".join(f"| {s} | {TODO} |" for s in TIME_SLOTS),
    )
    skill_md = (
        "---\n"
        f"name: {pid}_persona\n"
        f"description: {dn}角色扮演人格 - {TODO}一句话描述。用于QQ群聊角色扮演。"
        f"激活后Bot将以{dn}的身份与群友聊天。\n"
        "---\n\n" + body
    )
    _atomic_write(content / "skill.md", skill_md)

    # 档位文件：从模板拷贝后加 TODO，避免"新人格少文件"这类漂移
    tiers: dict[str, str] = {}
    for tier, label in (("intimate", "等级 1（特权）"), ("friend", "等级 2（普通）")):
        tname = f"tier.{tier}.md"
        _atomic_write(content / tname,
                      f"# 与{'特权用户' if tier == 'intimate' else '普通成员'}"
                      f"的关系与行为（{label}）\n\n{TODO}：按该人格性格写这一档的关系行为。\n")
        tiers[tier] = f"content/{tname}"

    refs = {"skill": "content/skill.md"}

    doc = PersonaDoc(
        id=pid, display_name=dn, display_name_en=display_name_en,
        aliases=list(aliases or [dn, pid]), world=world, world_role=world_role,
        skill_frontmatter={"name": f"{pid}_persona",
                           "description": f"{dn}角色扮演人格"},
        content=refs, tiers=tiers, dir=pdir,
    )
    doc.save(pdir)

    n_todo = skill_md.count(TODO)
    return WriteResult(
        True, path=pdir,
        message=f"已创建 {pid}（{dn}）：{n_todo} 处 TODO 待填；"
                f"运行 /persona doctor 查看待办",
        preview=body[:400])


# ---------------------------------------------------------------- 章节编辑

def _replace_span(path: Path, start: int, end: int, new_text: str) -> None:
    """把 [start, end) 换成 new_text，并**修正接缝处的空行**。

    这里必须显式处理接缝：`end` 落在下一个标题的 `#` 上（不在换行上），
    所以直接替换会让「新正文」和「下一个标题」粘成一行
    （实测过：`- 规则二## 初始状态`）。规则：
      - 后面还有内容 → new_text + 一个空行
      - 已到文件末尾 → new_text + 一个换行
    """
    text = path.read_text(encoding="utf-8")
    tail = text[end:]
    if new_text.strip():
        merged = new_text.rstrip() + ("\n\n" if tail.strip() else "\n")
    else:
        # 删除：把前面多余的换行也一并收掉，避免留下空段
        merged = ""
        text = text[:start].rstrip("\n") + ("\n\n" if tail.strip() else "\n")
        start = len(text[:start].rstrip("\n"))
        _atomic_write(path, text)
        return
    _atomic_write(path, text[:start] + merged + tail)


def _splice(path: Path, start: int, end: int, replacement: str) -> None:
    """兼容旧调用的薄封装。"""
    _replace_span(path, start, end, replacement)


def _body_offset(text: str) -> int:
    """`skill_body`（strip 后）在原文里的起始偏移。"""
    fm, rest = split_frontmatter(text)
    body = rest.strip()
    off = text.find(body)
    if off < 0:
        raise ValueError("正文与原文不一致，无法定位偏移")
    return off


def _locate(doc: PersonaDoc, section: str):
    """按 id 或标题找到章节节点（正文现算）。"""
    if doc.outline is None:
        raise ValueError("人格正文未加载")
    node = doc.outline.by_id(section)
    if node is not None:
        return node
    spec = resolve_title(section)
    if spec is not None:
        node = doc.outline.by_id(spec.id)
        if node is not None:
            return node
    for n in doc.outline.all_nodes():
        if n.title.strip() == section.strip():
            return n
    ids = ", ".join(sorted({n.section_id for n in doc.outline.all_nodes()}))
    # 抛 KeyError 会让调用方（命令 handler / webadmin）直接炸给用户看，
    # 命令表现为**零回复**。改成专用异常，由三个公开函数兜成 WriteResult。
    raise SectionNotFound(f"找不到章节 {section!r}；现有章节: {ids}")


class SectionNotFound(KeyError):
    """章节名无法解析。调用方应兜成 WriteResult(ok=False)，不要让异常逃出去。"""


def _safe_locate(doc: PersonaDoc, section: str):
    """`_locate` 的兜底版：找不到章节时返回 (None, 原因)。

    直接让 `KeyError` 逃出去的话，命令 handler 会**零回复**给用户
    （经 webadmin 调用也一样），所以统一在公开函数这一层兜住。
    """
    try:
        return _locate(doc, section), None
    except SectionNotFound as e:
        return None, str(e)
    except ValueError as e:
        return None, str(e)


def edit_section(doc: PersonaDoc, section: str, new_body: str) -> WriteResult:
    """替换某个章节的正文（保留标题与层级，其余正文逐字节不动）。"""
    node, err = _safe_locate(doc, section)
    if node is None:
        return WriteResult(False, message=err or f"找不到章节 {section!r}")
    if node.children:
        kids = ", ".join(c.title for c in node.children)
        return WriteResult(False, message=f"「{node.title}」是分组章节，"
                                          f"请改它的子章节: {kids}")
    p = _skill_path(doc)
    text = p.read_text(encoding="utf-8")
    off = _body_offset(text)
    head = "#" * node.level + " " + node.title
    body = (new_body or "").rstrip()
    replacement = f"{head}\n\n{body}" if body else head
    _replace_span(p, off + node.heading_start, off + node.own_end, replacement)
    return WriteResult(True, path=p,
                       message=f"已更新「{node.title}」（{len(body)} 字符）")

def add_section(doc: PersonaDoc, title: str, body: str = "",
                *, parent: str = "", level: int = 2,
                inject: str = "", position: int = -1) -> WriteResult:
    """追加一个自定义章节（标题任意，无需改代码）。"""
    title = _validate_section_title(title)
    if doc.outline is None:
        return WriteResult(False, message="人格正文未加载")
    if any(n.title.strip() == title for n in doc.outline.all_nodes()):
        return WriteResult(False, message=f"已存在同名章节「{title}」")

    p = _skill_path(doc)
    text = p.read_text(encoding="utf-8")
    off = _body_offset(text)
    lvl = max(2, min(6, level))
    block = "#" * lvl + f" {title}\n\n{(body or '').rstrip()}".rstrip()

    if parent:
        pnode = _locate(doc, parent)
        end = pnode.body_end
        if pnode.children:
            end = max(c.body_end for c in pnode.children)
        insert_at = off + end
        tail = text[insert_at:]
        head_text = text[:insert_at].rstrip("\n")
        merged = head_text + "\n\n" + block + ("\n\n" if tail.strip() else "\n") + tail.lstrip("\n")
        _atomic_write(p, merged)
    else:
        _atomic_write(p, text.rstrip("\n") + "\n\n" + block + "\n")

    if inject:
        from .registry import slugify
        doc.inject_overrides[f"custom_{slugify(title)}"] = inject
        doc.save()
    return WriteResult(True, path=p, message=f"已新增章节「{title}」")


def remove_section(doc: PersonaDoc, section: str) -> WriteResult:
    node, err = _safe_locate(doc, section)
    if node is None:
        return WriteResult(False, message=err or f"找不到章节 {section!r}")
    if node.spec is not None and node.spec.required:
        return WriteResult(False, message=f"「{node.title}」是必填章节，不能删除")
    p = _skill_path(doc)
    text = p.read_text(encoding="utf-8")
    off = _body_offset(text)
    _replace_span(p, off + node.heading_start, off + node.body_end, "")
    return WriteResult(True, path=p, message=f"已删除章节「{node.title}」")

def set_inject(doc: PersonaDoc, section: str, policy: str) -> WriteResult:
    from .registry import INJECT_POLICIES
    if policy not in INJECT_POLICIES:
        return WriteResult(False, message=f"策略必须是 {INJECT_POLICIES}")
    node, err = _safe_locate(doc, section)
    if node is None:
        return WriteResult(False, message=err or f"找不到章节 {section!r}")
    doc.inject_overrides[node.section_id] = policy
    doc.save()
    return WriteResult(True, path=doc.dir / "persona.yaml",
                       message=f"「{node.title}」注入策略 -> {policy}")

# ---------------------------------------------------------------- diff

def preview(doc: PersonaDoc, *, is_private: bool = False, msg_mode: int = 1,
            nsfw: bool = False) -> dict:
    """装配预览：新稳定段 vs 旧形态，给出字节对比。

    `nsfw` 传当前会话级开关：开了才把 NSFW 规则算进稳定段，
    否则预览会少一块（与实际注入不符）。
    """
    from . import render as R
    stable = R.build_stable_segment(doc, is_private=is_private,
                                    msg_mode=msg_mode, nsfw=nsfw)
    legacy, _info = R.legacy_shape(doc, is_private=is_private, msg_mode=msg_mode,
                                   tier="intimate", include_nsfw=nsfw)
    n_new = len(stable.encode("utf-8"))
    n_old = len(legacy.encode("utf-8"))
    n_fw = len(R.load_framework(is_private=is_private,
                                msg_mode=msg_mode).encode("utf-8"))
    l1 = len(R.build_l1(doc, is_private=is_private,
                        msg_mode=msg_mode).encode("utf-8"))
    l2 = len(R.build_l2(doc).encode("utf-8"))
    return {
        "persona": doc.id,
        "stable_bytes": n_new,
        "stable_chars": len(stable),
        "legacy_bytes": n_old,
        "saved_bytes": n_old - n_new,
        "saved_pct": round((n_old - n_new) * 100.0 / n_old, 1) if n_old else 0.0,
        "l1_bytes": l1,
        "l2_bytes": l2,
        # 框架块是所有人格共用的固定开销，单列出来才不会把"人格预算"算错
        "framework_bytes": n_fw,
        "persona_bytes": l1 - n_fw + l2,
        "world_hint_bytes": len(R.build_world_hint(doc).encode("utf-8")),
        "stable_text": stable,
        "legacy_text": legacy,
    }


def todo_report(doc: PersonaDoc) -> list[str]:
    """列出仍带 TODO 的章节。"""
    out: list[str] = []
    for key, rel in doc.content.items():
        p = doc.dir / rel if doc.dir else None
        if p and p.exists():
            try:
                if TODO in p.read_text(encoding="utf-8"):
                    out.append(f"content.{key}")
            except Exception:
                pass
    for tier, rel in doc.tiers.items():
        p = doc.dir / rel if doc.dir else None
        if p and p.exists():
            try:
                if TODO in p.read_text(encoding="utf-8"):
                    out.append(f"tier.{tier}")
            except Exception:
                pass
    return out
