# -*- coding: utf-8 -*-
"""静态检查：页面引用的元素 id 必须真的存在（否则按钮点了没反应）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAGE = (Path(__file__).resolve().parent.parent
        / "pages" / "admin" / "index.html")


def test_all_referenced_ids_exist():
    """`on("x")` / `getElementById("x")` / `val("x")` 引用的 id 必须存在。

    这是"按钮点了没反应"最隐蔽的一种成因：id 拼错或忘了写到 innerHTML 里，
    JS 里 `getElementById` 返回 null，`on()` 直接静默不绑定。
    """
    s = PAGE.read_text(encoding="utf-8")

    # 所有出现的 id="..."（静态 HTML 与 JS 字符串里都算）
    defined = set(re.findall(r'id="([A-Za-z][\w-]*)"', s))
    defined |= set(re.findall(r"id=\\?\"([A-Za-z][\w-]*)\\?\"", s))

    referenced: set[str] = set()
    for pat in (r'on\(\s*"([A-Za-z][\w-]*)"',
                r'getElementById\(\s*"([A-Za-z][\w-]*)"',
                r'val\(\s*"([A-Za-z][\w-]*)"',
                r'querySelector\(\s*"#([A-Za-z][\w-]*)"'):
        referenced |= set(re.findall(pat, s))

    # 排除动态拼接的前缀（如 getElementById("tab-" + id)）
    referenced = {r for r in referenced if not r.endswith("-")}

    missing = sorted(referenced - defined)
    assert not missing, (
        "页面引用了不存在的元素 id（按钮会静默失效）:\n  "
        + "\n  ".join(missing))


def test_every_bound_button_has_a_handler():
    """每个在 HTML 里出现的 <button id=...> 都应被 on() 绑定或被 onclick 覆盖。"""
    s = PAGE.read_text(encoding="utf-8")
    html_only = re.sub(r"<script[^>]*>.*?</script>", "", s, flags=re.DOTALL)
    ids = re.findall(r'<button[^>]*\bid="([A-Za-z][\w-]*)"', html_only)
    assert ids, "静态按钮应带 id"
    for bid in ids:
        bound = (f'on("{bid}"' in s) or (f'getElementById("{bid}")' in s)
        assert bound, f"按钮 #{bid} 没有任何 JS 绑定（点了没反应）"


def test_tab_switching_has_loader_for_every_tab():
    """（重复强调）没有 LOADER 的页签会表现为空白，不是"没反应"而是"没内容"。"""
    code = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                      PAGE.read_text(encoding="utf-8"), re.DOTALL)[0]
    # 页签定义是三个字符串的数组：["id", "标签", "说明"]
    # 用三元素形式匹配，避免把 ["embedding", ...] 这类 kv 数组也算进来。
    # id 允许连字符（session-params）—— 旧正则不认，把它整条漏掉了。
    body = re.search(r"const TABS = \[(.*?)\n\];", code, re.S).group(1)
    tabs = re.findall(r'\[\s*"([a-z][a-z0-9_-]*)"\s*,\s*"[^"]*"\s*,\s*"', body)
    loaders: set = set()
    for m in re.finditer(
            r'LOADERS(?:\.([A-Za-z0-9_]+)|\[\s*"([A-Za-z0-9_-]+)"\s*\])\s*=',
            code):
        loaders.add(m.group(1) or m.group(2))
    for t in tabs:
        assert t in loaders, f"页签 {t} 没有 LOADERS 实现"
