# -*- coding: utf-8 -*-
"""SKILL.md 大纲解析 —— 从正文**派生**章节树，从不存储。

为什么派生而不存储
------------------
如果 `persona.yaml` 里既存正文又存章节清单，两者会漂移：
用户改了正文忘了改清单，或反过来。这里让清单永远由正文现算，
于是"清单与正文不一致"这个 bug 类别从结构上不存在。

无损性由两件事保证：
1. 正文以 `content/skill.md` **逐字节**保存在人格目录里；
2. `render_full()` 直接返回正文，不做任何重组。
   `render_selection()` 只在需要按注入策略裁剪时才重组。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .registry import SectionSpec, resolve_or_custom

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]*(.*?)[ \t]*$", re.MULTILINE)


@dataclass
class SectionNode:
    """一个标题节点（可含子节点）。"""
    level: int
    title: str
    section_id: str
    spec: SectionSpec | None
    heading_start: int
    body_start: int
    body_end: int
    children: list["SectionNode"] = field(default_factory=list)

    @property
    def is_container(self) -> bool:
        return bool(self.children) or bool(self.spec and self.spec.container)

    @property
    def own_end(self) -> int:
        """本节**自身**正文的终点：有子节点则到第一个子节点标题为止。

        这一点很关键：`body_end` 是「下一个 level<=自身的标题」，
        对容器节点（如 `## 核心设定`）它跨过了全部 H3 子节。
        若用它取自身正文，重组时子节会被吐出两遍。
        """
        return self.children[0].heading_start if self.children else self.body_end

    def text(self, source: str) -> str:
        """本节自身的正文（不含子节），已 strip。"""
        return source[self.body_start:self.own_end].strip()

    def full_text(self, source: str) -> str:
        """本节连同全部子节的正文。"""
        return source[self.body_start:self.body_end].strip()

    def own_span(self, source: str) -> tuple[int, int]:
        return self.heading_start, self.body_end

    def walk(self) -> list["SectionNode"]:
        out = [self]
        for c in self.children:
            out.extend(c.walk())
        return out


@dataclass
class Outline:
    source: str
    h1_title: str
    preamble: str
    roots: list[SectionNode]

    def all_nodes(self) -> list[SectionNode]:
        out: list[SectionNode] = []
        for r in self.roots:
            out.extend(r.walk())
        return out

    def by_id(self, section_id: str) -> SectionNode | None:
        for n in self.all_nodes():
            if n.section_id == section_id:
                return n
        return None

    def ids(self) -> list[str]:
        return [n.section_id for n in self.all_nodes()]

    def titles(self) -> list[tuple[int, str]]:
        return [(n.level, n.title) for n in self.all_nodes()]


def split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """切成 (frontmatter 键值, 剩余全文)。

    只识别文件**开头**的 `---` 围栏，与 `_compose_persona_rules` 的
    `split("---", 2)[2]` 语义保持一致。
    """
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, parts[2]


def strip_frontmatter(raw: str) -> str:
    """等价于旧实现的 skill_body 计算方式（已 strip）。"""
    return split_frontmatter(raw)[1].strip()


def parse_outline(body: str) -> Outline:
    """把正文解析成章节树。`body` 应当是 strip 过的 skill_body。"""
    marks: list[tuple[int, int, int, str]] = []   # (heading_start, body_start, level, title)
    for m in _HEADING_RE.finditer(body):
        level = len(m.group(1))
        title = m.group(2).strip()
        if not title:
            continue
        nl = body.find("\n", m.end())
        body_start = len(body) if nl < 0 else nl + 1
        marks.append((m.start(), body_start, level, title))

    # 每个标题的正文终点 = 下一个 level<=自身 的标题起点
    nodes: list[SectionNode] = []
    for i, (hs, bs, lvl, title) in enumerate(marks):
        end = len(body)
        for hs2, _bs2, lvl2, _t2 in marks[i + 1:]:
            if lvl2 <= lvl:
                end = hs2
                break
        sid, spec = resolve_or_custom(title)
        nodes.append(SectionNode(level=lvl, title=title, section_id=sid,
                                 spec=spec, heading_start=hs, body_start=bs,
                                 body_end=end))

    # 组树
    roots: list[SectionNode] = []
    stack: list[SectionNode] = []
    for n in nodes:
        while stack and stack[-1].level >= n.level:
            stack.pop()
        if stack:
            stack[-1].children.append(n)
        else:
            roots.append(n)
        stack.append(n)

    # H1 是人格标题，不是章节：把它摘掉，但**必须把它的子节点提升为顶层**，
    # 否则所有 H2 章节会随 H1 一起消失（章节树看起来是空的）。
    h1 = ""
    top_roots: list[SectionNode] = []
    for r in roots:
        if r.level == 1 and not h1:
            h1 = r.title
            top_roots.extend(r.children)
            continue
        top_roots.append(r)

    preamble = body[:marks[0][0]].strip() if marks else body.strip()
    return Outline(source=body, h1_title=h1, preamble=preamble, roots=top_roots)


def render_node(node: SectionNode, source: str, include_children: bool = True) -> str:
    """按规范化格式重组一个节点（`## 标题` + 空行 + 正文）。"""
    head = "#" * node.level + " " + node.title
    if node.is_container and include_children:
        parts = [head]
        own = node.text(source)
        if own:
            parts.append(own)
        for c in node.children:
            parts.append(render_node(c, source, include_children))
        return "\n\n".join(parts)
    return head + "\n\n" + node.text(source)


def render_selection(outline: Outline, section_ids: list[str] | None = None,
                     exclude: set[str] | None = None,
                     include_preamble: bool = False,
                     include_title: bool = False) -> str:
    """按 id 选择/排除后重组正文。

    - `section_ids=None` → 全部章节
    - `exclude` 命中的节点整棵子树被跳过（含子节点）
    - `include_preamble` / `include_title` 打开后可复现**完整正文**，
      用于无损性验证；注入路径默认关闭（前言块引用的是旧工具协议，已废弃）。

    顶层节点之间用空行连接，与原文的空行约定一致。
    """
    exclude = exclude or set()
    picked: list[str] = []

    if include_preamble and outline.preamble:
        picked.append(outline.preamble)
    if include_title and outline.h1_title:
        picked.append("# " + outline.h1_title)

    def want(n: SectionNode) -> bool:
        if section_ids is None:
            return True
        return n.section_id in section_ids

    def emit(n: SectionNode) -> None:
        if n.section_id in exclude:
            return
        if want(n):
            picked.append(render_node(n, outline.source, include_children=False))
        for c in n.children:
            emit(c)

    for r in outline.roots:
        emit(r)
    return "\n\n".join(p for p in picked if p.strip())


def read_skill_file(path: Path) -> tuple[dict[str, str], str, Outline]:
    """读 SKILL.md → (frontmatter, 逐字节正文, 大纲)。"""
    raw = path.read_text(encoding="utf-8")
    meta, rest = split_frontmatter(raw)
    body = rest.strip()
    return meta, body, parse_outline(body)
