# -*- coding: utf-8 -*-
"""多人格同台：会话级「额外登场人格」（副人格）。

语义
----
一个会话除了主角色，还能让若干**副人格**同时登场，由主模型一并扮演。
与旧 lili_state 的「多人同台」同源，但按本插件结构重写：

* **仅在剧情模式下生效**：会话生效发言模式为 `MODE_NARRATIVE`（见
  `core/session_params.py`）时才注入；切到「纯信息」自动静默、配置保留。
  闸门在 `main.on_llm_request`，本模块只负责存取与渲染。
* **写法纪律**：副人格规则每轮以 `extra_user_content_parts + mark_as_temp()`
  注入**动态段**（`PromptAssembler` 的 `costars` 分区）——绝不进
  `req.contexts`、也不进 `system_prompt` 前缀，因此不落对话记录、不破坏缓存。
* **不重复框架**：主装配已注入一份通用框架，副人格只渲染自身的
  「人格正文 + 说话习惯摘要」（`include_framework=False`）。

数据：`state/_session_costars.json`，`{"<umo>": ["<persona_id>", ...]}`。
"""
from __future__ import annotations

from typing import Any, Iterable

from .paths import STATE_DIR
from .persona import render as R
from .sessionmap import JsonMap

H_COSTARS = "# 多人格同台"

# 配置 `costar_max` 之外的硬上限兜底（防止坏配置把上下文撑爆）
HARD_MAX = 8


def _norm(pids: Iterable[Any] | None) -> list[str]:
    """去重 + 去空 + 保序。"""
    out: list[str] = []
    for p in pids or []:
        s = str(p or "").strip()
        if s and s not in out:
            out.append(s)
    return out


class CoStarStore:
    """会话 → 副人格 id 列表（落盘即原子写）。"""

    def __init__(self, path=None) -> None:
        self.map = JsonMap(path or (STATE_DIR / "_session_costars.json"))

    # ------------------------------------------------------------ 读
    def cast_of(self, umo: str) -> list[str]:
        try:
            v = self.map.get(umo)
        except Exception:
            return []
        return _norm(v) if isinstance(v, list) else []

    def all(self) -> dict[str, list[str]]:
        try:
            items = self.map.items()
        except Exception:
            return {}
        out: dict[str, list[str]] = {}
        for k, v in items:
            val = _norm(v) if isinstance(v, list) else []
            if val:
                out[str(k)] = val
        return out

    # ------------------------------------------------------------ 写
    def set(self, umo: str, pids: Iterable[Any] | None) -> list[str]:
        val = _norm(pids)
        if val:
            self.map.set(umo, val)
        else:
            self.map.pop(umo)
        return val

    def add(self, umo: str, pid: str) -> list[str]:
        return self.set(umo, [*self.cast_of(umo), pid])

    def remove(self, umo: str, pid: str) -> list[str]:
        return self.set(umo, [p for p in self.cast_of(umo) if p != pid])

    def clear(self, umo: str) -> list[str]:
        return self.set(umo, [])


# ---------------------------------------------------------------- 渲染

def build_block(main_doc: Any, costar_docs: Iterable[Any] | None, *,
                msg_mode: int = 1, is_private: bool = True) -> str:
    """渲染同台动态段正文；无副人格时返回空串（=不注入）。

    走 `build_l1(include_framework=False)` + `build_l2()`：只带副人格自身的
    正文/外貌与说话习惯，框架规则不再重复（主装配已注入一份）。
    """
    docs = [d for d in (costar_docs or []) if d is not None]
    if not docs or main_doc is None:
        return ""
    names = [str(getattr(d, "display_name", "") or d.id) for d in docs]
    ex = names[0]
    head = "\n".join([
        f"本次对话你**同时扮演**主角「{main_doc.display_name}」"
        f"和额外角色：{'、'.join(names)}。",
        "以下每个角色的设定各自生效；请区分各自的身份、口吻与称呼体系，"
        "不要在同一个角色的话里混入另一个角色。",
        "多人同台发言格式：**每条消息都以【角色名】开头**标明这句话是谁说的"
        f"（例如【{ex}】），不同角色各说各的、各自独立成条。",
    ])
    parts = [head]
    for d in docs:
        body = R.build_l1(d, include_framework=False,
                          is_private=is_private, msg_mode=msg_mode)
        l2 = R.build_l2(d)
        seg = "\n\n".join(x for x in (body, l2) if x and x.strip())
        if seg:
            parts.append(f"## 额外角色：{d.display_name}（{d.id}）\n\n{seg}")
    return "\n\n".join(p for p in parts if p and p.strip())
