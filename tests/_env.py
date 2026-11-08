# -*- coding: utf-8 -*-
"""测试辅助：解析本机 AstrBot 安装位置，避免在仓库里写死绝对路径。

插件被安装成
    <AstrBot>/Astrbot_Env/<env>/data/plugins/astrbot_plugin_chatcore_neo
时，所有目录都能从插件自身位置推出来，不需要记住盘符。装法不同（自定义目录、
软链、CI 里只有仓库）时，用环境变量覆盖：

    ASTRBOT_APP          框架的 backend/app 目录（导入 astrbot.* 用）
    ASTRBOT_DATA         插件所在 AstrBot 环境的 data 目录
    ASTRBOT_LEGACY_DATA  迁移来源的旧环境 data 目录（默认不猜，找不到就让用例 skip）

解析不到时返回 ``MISSING``（一个相对路径），调用方照旧走 ``if X.is_dir()`` /
``raise Skip(...)`` 分支，测试不会因为"这台机器没装 AstrBot"而红。
"""
from __future__ import annotations

import os
from pathlib import Path

MISSING = Path("__missing__")

PLUGIN = Path(__file__).resolve().parent.parent

# 常见安装位置里 AstrBot 目录的名字（Windows 下再拼 Program Files）
_INSTALL_DIR_NAMES = ("AstrBot", "astrbot")
_WIN_PREFIXES = (r"\Program Files", r"\Program Files (x86)", "")


def _from_env(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def _drive_letters() -> list[str]:
    if os.name != "nt":
        return []
    return [f"{c}:" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"]


def _install_candidates():
    """可能的 AstrBot 安装根（只是候选，逐个 is_dir 验证）。"""
    seen: list[Path] = []
    # 1) 顺着插件位置向上找（插件在 <AstrBot>/Astrbot_Env/<env>/data/plugins 时命中）
    for anc in (PLUGIN, *PLUGIN.parents):
        seen.append(anc)
        seen.extend(anc / n for n in _INSTALL_DIR_NAMES)
    # 2) 用户主目录
    seen.extend(Path.home() / n for n in _INSTALL_DIR_NAMES)
    # 3) 各盘符下的常见安装位置（Windows）；POSIX 用 /opt 与 /usr/local
    for drive in _drive_letters():
        for prefix in _WIN_PREFIXES:
            base = Path(drive + prefix)
            seen.extend(base / n for n in _INSTALL_DIR_NAMES)
    for base in (Path("/opt"), Path("/usr/local"), Path("/srv")):
        seen.extend(base / n for n in _INSTALL_DIR_NAMES)
    return seen


def app() -> Path:
    """AstrBot 框架代码目录（``<AstrBot>/backend/app``）。"""
    env = _from_env("ASTRBOT_APP")
    if env is not None:
        return env
    for root in _install_candidates():
        cand = root / "backend" / "app"
        if (cand / "astrbot").is_dir():
            return cand
    return MISSING


def _installed_env_data() -> Path | None:
    """在 AstrBot 安装目录里找"本插件的已安装副本"所在的 data 目录。

    在仓库里跑测试时（插件目录不在 `<data>/plugins` 下），部分用例仍想读**真实
    安装**的配置/数据；找不到就返回 None，用例走自己的跳过分支。
    """
    app_dir = app()
    if app_dir is MISSING:
        return None
    astrbot_root = app_dir.parent.parent          # <AstrBot>/backend/app -> <AstrBot>
    for cand in sorted(astrbot_root.glob("Astrbot_Env/*/data")):
        cfg = cand / "config" / "astrbot_plugin_chatcore_neo_config.json"
        if cfg.is_file() or (cand / "plugins" / PLUGIN.name).is_dir():
            return cand
    return None


def data() -> Path:
    """插件所在 AstrBot 环境的 ``data`` 目录。"""
    env = _from_env("ASTRBOT_DATA")
    if env is not None:
        return env
    parent = PLUGIN.parent
    if (parent / "plugins").is_dir():
        return parent
    installed = _installed_env_data()
    return installed if installed is not None else MISSING


def plugin_root() -> Path:
    """本插件数据根 ``<data>/plugin_data/chatcore_neo``。"""
    d = data()
    return MISSING if d is MISSING else d / "plugin_data" / "chatcore_neo"


def plugin_config() -> Path:
    """``<data>/config/astrbot_plugin_chatcore_neo_config.json``。"""
    d = data()
    if d is MISSING:
        return MISSING
    return d / "config" / "astrbot_plugin_chatcore_neo_config.json"


def legacy_data() -> Path:
    """迁移来源的旧环境 ``data`` 目录。

    默认**不猜**（旧环境叫 ``.astrbot`` 还是 ``obslete-.astrbot`` 因机器而异，
    猜错会让"跳过"的用例突然真的跑起来），需要时设 ``ASTRBOT_LEGACY_DATA``。
    """
    return _from_env("ASTRBOT_LEGACY_DATA") or MISSING


def legacy_skills() -> Path:
    """旧环境技能树 ``<legacy_data>/skills``。"""
    d = legacy_data()
    return MISSING if d is MISSING else d / "skills"
