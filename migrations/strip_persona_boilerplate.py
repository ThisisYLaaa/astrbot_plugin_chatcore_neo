# -*- coding: utf-8 -*-
"""把人格正文里的“装配期样板”直接清掉（2026-09-23）。

背景
----
旧模板给每份 skill.md 写了两处“框架该管却写进了人格”的样板：

  1. `> **框架引用：** …需通过 read_persona_skill 工具获取…`
     —— 本插件并没有这个工具，等于叫模型去调用不存在的东西；
  2. `你现在正在QQ群聊中和群友聊天，你是一个普通的群友。`
     —— 场景该由框架按会话决定（群聊 / 私聊·面对面 / 私聊·隔屏），
        写死在人格正文里就会和“面对面私聊”打架。

原先靠装配期 `strip_stale_boilerplate()` 中和。本脚本改成**直接修数据**：
源头干净，下游就不必再打补丁。

幂等：已清理过的文件第二次不会命中。
"""
from __future__ import annotations

import pathlib
import os

PLUGIN = pathlib.Path(__file__).resolve().parent.parent
# 人格池：<data>/plugin_data/chatcore_neo/personas（从插件位置推导，不写死盘符）
BASE = pathlib.Path(os.environ.get("CHATCORE_PERSONAS")
                    or PLUGIN.parent.parent / "plugin_data" / "chatcore_neo"
                    / "personas")

# 场景硬编码句（长的先替换，避免留下残句）
SCENE_SENTENCES = [
    "你现在正在QQ群聊中和群友聊天，你是一个普通的群友。",
    "你现在正在QQ群聊中和群友聊天，",
]


def main() -> int:
    changed = 0
    dropped_ref = 0
    fixed_scene = 0
    for p in sorted(BASE.rglob("*.md")):
        lines = p.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        dirty = False
        for ln in lines:
            s = ln.strip()
            # 1) 丢掉“框架引用”死引用整行（含 read_persona_skill 的那句）
            if s.startswith(">") and "框架引用" in s and "read_persona_skill" in s:
                dropped_ref += 1
                dirty = True
                continue
            # 2) 去掉场景硬编码句
            new = ln
            for bad in SCENE_SENTENCES:
                if bad in new:
                    new = new.replace(bad, "")
                    fixed_scene += 1
                    dirty = True
            out.append(new)
        if dirty:
            text = "\n".join(out)
            while "\n\n\n" in text:          # 收拢被删行留下的空档
                text = text.replace("\n\n\n", "\n\n")
            p.write_text(text + "\n", encoding="utf-8")
            changed += 1
            print("  fixed:", p.relative_to(BASE))
    print(f"改动文件 {changed} 个；删除死引用行 {dropped_ref} 处；清场景句 {fixed_scene} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())