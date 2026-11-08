# -*- coding: utf-8 -*-
"""At / 引用 的回归测试。

## 事故（2026-09-12 11:37-11:38，用户实测）
群友 `@bot 艾特我` → 机器人回的是**纯文本** `[@江一楠] 在的在的…`
群友 `@bot 引用我这条消息` → 回 `[@江一楠 这条呀，星绘给你原样引用回来啦…`
（右括号都没闭合，也没有任何真正的引用）

## 根因
插件真协议是 `[[at:QQ号]]` / `[[reply:消息id]]`，但**从没告诉过模型** ——
`inject.py` 里搜不到 `[[at`，人格规则文件里也没有。
模型于是自己发明 `[@昵称]`；输出层只认 `[[...]]`，于是既不转换、还把坏文本发出去。
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from core.inject import build_marker_block  # noqa: E402
from core.output import (fix_literal_mentions, parse_markers,  # noqa: E402
                         strip_markers)

UID, NAME, MID = "100000001", "江一楠", "300000002"


# ---------------------------------------------------------------- 协议说明

def test_marker_block_teaches_protocol():
    blk = build_marker_block(uid=UID, user_name=NAME, message_id=MID)
    assert f"[[at:{UID}]]" in blk, "必须给出本轮可用的具体 QQ"
    assert f"[[reply:{MID}]]" in blk, "必须给出本轮可用的具体消息 id"
    assert "不要" in blk and "[@" in blk, "必须明确禁止手写 [@昵称]"


def test_marker_block_without_ids_says_so():
    """没有可用 id 时要明说，免得模型瞎编一个。"""
    blk = build_marker_block(uid="", user_name="", message_id="")
    assert "不要输出这类标记" in blk or "没有可用的" in blk, blk


def test_marker_block_is_injected():
    """协议段必须真的被接进装配流程（否则又是幽灵）。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    assert "build_marker_block(" in src, "main.py 未调用 build_marker_block"
    assert "marker_block=" in src, "未传给 AssemblyInput"
    asm = (PLUGIN / "core" / "assembler.py").read_text(encoding="utf-8")
    assert "inp.marker_block" in asm, "assembler 未使用 marker_block"


# ---------------------------------------------------------------- 兜底转换

def test_literal_at_with_nickname_becomes_real_at():
    """日志原文1：`[@江一楠] …` 必须转成真 @。"""
    text = "[@江一楠] 在的在的，引航者~要测记忆随时喊星绘 中午记得吃饭呀"
    fixed, qqs = fix_literal_mentions(text, uid=UID, user_name=NAME)
    assert qqs == [UID], qqs
    assert parse_markers(fixed)["at"] == [UID]
    assert strip_markers(fixed).startswith("在的在的"), strip_markers(fixed)


def test_unclosed_literal_at_is_cleaned():
    """日志原文2：`[@江一楠 这条呀…`（没闭合）不能再漏出方括号。"""
    text = "[@江一楠 这条呀，星绘给你原样引用回来啦，框架测得怎么样，问题出在哪儿~"
    fixed, _ = fix_literal_mentions(text, uid=UID, user_name=NAME)
    assert "[" not in fixed and "【" not in fixed, fixed
    assert "这条呀" in fixed and "原样引用回来啦" in fixed


def test_fullwidth_brackets():
    fixed, qqs = fix_literal_mentions("【@江一楠】你好", uid=UID, user_name=NAME)
    assert qqs == [UID] and "[[at:" in fixed


def test_unknown_nickname_degrades_to_plain_text():
    """@ 一个不认识的人：不留坏括号，也不误当成 @。"""
    fixed, qqs = fix_literal_mentions("[@路人甲] 你好", uid=UID, user_name=NAME)
    assert not qqs and "[" not in fixed and "路人甲" in fixed


def test_normal_text_untouched():
    for s in ("引航者中午好呀，记得吃饭哦~", "[[at:1]] 在的".replace("1", UID),
              "没有任何标记的普通回复。"):
        fixed, qqs = fix_literal_mentions(s, uid=UID, user_name=NAME)
        assert fixed == s and not qqs, (s, fixed, qqs)


