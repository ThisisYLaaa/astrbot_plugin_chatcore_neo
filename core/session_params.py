# -*- coding: utf-8 -*-
"""会话级发言参数：发言模式 + 额外规则集的解析与归一化。

数据放在 `state/_session_params.json`（JsonMap，键 = UMO）：

    {"<umo>": {"mode": 1|2, "rules": ["nsfw"]}}

* `mode`：1 = 剧情向（括号描写）；2 = 纯信息（无括号）。缺省 = 按会话类型默认。
* `rules`：额外规则列表（当前仅 "nsfw"）。缺省 = 按会话类型默认；[] = 明确全关。

解析规则（`resolve`）
----------------------
* 私聊默认：mode = 全局 `private_msg_mode`，nsfw = 开
* 群聊默认：mode = 2（纯信息），nsfw = 关
* 显式覆盖优先于默认；`rules` 一旦显式给出（含空数组）就整体覆盖默认集。

本模块可脱离框架单测，不 import 任何 astrbot 依赖。
"""
from __future__ import annotations

from typing import Any

# 合法发言模式
MODE_NARRATIVE = 1          # 剧情向（括号描写）
MODE_PLAIN = 2              # 纯信息（无括号）

# 已知的额外规则 id（未来扩展时在这里登记，WebUI 会读取这个常量）
RULE_IDS: tuple[str, ...] = ("nsfw",)
RULE_LABELS: dict[str, str] = {"nsfw": "NSFW"}

MODE_LABELS: dict[int, str] = {
    MODE_NARRATIVE: "剧情向（括号描写）",
    MODE_PLAIN: "纯信息（无括号）",
}


def normalize_override(values: dict | None) -> dict:
    """把外部传入的覆盖参数归一化：只保留合法键、转成正确类型。

    非法值直接丢弃（null/空串 = 该键未设置，继承默认）。
    """
    out: dict = {}
    if not isinstance(values, dict):
        return out
    m = values.get("mode")
    if m in (MODE_NARRATIVE, MODE_PLAIN):
        out["mode"] = int(m)
    r = values.get("rules")
    if r is None or r == "":
        pass                            # 未设置，继承默认
    elif isinstance(r, (list, tuple, set)):
        ids = [str(x).strip() for x in r]

        def _ok(x: str):
            return x in RULE_IDS

        out["rules"] = [x for x in ids if _ok(x)]
    return out


def resolve(is_private: bool, override: dict | None = None,
            global_mode: int | None = None) -> tuple[int, bool]:
    """求某会话的最终生效 (发言模式, 是否启用 nsfw)。

    `override` 为 `_session_params.json` 里该会话的值（已归一化或原始 dict）；
    `global_mode` 为配置文件里的 `private_msg_mode`（私聊默认发言模式）。
    """
    ov = override or {}
    if is_private:
        eff_mode = int(ov.get("mode") or global_mode or MODE_NARRATIVE)
        rules = ov.get("rules")
        eff_nsfw = ("nsfw" in rules) if rules is not None else True
    else:
        eff_mode = int(ov.get("mode") or MODE_PLAIN)
        rules = ov.get("rules")
        eff_nsfw = ("nsfw" in rules) if rules is not None else False
    if eff_mode not in (MODE_NARRATIVE, MODE_PLAIN):
        eff_mode = MODE_NARRATIVE if is_private else MODE_PLAIN
    return eff_mode, bool(eff_nsfw)


def describe(is_private: bool, override: dict | None = None,
             global_mode: int | None = None) -> dict:
    """给 WebUI 用：显式覆盖 + 解析后生效值 + 是否继承。"""
    mode, nsfw = resolve(is_private, override, global_mode)
    ov = override or {}
    return {
        "override_mode": ov.get("mode"),
        "override_rules": ov.get("rules"),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, str(mode)),
        "nsfw": nsfw,
        "inherited": not (ov.get("mode") or ov.get("rules") is not None),
    }