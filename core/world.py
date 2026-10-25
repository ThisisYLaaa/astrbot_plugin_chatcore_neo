# -*- coding: utf-8 -*-
"""世界观：自由层级 + 三个检索工具。

设计（方案 9.9，取代早前的 schema 方案）
----------------------------------------
* **无 schema、无清单文件**。世界 id = 目录名；层级/文件名/拆分粒度完全由用户决定。
* **文件树本身就是清单** —— 废弃 `_worlds/_index.md` 与各世界的 `_index.md`。
* `world.yaml` 可选，且只认 `display_name` + `aliases` 两个字段（纯为输出好看）。
* 三个工具**直接暴露给主 agent**，不经子代理 —— 这修掉了旧实现
  `_build_world_hint` 在 `transfer_to_world_keeper` 工具不存在时
  "整个世界观看不见且不报错"的缺陷（main.py:249-252）。

工具
----
world_list(world="")                   列出世界观 / 某个世界的文件树
world_search(query, world="", limit=5) 关键词检索，返回命中文件 + 片段 + 行号
world_read(path, start=1, end=0)       读某个文件，**支持行范围**
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import WORLDS_DIR

MAX_READ_LINES = 400          # 单次 world_read 的行数上限（防止一次灌进整份文件）
MAX_SNIPPET = 300             # world_search 每条的片段字符上限
SKIP_DIRS = {"__pycache__", ".git"}


class WorldError(Exception):
    """世界观读取错误（路径越界 / 文件不存在等）。"""


# ---------------------------------------------------------------- 路径安全

def worlds_root() -> Path:
    return WORLDS_DIR


def resolve_world_path(rel: str) -> Path:
    """把工具收到的相对路径解析为绝对路径，并做**越界校验**。

    只允许 `worlds/` 之内；拒绝 `..`、绝对路径、盘符、UNC。
    """
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise WorldError("路径不能为空")
    if re.match(r"^[A-Za-z]:", rel) or rel.startswith("//"):
        raise WorldError("不允许绝对路径")
    root = worlds_root().resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise WorldError(f"路径越界（只允许 worlds/ 之内）: {rel}") from None
    if not candidate.is_file():
        raise WorldError(f"文件不存在: {rel}")
    return candidate


def list_worlds() -> list[str]:
    root = worlds_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and p.name not in SKIP_DIRS)


def world_files(world: str) -> list[Path]:
    root = worlds_root() / world
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md")
                  if p.is_file() and not any(part in SKIP_DIRS
                                             for part in p.parts))


def world_display_name(world: str) -> str:
    """`world.yaml` 只认 display_name（可选）。缺失则用目录名。"""
    yml = worlds_root() / world / "world.yaml"
    if yml.is_file():
        try:
            import yaml
            d = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            name = str(d.get("display_name") or "").strip()
            if name:
                return name
        except Exception:
            pass
    return world


def world_aliases(world: str) -> list[str]:
    yml = worlds_root() / world / "world.yaml"
    if not yml.is_file():
        return []
    try:
        import yaml
        d = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        return [str(a).strip() for a in (d.get("aliases") or []) if str(a).strip()]
    except Exception:
        return []


# ---------------------------------------------------------------- 工具实现

def tool_world_list(world: str = "") -> str:
    """无参：列出全部世界观及体量；有参：列出该世界的文件树。"""
    root = worlds_root()
    if not root.is_dir():
        return "（没有世界观资料目录）"

    if not world:
        worlds = list_worlds()
        if not worlds:
            return "（还没有任何世界观资料）"
        lines = [f"共有 {len(worlds)} 个世界观："]
        for w in worlds:
            files = world_files(w)
            size = sum(f.stat().st_size for f in files)
            disp = world_display_name(w)
            alias = world_aliases(w)
            tag = f"（{disp}）" if disp != w else ""
            if alias:
                tag += f" 别名: {'、'.join(alias)}"
            lines.append(f"- {w}{tag}：{len(files)} 个文件 / {size} B")
        lines.append("\n用 world_list(\"<世界id>\") 看文件树；"
                     "用 world_search 检索；用 world_read 读具体文件。")
        return "\n".join(lines)

    if world not in list_worlds():
        return (f"没有世界观 '{world}'。可用的：" + "、".join(list_worlds())
                or "（无）")
    files = world_files(world)
    if not files:
        return f"世界观 '{world}' 目录为空。"
    wroot = root / world
    lines = [f"世界观 '{world}'（{world_display_name(world)}）共 {len(files)} 个文件："]
    for f in files:
        rel = f.relative_to(root).as_posix()
        n = f.stat().st_size
        first = ""
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                s = line.strip().lstrip("#").strip()
                if s:
                    first = s[:60]
                    break
        except Exception:
            pass
        lines.append(f"- {rel}  ({n} B)  {first}")
    lines.append("\n用 world_read(\"<相对路径>\") 读取，"
                 "必要时带 start/end 行号范围。")
    return "\n".join(lines)


def tool_world_search(query: str, world: str = "", limit: int = 5) -> str:
    """关键词检索，返回命中文件 + 片段 + 行号（让模型先判断相关性）。"""
    query = (query or "").strip()
    if not query:
        return "请给出要检索的关键词。"
    limit = max(1, min(int(limit or 5), 20))
    targets = [world] if world else list_worlds()
    if not targets:
        return "（没有可检索的世界观资料）"

    terms = [t for t in re.split(r"\s+", query) if t]
    hits: list[tuple[int, str, int, str]] = []      # (得分, 相对路径, 行号, 片段)
    for w in targets:
        for f in world_files(w):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            rel = f.relative_to(worlds_root()).as_posix()
            lines = text.splitlines()
            for i, line in enumerate(lines, 1):
                score = sum(1 for t in terms if t in line)
                if score == 0:
                    continue
                # 标题命中加权
                if line.lstrip().startswith("#"):
                    score += 2
                snippet = line.strip()[:MAX_SNIPPET]
                hits.append((score, rel, i, snippet))

    if not hits:
        return f"没找到与「{query}」相关的内容。"
    hits.sort(key=lambda h: (-h[0], h[1], h[2]))
    shown = hits[:limit]
    out = [f"「{query}」命中 {len(hits)} 处，显示前 {len(shown)} 处："]
    for _score, rel, lineno, snippet in shown:
        out.append(f"- {rel}:{lineno}\n    {snippet}")
    out.append("\n用 world_read(\"<路径>\", start=行号-5, end=行号+20) 读上下文。")
    return "\n".join(out)


def tool_world_read(path: str, start: int = 1, end: int = 0) -> str:
    """读某个世界观文件，**支持行范围**。

    行范围是必须的：否则这个工具只是把"整份读入"从注入换了个位置，
    没解决上下文膨胀问题（方案 9.9.2）。
    """
    p = resolve_world_path(path)
    try:
        all_lines = p.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        raise WorldError(f"读取失败: {e!r}") from e

    total = len(all_lines)
    start = max(1, int(start or 1))
    end = int(end or 0)
    if end <= 0:
        end = min(total, start + MAX_READ_LINES - 1)
    end = min(end, total)
    if start > total:
        return f"{path} 只有 {total} 行，起点 {start} 超出范围。"

    chunk = all_lines[start - 1:end]
    truncated = ""
    if end < total:
        truncated = (f"\n…（已截断，全文共 {total} 行；"
                     f"继续读请用 start={end + 1}）")
    header = f"{path} 第 {start}-{end} 行 / 共 {total} 行：\n"
    body = "\n".join(f"{start + i:4d}| {ln}" for i, ln in enumerate(chunk))
    return header + body + truncated


# ---------------------------------------------------------------- 每轮提示

WORLD_HINT_LINE = (
    "# 世界观资料\n"
    "需要设定细节时用 world_search 检索、world_read 读取，不要凭印象编造。"
)


def has_any_world_data() -> bool:
    return any(world_files(w) for w in list_worlds())


@dataclass
class WorldStats:
    worlds: int
    files: int
    bytes: int

    def text(self) -> str:
        return f"{self.worlds} 个世界 / {self.files} 个文件 / {self.bytes} B"


def stats() -> WorldStats:
    ws = list_worlds()
    files = [f for w in ws for f in world_files(w)]
    return WorldStats(len(ws), len(files), sum(f.stat().st_size for f in files))
