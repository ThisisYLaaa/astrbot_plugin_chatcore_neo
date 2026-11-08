# -*- coding: utf-8 -*-
"""W2 测试：状态层 / 世界观工具 / 装配器 / 历史卫生 / 兼容层。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import compat, world as W  # noqa: E402
from core.assembler import AssemblyInput, PromptAssembler  # noqa: E402
from core.history_hygiene import (PLACEHOLDER, strip_system_reminders,
                                  strip_tool_results)  # noqa: E402
from core.state import MemberState, Privilege, StateStore  # noqa: E402
from core.state.model import affection_desc, emotion_label, lewdness_label  # noqa: E402

from run_tests import Skip  # noqa: E402


# ---------------------------------------------------------------- 状态阈值

def test_label_thresholds_match_old_implementation():
    """阈值必须与旧实现逐值一致，否则用户会立刻察觉数值手感变了。"""
    assert [emotion_label(v) for v in (0, 19, 20, 39, 40, 69, 70, 100)] == \
        ["低落", "低落", "烦躁", "烦躁", "平静", "平静", "开心", "开心"]
    assert [lewdness_label(v) for v in (0, 33, 34, 66, 67, 99, 100)] == \
        ["低", "低", "中", "中", "高", "高", "满"]
    assert affection_desc(30) == "低" and affection_desc(50) == "中"
    assert affection_desc(80) == "高" and affection_desc(90) == "非常高"


def test_snapshot_format_unchanged():
    st = MemberState(affection=73, lewdness=30, depravity=0, emotion=100)
    assert st.snapshot() == "好感:73(中) 情绪:开心(100) 淫乱:低(30) 恶堕:0"


def test_state_update_keyword_rules():
    st = MemberState(affection=65, emotion=100, lewdness=20, depravity=0)
    st.update("你好可爱，好喜欢", "你好可爱")
    assert st.emotion == 100          # 已封顶
    assert st.affection == 68        # +3

    st2 = MemberState(affection=65, emotion=50)
    st2.update("", "你真讨厌")
    assert st2.emotion == 40          # -10
    assert st2.affection == 62        # -3


def test_state_reset_when_lewdness_caps():
    st = MemberState(lewdness=99, depravity=40, emotion=80)
    st.update("啊" * 120 + "身体", "嗯")
    assert st.lewdness == 0 and st.depravity == 0


def test_state_groom_keeps_recent():
    import time
    st = MemberState()
    now = int(time.time())
    st.conversation_log = [
        {"role": "user", "content": f"m{i}", "time": now - (100 - i) * 10}
        for i in range(20)
    ]
    st.groom(max_count=5, timeout_secs=60)
    assert len(st.conversation_log) <= 20
    times = [e["time"] for e in st.conversation_log]
    assert times == sorted(times), "裁剪后必须仍按时间有序"


# ---------------------------------------------------------------- 状态持久化

def test_state_store_roundtrip_and_path_layout():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = StateStore(root)
        # 群聊：三级目录
        p = store.path_for("Sui:GroupMessage:123", "strinova_kanami", "456")
        assert p == root / "Sui_GroupMessage_123" / "strinova_kanami" / "456.json"
        # 私聊：两级
        p2 = store.path_for("Banyan:FriendMessage:789", "diy_sui")
        assert p2 == root / "Banyan_FriendMessage_789" / "diy_sui.json"

        st = store.load("Sui:GroupMessage:123", "strinova_kanami", "456")
        st.affection = 88
        st.append_user("你好", "456")
        store.save("Sui:GroupMessage:123", "strinova_kanami", "456", st)

        store2 = StateStore(root)          # 新实例，强制从磁盘读
        got = store2.load("Sui:GroupMessage:123", "strinova_kanami", "456")
        assert got.affection == 88
        assert len(got.conversation_log) == 1


def test_state_store_inject_override():
    with tempfile.TemporaryDirectory() as td:
        store = StateStore(Path(td))
        store.set_inject("u", "p", "1", "你暗恋这个人")
        assert store.load("u", "p", "1").inject == "你暗恋这个人"
        store.invalidate()
        assert store.load("u", "p", "1").inject == "你暗恋这个人"
        store.set_inject("u", "p", "1", "")
        store.invalidate()
        assert store.load("u", "p", "1").inject == ""


def test_privilege_tier():
    """实例级单特权用户：命中即 intimate，其余一律 friend。

    2026-10-28 多实例重构（`core/state/store.py:191`）把 `Privilege` 从
    「全局名单 `Privilege(qids)` + 分会话前缀覆盖 `per_session=`」改成
    「**每个实例有且只有一名**特权用户」，签名变成
    `Privilege(uid_of=callable)` / `Privilege(uid="...")`。
    旧写法 `Privilege(["100000001"])` 不但参数被忽略（列表不可调用 →
    落成"没有特权用户"），而且**不报错**，属于最坏的一种静默失效，
    所以这里改成显式 `uid=`。
    """
    pv = Privilege(uid="100000001")
    assert pv.tier("100000001") == "intimate"
    assert pv.tier("123") == "friend"
    assert pv.tier("") == "friend"
    assert Privilege.is_private_umo("Banyan:FriendMessage:1")
    assert not Privilege.is_private_umo("Banyan:GroupMessage:1")

    # 实例没配特权用户（uid 为空）→ 谁都不是特权；尤其不能把空串当命中
    assert Privilege(uid="").tier("") == "friend"
    assert Privilege().effective("Banyan:GroupMessage:1") == set()


def test_privilege_isolated_per_instance():
    """特权按**实例**隔离 —— 取代了旧的「分会话前缀覆盖」那一层。

    旧实现：全局名单 + `per_session` 前缀表，按
    「精确 UMO > 平台:类型 > 平台 > 全局」逐级覆盖。
    重构后：实例边界本身就是那个前缀，`uid_of(inst)` 按 UMO 第一段取本
    实例的特权用户，跨实例互不可见（旧测试断言的覆盖分级已不存在，
    保留它只会测一个删掉的功能）。
    """
    uids = {"Sui": "1705744120", "Banyan": "999"}
    pv = Privilege(uid_of=lambda inst: uids.get(inst, ""))
    # 同一实例下，无论私聊/群聊/哪个会话，认的都是该实例那一个人
    assert pv.uid_for("Sui:FriendMessage:1") == "1705744120"
    assert pv.tier("1705744120", scope="Sui:GroupMessage:2") == "intimate"
    assert pv.tier("1705744120", scope="Sui:FriendMessage:1") == "intimate"
    assert pv.tier("100000001", scope="Sui:FriendMessage:1") == "friend"
    # 跨实例隔离：Banyan 的特权用户在 Sui 里只是普通用户
    assert pv.tier("999", scope="Banyan:FriendMessage:9") == "intimate"
    assert pv.tier("1705744120", scope="Banyan:FriendMessage:9") == "friend"
    # effective() 保留旧口径：0 或 1 个元素的集合（webadmin / 口令沿用）
    assert pv.effective("Sui:FriendMessage:1") == {"1705744120"}
    assert pv.effective("Banyan:GroupMessage:1") == {"999"}
    # 空 scope → 不知道属于哪个实例 → 没有特权用户（绝不回退"全局名单"）
    assert pv.uid_for("") == "" and pv.effective("") == set()
    # 无回调（固定 uid）时对所有 scope 都算同一个实例
    fixed = Privilege(uid="1")
    assert fixed.tier("1", scope="Banyan:GroupMessage:1") == "intimate"
    assert fixed.effective("Banyan:GroupMessage:1") == {"1"}


# ---------------------------------------------------------------- 世界工具

def test_world_path_traversal_is_blocked():
    """world_read 的路径参数必须挡住 ../ 越界。"""
    for evil in ("../secret.md", "strinova/../../etc/passwd",
                 "..\\..\\win.ini", "/etc/passwd", "C:/Windows/win.ini",
                 "//server/share/x.md"):
        try:
            W.resolve_world_path(evil)
        except W.WorldError:
            continue
        except Exception as e:
            raise AssertionError(f"{evil!r} 抛了非 WorldError: {e!r}")
        raise AssertionError(f"{evil!r} 竟然通过了越界校验")


def test_world_tools_on_real_data():
    if not W.list_worlds():
        raise Skip("新环境没有世界观目录")
    listing = W.tool_world_list()
    assert "世界观" in listing
    w = W.list_worlds()[0]
    tree = W.tool_world_list(w)
    assert "个文件" in tree
    # 检索
    res = W.tool_world_search("的", limit=3)
    assert res
    # 行范围读取：截断提示必须出现（否则等于整份读入）
    files = W.world_files(w)
    if files:
        rel = files[0].relative_to(W.worlds_root()).as_posix()
        out = W.tool_world_read(rel, 1, 3)
        assert "第 1-3 行" in out
        full = W.tool_world_read(rel)
        assert rel in full


def test_world_read_truncates_long_files():
    p = W.worlds_root()
    if not p.is_dir():
        raise Skip("无世界观目录")
    biggest = None
    for f in p.rglob("*.md"):
        if biggest is None or f.stat().st_size > biggest.stat().st_size:
            biggest = f
    if biggest is None:
        raise Skip("无 md 文件")
    rel = biggest.relative_to(p).as_posix()
    out = W.tool_world_read(rel)
    if len(biggest.read_text(encoding="utf-8").splitlines()) > W.MAX_READ_LINES:
        assert "已截断" in out and "继续读" in out


def test_world_index_files_removed():
    p = W.worlds_root()
    if not p.is_dir():
        raise Skip("无世界观目录")
    left = list(p.rglob("_index.md"))
    assert not left, f"仍有 _index.md（文件树取代了清单）: {left}"


# ---------------------------------------------------------------- 历史卫生

def _ctx_with_tool(n_rounds: int, size: int = 500) -> list:
    out = []
    for i in range(n_rounds):
        cid = f"call_{i}"
        out.append({"role": "assistant", "content": "",
                    "tool_calls": [{"id": cid,
                                    "function": {"name": "world_read",
                                                 "arguments": "{}"}}]})
        out.append({"role": "tool", "tool_call_id": cid,
                    "content": "x" * size})
    return out


def test_strip_keeps_recent_round_and_pairs():
    ctx = _ctx_with_tool(3)
    strip_tool_results(ctx, keep_recent=1)
    # 最近一轮保留全文
    assert ctx[-1]["content"] == "x" * 500
    # 更早的替换为占位符
    assert ctx[1]["content"] == PLACEHOLDER
    assert ctx[3]["content"] == PLACEHOLDER
    # assistant↔tool 配对未被破坏
    ids = {tc["id"] for m in ctx if m.get("role") == "assistant"
           for tc in m.get("tool_calls") or []}
    tool_ids = {m.get("tool_call_id") for m in ctx if m.get("role") == "tool"}
    assert ids == tool_ids


def test_strip_zero_keeps_nothing():
    ctx = _ctx_with_tool(2)
    strip_tool_results(ctx, keep_recent=0)
    assert all(m["content"] == PLACEHOLDER for m in ctx if m["role"] == "tool")


def test_strip_ignores_unrelated_tools():
    ctx = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "晴"}]
    strip_tool_results(ctx, keep_recent=0)
    assert ctx[1]["content"] == "晴", "不该动无关工具的结果"


# ---------------------------------------------------------------- 兼容层

def test_event_template_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = compat.EventTemplateStore(Path(td) / "tpl.json")
        tpl = compat.EventTemplate(platform="aiocqhttp", self_id="111",
                                   sender={"user_id": "222"}, group_id="333")
        store.put("umo1", tpl)
        store.save()

        store2 = compat.EventTemplateStore(Path(td) / "tpl.json")
        got = store2.get("umo1")
        assert got is not None
        assert got.self_id == "111" and got.group_id == "333"
        assert got.sender.get("user_id") == "222"
        assert store2.get("nope") is None


def test_simulated_and_gutoken_flags():
    class E:
        pass
    e = E()
    assert not compat.is_simulated(e)
    compat.mark_simulated(e)
    assert compat.is_simulated(e)
    assert not compat.should_skip_side_effects(e)

    g = E()
    g._gutoken_active = True
    assert compat.should_skip_side_effects(g)
    assert compat.is_gutoken(g)


# ---------------------------------------------------------------- 装配器

def test_assembler_stable_segment_is_idempotent_and_marked():
    from core.persona import PersonaService
    svc = PersonaService()
    docs = svc.all()
    if not docs:
        raise Skip("无人格")
    asm = PromptAssembler()
    inp = AssemblyInput(doc=docs[0], state=MemberState(), tier="friend",
                        is_private=False)
    r1 = asm.build(inp)
    assert r1.system_suffix.startswith("\n\n" + "<!-- chatcore_neo:stable:begin -->")
    base = "系统原有提示词"
    once = base + r1.system_suffix
    # 幂等语义："先剔除自己上一轮的注入，再重新装配" 的结果应与上一轮完全相同
    assert asm.strip_previous(once) == base, "剔除后应回到原始提示词"
    assert asm.strip_previous(once) + r1.system_suffix == once, \
        "剔旧 + 重新装配 必须等于上一轮结果（幂等）"
    # 连续两轮注入后，标记块也只应剩一份
    doubled = asm.strip_previous(once + r1.system_suffix) + r1.system_suffix
    assert doubled.count("chatcore_neo:stable:begin") == 1


def test_dynamic_goes_to_parts_not_contexts():
    """装配结果只产出文本；写 req 的动作在 main.py，且只写 parts。

    2026-10-29 修：原版硬断言「超喜欢的」不在 system_suffix，实际靠的是
    当时 `svc.all()[0]` 那个目录恰好没写这一档好感度标题 —— 但大多数人格的
    `content/tier.intimate.md` 本来就有一行「- "超喜欢的，有点依赖感"：…」，
    而 `build_stable()` 会把 tier 正文拼进稳定段（设计如此：关系规则属稳定段）。
    于是新增人格、排序变化后 all()[0] 换人，用例就变成「看池里第一个目录
    怎么写」的玄学断言（2026-10-29 加了 20 个终末地人格后 `akekuri` 排到第一，
    实测报错）。现在改为：动态输入一律加哨兵串断言不外泄；「超喜欢的」这类
    框架文案只有在人格自带同款文案时才放行。
    """
    from core.persona import PersonaService
    from core.persona import render as R
    svc = PersonaService()
    if not svc.all():
        raise Skip("无人格")
    doc = svc.all()[0]
    asm = PromptAssembler()
    st = MemberState(affection=91, emotion=80, lewdness=70, depravity=5)
    sent = "__CCN_SENTINEL__"
    inp = AssemblyInput(doc=doc, state=st, tier="intimate",
                        is_private=False, impression="喜欢聊游戏" + sent,
                        memories=["上次聊了抽卡" + sent],
                        notices=["人格已切换" + sent])
    r = asm.build(inp)
    # 动态段走的是 build_inject_text 的文案（含好感内心描摹），不是裸数值
    assert "超喜欢的" in r.dynamic_text, r.dynamic_text[:200]
    assert "喜欢聊游戏" in r.dynamic_text
    assert "上次聊了抽卡" in r.dynamic_text
    assert "人格已切换" in r.dynamic_text
    # 稳定段里不能出现本轮动态内容（否则可缓存前缀就废了）
    assert "喜欢聊游戏" + sent not in r.system_suffix
    assert "上次聊了抽卡" + sent not in r.system_suffix
    assert "人格已切换" + sent not in r.system_suffix
    # 人格自带同款好感档位文案时（tier 正文按设计进稳定段）不判为泄漏
    tier_body = R.load_tier_body(doc, "intimate") or ""
    if "超喜欢的" not in tier_body:
        assert "超喜欢的" not in r.system_suffix


def test_assembler_stable_is_timestamp_free():
    """稳定段不得含时间戳，否则前缀缓存永远打不中。"""
    import re
    from core.persona import PersonaService
    svc = PersonaService()
    if not svc.all():
        raise Skip("无人格")
    r = PromptAssembler().build(AssemblyInput(doc=svc.all()[0],
                                              state=MemberState()))
    for pat in (r"\d{4}-\d{2}-\d{2}", r"\d{2}:\d{2}:\d{2}",
                r"\d{10}", r"timestamp"):
        assert not re.search(pat, r.system_suffix), f"稳定段含时间痕迹: {pat}"


# ---------------------------------------------------------------- 历史 reminder

_R = ("<system_reminder>User ID: 100000001, Nickname: 希希你的芙\n"
      "Group name: Adofaity丨冰与火之屋\n"
      "Current datetime: 2026-10-01 13:19 (CST), Weekday: Thursday</system_reminder>")
_TAG = "（说话人：希希你的芙，uid 100000001）"
_CTX = ("<system_reminder>You are in a group chat. Belows are group chat context "
        "after your last reply:\n--- BEGIN CONTEXT---\n[松子/23:10:05]: [Image]\n"
        "--- END CONTEXT ---\n</system_reminder>")


def test_reminders_str_content_tagged():
    """字符串 content：identity reminder 压成说话人标签，正文保留。"""
    ctx = [{"role": "user", "content": "晚上好\n" + _R}]
    out, removed = strip_system_reminders(ctx)
    assert removed > 0
    assert "<system_reminder>" not in out[0]["content"]
    assert "晚上好" in out[0]["content"]
    assert _TAG in out[0]["content"]


def test_reminders_list_content_separate_part():
    """分块 content（真实落库形态）：reminder 分块原地变成标签分块。

    2026-10-01 线上验证发现：框架把 extra_user_content_parts 作为独立
    text 分块落库，早期实现只处理字符串 content，等于完全没生效。
    """
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "晚上好"},
        {"type": "text", "text": _R},
    ]}]
    out, removed = strip_system_reminders(ctx)
    assert removed > 0
    parts = out[0]["content"]
    assert parts[0]["text"] == "晚上好"
    assert parts[1]["text"] == _TAG
    assert "<system_reminder>" not in parts[1]["text"]


def test_reminders_context_block_dropped():
    """群聊流水块（无身份行）整块删除；当轮由内置插件重新注入。"""
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "晚上好"},
        {"type": "text", "text": _CTX},
    ]}]
    out, removed = strip_system_reminders(ctx)
    assert removed == len(_CTX)
    assert out[0]["content"] == [{"type": "text", "text": "晚上好"}]


def test_reminders_mixed_part():
    """reminder 与正文混在同一分块：身份压成标签、时间戳/群名消失。"""
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "早上好" + _R},
    ]}]
    out, removed = strip_system_reminders(ctx)
    assert removed > 0
    text = out[0]["content"][0]["text"]
    assert text == "早上好" + _TAG
    assert "2026-10-01" not in text and "Adofaity" not in text


def test_reminders_list_keeps_non_text_parts():
    """图片等非文本分块原样保留；reminder 分块照常变标签。"""
    img = {"type": "image_url", "image_url": {"url": "file://x.png"}}
    ctx = [{"role": "user", "content": [img, {"type": "text", "text": _R}]}]
    out, removed = strip_system_reminders(ctx)
    assert removed > 0
    assert out[0]["content"] == [img, {"type": "text", "text": _TAG}]


def test_reminders_no_identity_block_dropped():
    """reminder 里没有身份行（残缺/异构）：整块删除，不发垃圾。"""
    bare = "<system_reminder>Current datetime: 2026-10-01 00:00 (CST)</system_reminder>"
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "嘿"},
        {"type": "text", "text": bare},
    ]}]
    out, removed = strip_system_reminders(ctx)
    assert removed == len(bare)
    assert out[0]["content"] == [{"type": "text", "text": "嘿"}]


def test_reminders_idempotent_and_noop():
    """标签不含 reminder 标记 → 重复执行零改动；无 reminder 零改动。"""
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "晚上好"},
        {"type": "text", "text": _R},
    ]}]
    out, r1 = strip_system_reminders(ctx)
    assert r1 > 0
    _, r2 = strip_system_reminders(out)
    assert r2 == 0
    plain = [{"role": "user", "content": "纯聊天"},
             {"role": "assistant", "content": "哦"}]
    _, r3 = strip_system_reminders(plain)
    assert r3 == 0


def test_reminders_speaker_tag_variants():
    """身份行缺昵称/缺 uid 的兜底格式。"""
    from core.history_hygiene import _speaker_tag
    assert _speaker_tag("<system_reminder>User ID: 123, Nickname: 小明</system_reminder>") == "（说话人：小明，uid 123）"
    assert _speaker_tag("<system_reminder>User ID: 123, Nickname: </system_reminder>") == "（说话人 uid 123）"
    assert _speaker_tag("<system_reminder>User ID: , Nickname: 小明</system_reminder>") == "（说话人：小明）"
    assert _speaker_tag("<system_reminder>nothing here</system_reminder>") == ""


def test_reminders_private_mode_keeps_time_drops_identity():
    """私聊模式：身份行（昵称/uid）丢弃、时间保留为 `[2026-10-01 周四 13:19]`。"""
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "这是啥底模"},
        {"type": "text", "text": _R},
    ]}]
    out, removed = strip_system_reminders(ctx, private=True)
    assert removed > 0
    parts = out[0]["content"]
    assert parts[0]["text"] == "这是啥底模"
    assert parts[-1]["text"] == "[2026-10-01 周四 13:19]"
    assert "说话人" not in parts[-1]["text"]
    assert "100000001" not in parts[-1]["text"]


def test_reminders_private_mode_str_content():
    """私聊模式 + 字符串 content：正文保留、尾部换成时间标签。"""
    ctx = [{"role": "user", "content": "晚上好\n" + _R}]
    out, removed = strip_system_reminders(ctx, private=True)
    assert removed > 0
    assert "晚上好" in out[0]["content"]
    assert "[2026-10-01 周四 13:19]" in out[0]["content"]
    assert "说话人" not in out[0]["content"]


def test_reminders_private_mode_no_datetime_drops_block():
    """私聊模式：没有 datetime 的 reminder 块整块删除（没东西可留）。"""
    bare = ("<system_reminder>User ID: 100000001, Nickname: 希希你的芙"
            "</system_reminder>")
    ctx = [{"role": "user", "content": [
        {"type": "text", "text": "嘿"},
        {"type": "text", "text": bare},
    ]}]
    out, removed = strip_system_reminders(ctx, private=True)
    assert removed == len(bare)
    assert out[0]["content"] == [{"type": "text", "text": "嘿"}]



# ---------------------------------------------------------------- embed 并发去重

class _FakeProvider:
    """计数假 provider：get_embedding 慢 50ms，记录每次调用。"""

    def __init__(self):
        self.calls = 0

    async def get_embedding(self, text):
        self.calls += 1
        await asyncio.sleep(0.05)
        return [0.1, 0.2, 0.3]


def _fake_ctx(prov):
    from core.memory import embedder as M

    class Ctx:
        def get_all_embedding_providers(self):
            return [prov]
    orig_pick = M.pick_provider
    M.pick_provider.last_error = ""
    M.pick_provider = lambda ctx, want, kind="embedding": prov
    return M, orig_pick


def test_embed_dedup_merges_concurrent_calls():
    """同一文本并发 embed 只打一次 provider。"""
    import asyncio
    from core.memory.embedder import Embedder

    prov = _FakeProvider()
    M, orig_pick = _fake_ctx(prov)
    try:
        M.pick_provider = lambda ctx, want, kind="embedding": prov
        e = Embedder(context=object(), db_path=Path(tempfile.mkdtemp()) / "m.db")
        e._provider = prov      # 跳过 pick
        async def go():
            rs = await asyncio.gather(*(e.embed_dedup(f"同一句查询{i}") for i in range(5)))
            return rs
        rs = asyncio.run(go())
        info = (f"lens={[len(x) for x in rs]} first={rs[0]!r} "
                f"err={e._last_error!r} failed={e._failed} calls={prov.calls} "
                f"cache={len(e._cache)} ready={e._ready}")
        assert all(len(r) == 3 for r in rs), info
        assert prov.calls == 5          # 5 个不同文本 = 5 次
        # 同一文本 ×10 并发 → 1 次
        async def go2():
            return await asyncio.gather(*(e.embed_dedup("同一条查询") for _ in range(10)))
        asyncio.run(go2())
        assert prov.calls == 6          # 5 + 1
        # 第二轮全走缓存，0 次
        asyncio.run(e.embed_dedup("同一条查询"))
        assert prov.calls == 6
    finally:
        M.pick_provider = orig_pick


def test_embed_dedup_error_not_shared_forever():
    """失败的 in-flight 不缓存结果：下次调用重新发起。"""
    import asyncio
    from core.memory.embedder import Embedder

    prov = _FakeProvider()
    state = {"fail": True}

    async def flaky(text):
        if state["fail"]:
            raise RuntimeError("boom")
        return [1.0, 2.0]
    prov.get_embedding = flaky

    from core.memory import embedder as M
    orig_pick = M.pick_provider
    M.pick_provider = lambda ctx, want, kind="embedding": prov
    try:
        e = Embedder(context=object(), db_path=Path(tempfile.mkdtemp()) / "m.db")
        e._provider = prov
        r = asyncio.run(e.embed_dedup("会失败的查询"))
        assert r == []                  # 失败 → 空列表（合法降级）
        state["fail"] = False
        r = asyncio.run(e.embed_dedup("会失败的查询"))
        assert r == [1.0, 2.0]          # 恢复后能再次调用
    finally:
        M.pick_provider = orig_pick
