# -*- coding: utf-8 -*-
"""pytest 适配层：把本套件自带的 ``run_tests.Skip`` 当成 skip。

本套测试原本只配 ``tests/run_tests.py``（纯标准库运行器，不要求装 pytest）。
用例里用 ``raise Skip("环境不满足")`` 表示"这条跑不了，不算失败"——
pytest 不认识这个自定义异常，会把它算成 failure。这里做一层薄适配，
让两种跑法给出同一个结论：

    python tests/run_tests.py       # 官方跑法（多进程，最快）
    python -m pytest tests -q       # 也等价

不改任何用例、不改 ``run_tests.Skip`` 的语义。
"""
from __future__ import annotations

import pytest

try:  # tests/ 目录由 pytest 的 rootdir 插入 sys.path，正常都能 import 到
    from run_tests import Skip
except Exception:  # pragma: no cover - 极端情况下退化成"没有 Skip 语义"
    Skip = None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    excinfo = call.excinfo
    if Skip is not None and excinfo is not None and excinfo.errisinstance(Skip):
        rep.outcome = "skipped"
        rep.longrepr = (
            str(item.path),
            None,
            f"Skipped: {str(excinfo.value) or '环境不满足'}",
        )