def test_protocol_markers_survive_the_fallback():
    """模型正确写的 `[[at:]]` / `[[reply:]]` 不能被兜底逻辑破坏。"""
    for kind, val in (("at", UID), ("reply", MID)):
        s = f"[[{kind}:{val}]] 内容"
        fixed, _ = fix_literal_mentions(s, uid=UID, user_name=NAME)
        assert parse_markers(fixed)[kind] == [val], (kind, fixed)


def test_output_layer_uses_the_fallback():
    """输出层必须真的调它（否则修了也没用）。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    assert "fix_literal_mentions(" in src, "main.py 未调用 fix_literal_mentions"


def test_output_layer_has_sender_context():
    """兜底需要 uid/昵称，输出层要取到。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def on_decorating_result")
    seg = src[i:i + 2600]
    assert "get_sender_name()" in seg, "输出层没取昵称，兜底无法匹配"
    assert "get_sender_id()" in seg or "X_UID" in seg, "输出层没取 uid"

# ---------------------------------------------------------------- 容错解析

def test_marker_missing_one_bracket_still_parsed():
    """**日志实测**：模型写成 `[[reply:300000001]`（少一个右括号）。

    旧正则要求两个 `]]` → 既没转成引用，也没被剥掉 →
    **原始标记文本直接发给了用户**。模型写坏标记是常态，解析必须容错。
    """
    text = "[[reply:300000001] 引用好啦，这次不艾特你，这样对了吧~"
    assert parse_markers(text)["reply"] == ["300000001"]
    left = strip_markers(text)
    assert "[" not in left and "reply" not in left, left
    assert left == "引用好啦，这次不艾特你，这样对了吧~", left


def test_marker_variants_all_parsed_and_stripped():
    cases = [
        ("[[reply:123]] 正常", "reply", "123"),
        ("[[reply:456] 少一个", "reply", "456"),
        ("[reply:789] 单层", "reply", "789"),
        ("【at:111】全角", "at", "111"),
        ("[[at:222] 少一个", "at", "222"),
        ("[[ reply : 333 ]] 带空格", "reply", "333"),
    ]
    for text, kind, val in cases:
        assert parse_markers(text)[kind] == [val], (text, parse_markers(text))
        left = strip_markers(text)
        assert "[" not in left and "【" not in left, (text, left)


def test_extra_bracket_does_not_leak():
    left = strip_markers("[[reply:222]]] 多一个")
    assert "]" not in left and "[" not in left, left


def test_non_numeric_marker_is_not_used_as_id():
    """`[[at:某某]]` 这种瞎写的不能生成坏组件，但也要被剥掉。"""
    assert parse_markers("[[at:abc]] 你好")["at"] == []
    left = strip_markers("[[at:abc]] 你好")
    assert "[" not in left and "abc" not in left, left


def test_marker_never_leaks_into_sent_text():
    """**核心不变量**：任何形态的标记都不能出现在最终发出去的文本里。"""
    import itertools
    opens = ["[[", "[", "【"]
    closes = ["]]", "]", "】", ""]
    keys = ["at", "reply"]
    for o, c, k in itertools.product(opens, closes, keys):
        text = f"{o}{k}:123456789{c} 正文"
        left = strip_markers(text)
        assert "[" not in left and "【" not in left, (text, left)
        assert "123456789" not in left or k not in left, (text, left)


def test_normal_brackets_survive():
    """正常的方括号内容不该被误删。"""
    for s in ("【公告】今晚维护", "（笑）", "你好[1]呀"):
        assert strip_markers(s) == s, s


def test_marker_block_demands_double_brackets():
    blk = build_marker_block(uid=UID, user_name=NAME, message_id=MID)
    assert "两对方括号" in blk, "必须明确写出标记的括号层数（模型最容易写错这里）"
    assert "不要 @。" in blk or "不要 @" in blk, "要说明 @ 是按需使用，不是默认动作"
