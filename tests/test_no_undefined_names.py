# -*- coding: utf-8 -*-
"""静态检查：不许出现"用到但没定义"的裸名字（NameError 类）。

为什么需要它
------------
`/persona new` 从写下那天起就调用 `_parse_opts(args[2:])`，而**这个函数从未被定义** ——
命令抛 NameError、用户零回复。`py_compile` 抓不到（语法没错），
`test_plugin_wiring` 也抓不到（它只查 `self.X.Y` 形式）。

同一轮里这个检查还抓到了第二处：`embedder.py` 引用了已被删除的
`EMBED_CACHE_PATH`（缓存读写会 NameError）。

实现：把模块里所有"定义"（import / def / class / 赋值 / 参数 / except 名 /
global / 内置）收集起来，再和其它所有 Name 加载点求差。
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PLUGIN = Path(__file__).resolve().parent.parent

# 解释器注入的模块级名字
MODULE_GLOBALS = {"__file__", "__name__", "__doc__", "__package__",
                  "__spec__", "__loader__", "__builtins__", "__debug__"}


def _defined(tree: ast.AST) -> set[str]:
    out = set(dir(builtins)) | MODULE_GLOBALS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global):
            out.update(node.names)
        elif isinstance(node, (ast.comprehension,)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    out.add(n.id)
    return out


def test_no_undefined_names_in_runtime_code():
    """运行时代码里不得有未定义的裸名字。"""
    bad: dict[str, list[str]] = {}
    for f in sorted(PLUGIN.rglob("*.py")):
        rel = f.relative_to(PLUGIN)
        if "__pycache__" in str(f) or rel.parts[0] in ("tests", "migrations"):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        defined = _defined(tree)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        miss = sorted(used - defined)
        if miss:
            bad[rel.as_posix()] = miss
    assert not bad, (
        "以下文件用到了没有定义的名字（运行时会 NameError）:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in bad.items()))


def test_parse_opts_actually_exists():
    """`/persona new` 依赖 `_parse_opts` —— 它曾经只被调用、从未被定义。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    if "_parse_opts(" not in src:
        from run_tests import Skip
        raise Skip("该命令已移除")
    assert "def _parse_opts(" in src, \
        "main.py 调用了 _parse_opts 但没有定义它"
