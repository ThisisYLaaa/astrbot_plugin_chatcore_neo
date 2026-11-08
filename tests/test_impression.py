# -*- coding: utf-8 -*-
"""W4 印象总结测试：阈值触发 / 人工覆写保护 / 格式兼容 / fail-open。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.impression import (ImpressionSummarizer,  # noqa: E402
                             build_impression_prompt)
from core.state import StateStore  # noqa: E402


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _mk(cfg: dict | None = None, threshold: int = 20):
    root = Path(tempfile.mkdtemp())
    store = StateStore(root)
    s = ImpressionSummarizer(None, _Cfg(cfg or {}), store, threshold=threshold)
    return s, store


# ---------------------------------------------------------------- 阈值

def test_due_only_at_threshold_multiples():
    s, store = _mk(threshold=20)
    assert not s.due("u1", 5)
    assert not s.due("u1", 19)
    assert s.due("u1", 20)
    assert not s.due("u1", 21)
    assert s.due("u1", 40)


def test_due_matches_the_real_call_order():
    """回归：`on_llm_response` 是**先把计数 +1 落盘、再问要不要总结**。

    所以判据绝不能是"和文件里已存的数字比大小" —— 那个数字就是刚存进去的
    同一个值，差值恒为 0，印象一辈子都不会更新。
    （实测：生产日志里「印象总结·触发」0 次，而同一批加进去的
    「记忆总结」8 次。）
    """
    s, store = _mk(threshold=3)
    fired = []
    for _ in range(7):
        imp = store.load_impression("u1")
        imp["msg_count"] = int(imp.get("msg_count", 0) or 0) + 1
        store.save_impression("u1", imp)                  # ← 先落盘
        fired.append(s.due("u1", imp["msg_count"]))       # ← 再判定
    assert fired == [False, False, True, False, False, True, False], fired


def test_due_is_restart_safe():
    """计数持久化：重启后从已存轮数接着数，不会把同一批轮数再触发一遍。"""
    s, store = _mk(threshold=20)
    store.save_impression("u1", {"msg_count": 20, "sfw": "旧印象"})
    assert not s.due("u1", 21)          # 重启后第一轮
    assert not s.due("u1", 39)
    assert s.due("u1", 40)              # 下一个整数倍才触发


def test_due_rejects_zero_and_negative():
    """全新用户（0 轮）没有对话记录可总结，0 % N == 0 不能当成触发。"""
    s, _ = _mk(threshold=20)
    assert not s.due("u1", 0)
    assert not s.due("u1", -20)


def test_due_disabled_by_config():
    s, store = _mk({"enable_impression": False}, threshold=1)
    assert not s.due("u1", 100)


def test_due_rejects_empty_uid():
    s, _ = _mk(threshold=1)
    assert not s.due("", 100)


# ---------------------------------------------------------------- 提示词

def test_build_impression_prompt_uses_both_roles():
    log = [
        {"role": "user", "content": "我最近在打游戏"},
        {"role": "assistant", "lili_thought": "他说在打游戏"},
    ]
    txt = build_impression_prompt("香奈美", log)
    assert "用户：我最近在打游戏" in txt
    assert "香奈美：他说在打游戏" in txt


def test_build_impression_prompt_empty_log():
    assert "无对话记录" in build_impression_prompt("X", [])


def test_build_impression_prompt_truncates():
    log = [{"role": "user", "content": "啊" * 500} for _ in range(100)]
    txt = build_impression_prompt("X", log, limit=10)
    assert len(txt) < 3000, "必须截断，否则会把整个日志灌进摘要模型"


# ---------------------------------------------------------------- fail-open

def test_maybe_update_fails_open_without_provider():
    """**关键**：拿不到 provider 时不得更新、不得抛异常。"""
    s, store = _mk(threshold=1)
    ok = asyncio.run(s.maybe_update("umo", "u1", "香奈美",
                                    [{"role": "user", "content": "hi"}], 5))
    assert ok is False
    assert store.load_impression("u1").get("sfw") in (None, "")
    assert s.stats["checked"] == 1


def test_maybe_update_skips_below_threshold():
    s, _ = _mk(threshold=100)
    ok = asyncio.run(s.maybe_update("umo", "u1", "X", [], 3))
    assert ok is False
    assert s.stats["skipped"] == 1


# ---------------------------------------------------------------- 写入与保护

def test_update_preserves_custom_fields():
    """人工覆写位绝不能被自动总结覆盖。"""
    s, store = _mk(threshold=1)
    store.save_impression("u1", {
        "sfw": "旧的自动印象", "nsfw": "",
        "sfw_custom": "我手写的印象", "nsfw_custom": "手写私密",
        "updated": 0, "msg_count": 0,
    })

    # 打桩：让摘要始终成功，绕过 provider
    async def fake_ask(umo, model, system, user, timeout):
        return "自动生成的印象"

    s._ask = fake_ask
    ok = asyncio.run(s.maybe_update("umo", "u1", "香奈美",
                                    [{"role": "user", "content": "hi"}], 5))
    assert ok is True
    imp = store.load_impression("u1")
    assert imp["sfw"] == "自动生成的印象"
    assert imp["sfw_custom"] == "我手写的印象", "人工覆写被覆盖了"
    assert imp["nsfw_custom"] == "手写私密"
    assert imp["msg_count"] == 5
    assert imp["updated"] > 0


def test_update_writes_nsfw_and_clears_none_marker():
    s, store = _mk(threshold=1)
    calls = {"n": 0}

    async def fake_ask(umo, model, system, user, timeout):
        calls["n"] += 1
        return "无" if calls["n"] == 2 else "正常的 SFW 印象"

    s._ask = fake_ask
    asyncio.run(s.maybe_update("umo", "u1", "X",
                               [{"role": "user", "content": "hi"}], 5))
    imp = store.load_impression("u1")
    assert imp["sfw"] == "正常的 SFW 印象"
    assert imp.get("nsfw", "") == "", "NSFW 返回「无」时应存空串而不是「无」"


def test_empty_summary_does_not_overwrite():
    s, store = _mk(threshold=1)
    store.save_impression("u1", {"sfw": "原有印象", "msg_count": 0})

    async def fake_ask(umo, model, system, user, timeout):
        return ""      # 模型没吐东西

    s._ask = fake_ask
    ok = asyncio.run(s.maybe_update("umo", "u1", "X",
                                    [{"role": "user", "content": "hi"}], 5))
    assert ok is False
    assert store.load_impression("u1")["sfw"] == "原有印象", \
        "空结果不该把已有印象清掉"


def test_old_lilistate_impression_format_is_readable():
    """迁移过来的旧印象文件必须能直接读（字段形状未变）。"""
    s, store = _mk(threshold=1)
    store.save_impression("100000001", {
        "sfw": "", "nsfw": "", "sfw_custom": "主人，喜欢被叫哥哥",
        "nsfw_custom": "", "updated": 0, "msg_count": 1,
    })
    got = store.impression_text("100000001")
    assert got == "主人，喜欢被叫哥哥", got


def test_report_shape():
    s, _ = _mk()
    r = s.report()
    for k in ("checked", "updated", "skipped", "errors", "threshold", "locks"):
        assert k in r, k
