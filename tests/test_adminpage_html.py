# -*- coding: utf-8 -*-
"""把「HTML 注入」这一课固化成测试。

事故复盘
--------
我在 JS 注释里写了字面量 `</body>`。AstrBot 注入插件页桥的方式是：

    html.replace("</body>", f"{bridge_tag}</body>", 1)

`str.replace(..., 1)` 替换的是**第一个** `</body>` —— 正好是注释里那个。
于是 `<script src="...bridge-sdk.js"></script>` 被塞进了我的脚本块内部，
它自带的 `</script>` 把我的脚本**提前关闭**，后面所有 JS 都变成页面文本，
看起来就是「整个 WebUI 崩坏、页面显示源代码」。

教训：内联 `<script>` 里**不能出现任何 `</body>` / `</html>` / `</script` 字面量**，
因为它们会参与服务端的字符串替换与 HTML 解析。注释里也不例外。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAGE = (Path(__file__).resolve().parent.parent
        / "pages" / "admin" / "index.html")

# AstrBot 实际使用的注入片段（占位 URL）
BRIDGE_TAG = '<script src="/api/plugin/page/bridge-sdk.js"></script>'


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _script_body(s: str) -> str:
    i = s.find("<script")
    j = s.find("</script>", i)
    assert i >= 0 and j > i, "页面应有一个完整的 script 块"
    return s[i:j]


def test_exactly_one_body_and_html_close():
    """`</body>` / `</html>` 必须唯一。

    多于一个时，AstrBot 的 `replace(..., 1)` 会命中错误的那一个。
    """
    s = _page()
    assert s.count("</body>") == 1, (
        f"</body> 出现 {s.count('</body>')} 次；"
        "多出来的那个会抢走桥脚本注入点（注释里也不行）")
    assert s.count("</html>") == 1, f"</html> 出现 {s.count('</html>')} 次"


def test_script_body_has_no_html_close_tags():
    """脚本块内不得出现 `</body` / `</html` / `</script` 字面量。"""
    body = _script_body(_page())
    bad = []
    for pat in ("</body", "</html", "</script"):
        for m in re.finditer(re.escape(pat), body, re.IGNORECASE):
            ln = body[:m.start()].count("\n") + 1
            bad.append((pat, ln))
    assert not bad, (
        "内联脚本里出现了会破坏 HTML 解析的字面量标签 "
        f"(相对脚本起始的行号): {bad}\n"
        "注释里也不行 —— AstrBot 用字符串替换注入桥脚本。")


def test_bridge_injection_lands_outside_page_script():
    """模拟 AstrBot 的注入，断言桥脚本没有落进页面脚本内部。

    这是对事故的**直接回归测试**：如果桥的 `<script>` 落在页面脚本内部，
    它自带的 `</script>` 就会提前关闭页面脚本。
    """
    s = _page()
    injected = s.replace("</body>", f"{BRIDGE_TAG}</body>", 1)

    # 页面脚本块（注入后）应当是完整的：从 <script> 到紧随其后的 </script>
    first = injected.find("<script")
    first_end = injected.find("</script>", first)
    page_script_len = len(_script_body(s))
    actual_len = first_end - first
    assert abs(actual_len - page_script_len) <= 2, (
        f"注入后第一个 script 块长度 {actual_len}，页面脚本长度 {page_script_len} —— "
        "说明桥脚本被塞进了页面脚本内部")

    # 桥脚本必须落在页面脚本结束之后
    assert first_end < injected.find("bridge-sdk.js"), \
        "桥脚本应该在页面脚本之后注入"
    assert injected.count("bridge-sdk.js") == 1


def test_script_body_has_no_double_dash_comment_soup():
    """`<!--` / `-->` 落在脚本块内会把解析器带进 escaped 状态。

    它们只应出现在 HTML 注释里（脚本块之外）。
    """
    body = _script_body(_page())
    assert "<!--" not in body, "脚本块内不应出现 <!--"
    assert "-->" not in body, "脚本块内不应出现 -->"


def test_page_javascript_parses():
    """用 node --check 验证内联 JS 语法（node 不可用时跳过）。"""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("node"):
        from run_tests import Skip
        raise Skip("未安装 node")

    code = _script_body(_page())
    code = code[code.find(">") + 1:]          # 去掉 <script>
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(code)
        tmp = f.name
    try:
        r = subprocess.run(["node", "--check", tmp],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"内联 JS 语法错误:\n{r.stderr[:600]}"
    finally:
        Path(tmp).unlink()
