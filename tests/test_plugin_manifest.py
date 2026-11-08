# -*- coding: utf-8 -*-
"""插件清单守卫：有 main.py 的插件目录必须有**被框架认可**的 metadata.yaml。

事故
----
`astrbot_plugin_ccn_debug` 写好了 `main.py` / `_conf_schema.json`，
但漏了 `metadata.yaml` —— AstrBot 的 `_load_plugin_metadata()` 返回 None，
插件**根本不会被注册**（插件列表里看不到），而 import 与语法检查全过。

这里直接调**框架自己的**校验器，而不是自己写一套 YAML 规则 ——
否则框架改了要求我们不会知道。
"""
from __future__ import annotations

import sys
from pathlib import Path
import _env  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN.parent

_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))


def _framework():
    """拿到框架的元数据文件名清单与校验器。"""
    try:
        from astrbot.core.star.star_manager import PluginManager
        from astrbot.core.star.updater import (
            PLUGIN_METADATA_FILENAMES, _PluginUpdater)
        # 取的是 `_PluginUpdater` 上的 **staticmethod**，不是类本身 ——
        # 直接返回类的话，下面的 `validate(md, name)` 会变成构造调用。
        return (PluginManager, PLUGIN_METADATA_FILENAMES,
                _PluginUpdater.validate_plugin_metadata)
    except Exception:
        return None, ("metadata.yaml", "metadata.yml"), None


def test_own_metadata_is_valid():
    """本插件自己的 metadata.yaml 必须存在且通过框架校验。"""
    PluginManager, names, validate = _framework()
    found = next((PLUGIN / n for n in names if (PLUGIN / n).is_file()), None)
    assert found is not None, (
        f"{PLUGIN.name} 缺少元数据文件（框架认这几种：{names}）→ "
        f"AstrBot 不会注册这个插件")
    if PluginManager is None:
        from run_tests import Skip
        raise Skip("拿不到框架的加载器（不在 AstrBot 环境里）")
    import yaml
    md = yaml.safe_load(found.read_text(encoding="utf-8"))
    assert isinstance(md, dict), f"{found.name} 不是合法 YAML 映射"
    if "desc" not in md and "description" in md:
        md["desc"] = md["description"]
    if validate is not None:
        validate(md, found.name)          # 框架自己的必填项校验
    meta = PluginManager._load_plugin_metadata(str(PLUGIN))
    assert meta is not None, "框架加载元数据返回 None → 插件不会被注册"
    for field in ("name", "author", "version", "desc"):
        assert getattr(meta, field, None), f"元数据缺少 {field}"


def test_every_sibling_plugin_has_metadata():
    """同级目录里所有带 main.py 的插件都必须有元数据文件。

    这条能拦住"新写一个插件忘了放 metadata.yaml"。
    非本仓库维护的插件（如框架自带的）也一并检查 —— 它们本来就都有。
    """
    if not PLUGINS_DIR.is_dir():
        from run_tests import Skip
        raise Skip("找不到插件目录")
    _, names, _ = _framework()
    missing = []
    for d in sorted(PLUGINS_DIR.iterdir()):
        if not d.is_dir():
            continue
        if not ((d / "main.py").is_file() or (d / f"{d.name}.py").is_file()):
            continue                       # 框架也不会扫它
        if not any((d / n).is_file() for n in names):
            missing.append(d.name)
    assert not missing, (
        "以下插件目录有 main.py 但没有元数据文件，AstrBot 不会注册它们：\n  "
        + "\n  ".join(missing))


def test_debug_plugin_metadata_matches_dir_name():
    """元数据里的 name 应与目录名一致（框架用它做 star 名查找）。"""
    d = PLUGINS_DIR / "astrbot_plugin_ccn_debug"
    f = d / "metadata.yaml"
    if not f.is_file():
        from run_tests import Skip
        raise Skip("调试插件未安装")
    import yaml
    md = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert md.get("name") == d.name, (
        f"metadata.name={md.get('name')!r} 与目录名 {d.name!r} 不一致")
