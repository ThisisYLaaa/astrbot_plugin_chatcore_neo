# -*- coding: utf-8 -*-
"""接线一致性：main.py 里引用的每个属性/方法都必须真实存在。

为什么需要它
------------
这个插件反复出现"**能 import、能通过单元测试，但一跑到某行就 AttributeError
或静默不生效**"的问题。原因是测试直接调下层函数，从没碰过 `main.py` 的接线。

本测试用 `FakePlugin`（与 `main.py:__init__` 同构）做静态核对：
  * `self.X` 是否真在 `__init__` 里赋过值
  * `self.X.Y(...)` 里 `Y` 是否真的存在于 X 上

它能抓到的真实 bug 类型：
  * 属性名拼错（`self.poke` vs `self.pokee`）
  * 在 `__init__` 里忘了构造某个对象
  * 调了某个下层对象不存在的方法（例如 `plugin.memory.forget` 写成了 `forget_all`）
  * **测试替身太薄**（FakePlugin 缺属性 → 测试通过但生产代码会崩）

最后一类正是发现本测试的契机：FakePlugin 最初漏了 `assembler` /
`_session_persona` / `_active_triggers` / `_mem_tasks` / `_intent_last` /
`_initialized` / `web_paths`，静态核对一次报出 9 处。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_admin_api import _plugin  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
MAIN = PLUGIN / "main.py"

# FakePlugin 刻意不构造的对象（None 或与真实不同）
# `_p` 是 `main.py:116 class _MemProxy` 的 `__slots__`，属于**那个代理类**的
# self，不是插件实例的属性 —— 静态扫描会把 `self._p.mem()` 误算到插件头上。
SKIP_ATTR = {"context", "_p"}


def _self_calls(tree: ast.AST) -> list[tuple[int, str, str]]:
    """收集 `self.X.Y(...)` 形式的调用 → (行号, X, Y)。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute):
            continue
        inner = f.value
        if not isinstance(inner, ast.Attribute):
            continue
        if isinstance(inner.value, ast.Name) and inner.value.id == "self":
            out.append((node.lineno, inner.attr, f.attr))
    return out


def test_main_self_attributes_and_methods_exist():
    """main.py 里 self.X.Y(...) 的 X 与 Y 都必须真实存在。"""
    plugin = _plugin()
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))

    missing_attr: list[str] = []
    missing_method: list[str] = []
    for lineno, attr, method in _self_calls(tree):
        if attr in SKIP_ATTR:
            continue
        if not hasattr(plugin, attr):
            missing_attr.append(f"main.py:{lineno} self.{attr} 不存在（调用 .{method}）")
            continue
        obj = getattr(plugin, attr)
        if obj is None:
            continue
        if not hasattr(obj, method):
            missing_method.append(
                f"main.py:{lineno} self.{attr}({type(obj).__name__}) 没有 .{method}()")

    assert not missing_attr, (
        "main.py 用到了 __init__ 里没构造的属性（属性名拼错或忘了构造）:\n  "
        + "\n  ".join(sorted(set(missing_attr))))
    assert not missing_method, (
        "main.py 调用了下层对象不存在的方法:\n  "
        + "\n  ".join(sorted(set(missing_method))))


def test_fake_plugin_mirrors_init():
    """FakePlugin 必须覆盖 main.py 里实际用到的全部 self.X。

    这个断言的作用是**防止测试替身变薄**：替身缺属性时，
    别的测试仍然全绿，但生产代码会在那一行崩。
    """
    plugin = _plugin()
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    used = {a for _l, a, _m in _self_calls(tree)} | {
        "config", "_stats", "_last_error", "personas"}
    gaps = sorted(a for a in used if a not in SKIP_ATTR and not hasattr(plugin, a))
    assert not gaps, (
        f"FakePlugin 缺少 main.py 用到的属性: {gaps}\n"
        "盲点：这些属性上的 bug 不会被任何测试发现。")


def test_no_attribute_typo_in_core_modules():
    """core/** 里 `self.X(...)` 调用的方法必须在同类上存在。

    只查"在同一文件里给 self 赋过值"的对象，避免误报。
    """
    problems: list[str] = []
    for f in sorted((PLUGIN / "core").rglob("*.py")):
        if "__pycache__" in str(f):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            defined = {
                n.name for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            # 收集本类里 self.x = ... 赋值的名字（含 __init__）
            assigned: set[str] = set()
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == "self"):
                            assigned.add(t.attr)
                if isinstance(node, ast.AnnAssign):
                    t = node.target
                    if (isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        assigned.add(t.attr)
            # self.<name>() 调用：name 既不是本类方法也不是本类属性 → 可疑
            for node in ast.walk(cls):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if (isinstance(fn, ast.Attribute)
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id == "self"):
                    nm = fn.attr
                    if nm not in defined and nm not in assigned:
                        # 可能是继承来的；只在明显可疑时报告
                        base_names = {b.id for b in cls.bases if isinstance(b, ast.Name)}
                        if base_names:
                            continue
                        problems.append(
                            f"{f.relative_to(PLUGIN).as_posix()}:{node.lineno} "
                            f"{cls.name}.self.{nm}() 未定义")
    assert not problems, "可疑的 self 调用:\n  " + "\n  ".join(problems)
