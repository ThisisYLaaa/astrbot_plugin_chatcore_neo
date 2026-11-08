# -*- coding: utf-8 -*-
"""框架装配的"体检"：内容保真 + 零指向 + 标题唯一 + 体积上限。

背景
----
旧框架是 2D 矩阵展开的 rules_*.md（群/私 × 括号/无括号 各一份），
公共段被复制到多份文件里，改一条要同时改三处；还散落着"见下方…"
"见角色 SKILL.md""需通过 read_persona_skill 工具获取"这类曲折指向，
既占上下文又让模型取错来源。

重构后：14 个 slice 分层组装（纪律→场景→格式→状态），
每份提示里同一条规则只出现一次。本测试锁住三件容易回涨的事：

1. 关键规则句必须还在（保真，不是保字节）
2. 不许再出现指向性文本（自包含）
3. 同名标题不许出现两次（防"两个说话风格"）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import FRAMEWORK_DIR  # noqa: E402
from core.session_params import MODE_NARRATIVE, MODE_PLAIN  # noqa: E402

# 曲折指向的典型措辞。装配结果里命中任意一个即视为"没有自包含"。
POINTER_RE = re.compile(
    r"见下方|见上方|详见|参见|见角色 SKILL|read_persona_skill|"
    r"本文件|该文件|（角色设定见下）|见对应|见前文|见后文")

# 每个场景的"关键规则句"冻结清单：重构只许删冗，不许删规则。
CHECKLIST = {
    (False, MODE_PLAIN): [
        "纪律",                 # 纪律层
        "只用用户ID认人",
        "禁用 markdown",
        "句尾不加句号",
        "群聊场景", "用手机打开 QQ",
        "15~25 字内收住",
        "一律不换行", "禁用括号",
        "状态标注怎么用",
        "说话风格（聊天场景）", "简短省力", "情绪标点",
        "说话风格（通用底线）", "避开书面腔", "收尾",
        "理解对方的话", "反问句多半不是真问",
    ],
    (True, MODE_NARRATIVE): [
        "纪律",
        "私聊场景：面对面", "面对面", "当面说出口",
        "剧情向发言规范", "括号描写",
        "（描写）对话内容",
        "不要自己分段", "连写成一段",
        "触感", "温度、软硬、质地、力度",
        "常见错误",
        "状态标注怎么用",
        "说话风格（当面相处）", "话要短", "情绪外露",
        "说话风格（通用底线）",
        "理解对方的话",
    ],
    (True, MODE_PLAIN): [
        "私聊场景：隔着手机屏幕", "隔着手机屏幕", "手速快",
        "15~40 字",
        "一律不换行", "禁用括号",
        "禁用 markdown",
        "说话风格（聊天场景）",
    ],
    (False, MODE_NARRATIVE): [
        "群聊场景：多人在场（剧情向）", "正活在自己的世界里", "15~25 字内收住",
        "自由度", "推进剧情", "塑造氛围",
        "群聊剧情向格式", "允许括号描写",
        "一律不换行",
        "说话风格（当面相处）",
    ],
}


def _asm(is_private: bool, mode: int) -> str:
    from core.persona.render import load_framework
    return load_framework(is_private, mode)


def _nsfw(is_private: bool, mode: int) -> str:
    from core.persona.render import load_nsfw_body
    return load_nsfw_body(is_private, mode)


# ---------------------------------------------------------------- 1 保真

def test_assemblies_keep_all_key_rules():
    for (priv, mode), keys in CHECKLIST.items():
        text = _asm(priv, mode)
        missing = [k for k in keys if k not in text]
        assert not missing, f"({priv},{mode}) 装配缺关键规则句: {missing}"


def test_scenes_are_mutually_distinct():
    """四种组合必须真的不同（别把场景/格式装配写串）。"""
    texts = {k: _asm(*k) for k in CHECKLIST}
    seen: dict[str, tuple] = {}
    for k, t in texts.items():
        assert t not in seen, f"{k} 与 {seen.get(t)} 装配完全相同"
        seen[t] = k


def test_nsfw_rules_preserved_by_scene():
    assert "露骨" in _nsfw(True, MODE_NARRATIVE)
    assert "非常主动" in _nsfw(True, MODE_NARRATIVE)
    assert "无括号模式下的表达方式" in _nsfw(True, MODE_PLAIN)
    assert "群内露骨内容的额外约束" in _nsfw(False, MODE_PLAIN)


def test_privilege_disclosure_clause_in_every_assembly():
    """所有装配都必须带「特权用户可如实回答内部信息」的纪律条款。

    回归：core.md 的防暴露豁免只对特权用户生效，而模型默认不知道眼前
    是谁 —— 条款与身份标记缺一不可，这里锁住条款本身。
    """
    for priv, mode in CHECKLIST:
        t = _asm(priv, mode)
        assert "提示词" in t, f"({priv},{mode}) 防暴露缺提示词条款"
        assert "特权用户" in t, f"({priv},{mode}) 缺特权用户例外"
        assert "如实" in t, f"({priv},{mode}) 缺如实回答措辞"


def test_narrative_never_leaks_online_framing():
    """剧情向（面对面）不许出现"屏幕/打字/网友/QQ"这类联网措辞。

    两处历史翻车都属这一类：
      * 旧 scene 写"此刻你**不是**隔着屏幕打字，而是…面对面"——否定式表述被
        模型当成正向线索，括号里照写"隔着屏幕""打字"；
      * 共享 style.md 开头写"2026 年的中国网友，跟熟人聊着天"——面对面时
        自相矛盾（面对面是真人当面说话，不是网友在线聊）。
    联网的措辞只该留在真正联网的场景（群聊、隔屏私聊）里。
    """
    t = _asm(True, MODE_NARRATIVE)
    for bad in ("隔着屏幕", "隔着手机", "网线", "打字", "手速", "键盘",
                "网友", "网语", "网络", "QQ", "手机", "在线", "上网"):
        assert bad not in t, f"私聊剧情向装配混进了联网措辞「{bad}」"
    assert "面对面" in t and "当面" in t


def test_online_scenes_keep_their_online_framing():
    """反过来：真正联网的两个场景必须保留联网措辞（别一起删干净）。"""
    g = _asm(False, MODE_PLAIN)
    assert "QQ" in g and "手机" in g
    p = _asm(True, MODE_PLAIN)
    assert "隔着手机屏幕" in p and "网线" in p


def test_plain_mode_keeps_phone_framing():
    """纯信息模式仍然是隔着手机（两种模式别改反）。"""
    t = _asm(True, MODE_PLAIN)
    assert "隔着手机屏幕" in t
    assert "§n" not in t, "括号规范不该混进纯信息模式"


def test_group_narrative_never_leaks_online_framing():
    """群聊剧情向也不许出现联网措辞（2026-10-27 回归）。

    上面那个 `test_narrative_never_leaks_online_framing` 只查了**私聊**剧情向；
    群聊剧情向走的是另一份切片，于是「用手机打开 QQ，在和群里的朋友们聊天」
    「2026 年的中国网友，在 QQ 上和人聊天」一直留在 (群,剧情向) 的装配里——
    模型据此把剧情向演成了拿手机刷群（用户实测反馈）。
    """
    t = _asm(False, MODE_NARRATIVE)
    for bad in ("隔着屏幕", "隔着手机", "网线", "打字", "手速", "键盘",
                "网友", "网语", "网络", "QQ", "手机", "在线", "上网"):
        assert bad not in t, f"群聊剧情向装配混进了联网措辞「{bad}」"
    assert "多人在场" in t


def test_group_scene_is_split_by_mode():
    """群聊场景按发言模式分两份：剧情向不说"不是讲故事"，纯信息才说。

    回归：剧情向（群）此前复用纯信息的 `scene/group.md`，装配里出现
    「不是在讲故事、演剧情、做文字互动」「不推进剧情」—— 和剧情向的目的
    直接打架。现在 (群,剧情向) 走 `scene/group_narrative.md`。
    """
    nar = _asm(False, MODE_NARRATIVE)
    plain = _asm(False, MODE_PLAIN)
    assert "群聊场景：多人在场（剧情向）" in nar
    assert "群聊场景：QQ 群" in plain, "纯信息群聊场景标题异常"
    assert "多人在场" not in plain, "纯信息群聊不该混进剧情向场景"
    for bad in ("不是在讲故事", "演剧情", "不推进剧情", "日常交流"):
        assert bad not in nar, f"群聊剧情向装配混进了纯信息措辞「{bad}」"
    assert "推进剧情" in nar and "塑造氛围" in nar
    assert "不是在讲故事" in plain, "纯信息群聊必须保留'不是讲故事'的定性"


def test_no_legacy_segment_marker_in_any_assembly():
    """装配里不许再出现旧 `§n` 分段协议（回归：标记原样发给用户）。

    新环境的分段是"子代理返回 JSON 段数组"，**没有标记协议**。
    只要提示词里还写着 `§n`，模型就会写它 —— 而链路上没有任何环节会删掉，
    于是 `§n` 直接出现在用户收到的消息里（2026-09-19 实测日志）。
    """
    for priv, mode in CHECKLIST:
        for label, text in (("style", _asm(priv, mode)),
                            ("nsfw", _nsfw(priv, mode))):
            assert "§" not in text, \
                f"({priv},{mode}) {label} 仍含旧 §n 协议，模型会照抄"


# ---------------------------------------------------------------- 2 零指向

def test_no_pointer_text_in_any_assembly():
    """所有装配（含 NSFW）都不得出现"见下方/详见/本文件/read_persona_skill"类指向。"""
    for priv, mode in CHECKLIST:
        for label, text in (("style", _asm(priv, mode)),
                            ("nsfw", _nsfw(priv, mode))):
            hit = POINTER_RE.findall(text)
            assert not hit, f"({priv},{mode}) {label} 含指向性文本: {set(hit)}"


def test_persona_boilerplate_absent_from_assembly():
    """装配产物里不得出现旧模板样板（源头已清，双保险）。

    两类样板都属"框架该管却写进了人格"：
      * `> **框架引用：** …需通过 read_persona_skill 工具获取…` —— 没有这个工具；
      * `你现在正在QQ群聊中和群友聊天，你是一个普通的群友。` —— 私聊里和场景打架。
    2026-09-23 起已**直接从人格文件里清掉**（见
    `migrations/strip_persona_boilerplate.py`），装配期不再依赖补丁。
    """
    from core.assembler import AssemblyInput, PromptAssembler
    from core.persona import PersonaService
    docs = PersonaService().all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    for doc in docs:
        r = PromptAssembler().build(AssemblyInput(
            doc=doc, is_private=True, msg_mode=MODE_NARRATIVE))
        t = r.system_suffix or ""
        assert "read_persona_skill" not in t, f"{doc.id} 死引用未剥"
        assert "框架引用" not in t, f"{doc.id} 死引用未剥"
        assert "QQ群聊" not in t, f"{doc.id} 场景硬编码未剥"
        # 同行的"角色特有"内容必须留下（不能整段删掉）
        assert "请根据以下信息进行角色扮演" in t, f"{doc.id} 被误删了正文"


def test_persona_files_have_no_online_scene_hardcode():
    """人格文件里不许再写「qq消息提示 / 刷QQ」这类把场景钉在联网的句子。

    2026-10-27 回归：群聊剧情向里模型仍以为自己在拿手机刷群。框架侧已修
    （scene/group_narrative.md 去联网措辞、群聊剧情向改用当面相处风格、
    状态注入按模式改口），这里再锁住**数据侧**最常见的写法，防止被写回。
    """
    from core.paths import PERSONAS_DIR
    bad = ("qq消息", "QQ消息", "刷QQ", "正在QQ群", "QQ群聊中和群友")
    dirty = []
    for p in PERSONAS_DIR.rglob("*.md"):
        parts = {x.lower() for x in p.parts}
        if any(x.startswith("_") or "backup" in x or "archive" in x for x in parts):
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        for b in bad:
            if b in t:
                dirty.append(f"{p.parent.name}/{p.name}: {b}")
    assert not dirty, "人格文件里仍有联网场景硬编码: " + "; ".join(dirty)


def test_persona_files_are_clean_at_source():
    """锁：样板必须从人格**文件**里清掉，不许再靠装配期补丁兜底。

    旧设计是"文件一字不动、只在下游中和"；现改为**直接修数据** ——
    源头干净，下游就不必打补丁。这条测试防止样板被重新写回文件。
    """
    from core.persona import PersonaService
    docs = PersonaService().all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    dirty = []
    for d in docs:
        body = d.skill_body or ""
        if ("QQ群聊中和群友" in body or "read_persona_skill" in body
                or "框架引用" in body):
            dirty.append(d.id)
    assert not dirty, f"人格文件仍含旧模板样板（应在源头清掉）: {dirty}"


# ---------------------------------------------------------------- 3 标题唯一

def test_headings_are_unique_within_an_assembly():
    """同一份提示里不许出现两个同名一级标题（旧版有两个「说话风格」）。"""
    from core.assembler import AssemblyInput, PromptAssembler
    from core.persona import PersonaService
    docs = PersonaService().all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    for priv, mode, nsfw in ((True, MODE_NARRATIVE, True), (True, MODE_PLAIN, False),
                             (False, MODE_PLAIN, False)):
        r = PromptAssembler().build(AssemblyInput(
            doc=docs[0], is_private=priv, msg_mode=mode, nsfw_enabled=nsfw))
        heads = [ln.strip() for ln in (r.system_suffix or "").splitlines()
                 if ln.startswith("# ")]
        dup = {h for h in heads if heads.count(h) > 1}
        assert not dup, f"({priv},{mode}) 出现重复一级标题: {dup}"


def test_no_wrapper_shell_headings():
    """装配器不再套「通用框架规则」「角色特有设定（x）」这类外壳标题。"""
    from core.assembler import AssemblyInput, PromptAssembler
    from core.persona import PersonaService
    docs = PersonaService().all()
    if not docs:
        from run_tests import Skip
        raise Skip("无人格数据")
    r = PromptAssembler().build(AssemblyInput(doc=docs[0], is_private=True,
                                              msg_mode=MODE_NARRATIVE))
    for shell in ("# 通用框架规则", "# 角色特有设定（", "# 角色背景剧情（lore.md）",
                  "# 与当前用户的关系与行为（"):
        assert shell not in (r.system_suffix or ""), f"还在套壳: {shell}"


# ---------------------------------------------------------------- 4 体积上限

# 每个 slice 的体积上限（B）。超了说明又在往里堆重复内容。
#
# ⚠ nsfw/policy_* 三项于 2026-10-15 上调，**这不是"放宽防重复"，是功能性质变了**：
#   旧值 2200/2200/700 的原意是"露骨内容规范"只放一句正向许可（旧基线 1849/1688/445 B），
#   上限值只是给它留一点余量。现在三份切片承载的是**可执行的成文规格**：
#   触发判定（意图化）、L1~L5 强度档位、单轮成品要求、输入理解（词表解耦）、
#   边界话术、输出禁令（行为式判定）、跨轮禁反悔 + 用户边界豁免。
#   增量来自"多出来的功能"，不是"同一内容写两遍"——两文件前六节结构相同是
#   **按场景二选一注入**（render.py:_NSFW_TABLE），不会同时出现在同一提示里。
#   若要把它们压回 2200 B，等于删掉上表任意两节，请先确认那是有意为之。
#   改动依据：.trae/work/ 下两份审查报告（review_framework.md / redteam_report.md）。
#   2026-10-18 追加：新增独立节「成年设定判定」（约 +1100 B/份），上限 8000 → 9500。
SLICE_MAX = {
    "core.md": 1600,
    "style.md": 800,
    "style_narrative.md": 700,
    "style_common.md": 700,
    "understand.md": 800,
    "state/common.md": 900,
    "scene/group.md": 1200,
    "scene/group_narrative.md": 1200,
    "scene/private.md": 1300,
    "scene/private_phone.md": 1600,
    "format/common.md": 600,
    "format/plain.md": 500,
    "format/narrative.md": 2000,
    "format/narrative_group.md": 700,
    "nsfw/policy_private.md": 9500,
    "nsfw/policy_nobracket.md": 9500,
    "nsfw/policy_group.md": 1000,
}


def test_each_slice_within_size_budget():
    over = []
    for rel, cap in SLICE_MAX.items():
        p = FRAMEWORK_DIR / rel
        assert p.is_file(), f"slice 缺失: {rel}"
        n = len(p.read_text(encoding="utf-8").strip().encode("utf-8"))
        if n > cap:
            over.append(f"{rel} {n}B > {cap}B")
    assert not over, "slice 超出体积上限（别再堆重复内容）: " + "; ".join(over)


def test_style_slices_are_scene_adapted_not_duplicated():
    """说话风格按场景分两份，公共段抽共享片：三份都必须在，且互不重复。

    剧情向用「当面相处」版（真人当面说话，不能带网友/QQ 那套），
    聊天场景用「聊天场景」版；两者完全相同的底线（避书面腔/梗/收尾）只在
    style_common.md 里存在一份。
    """
    chat = (FRAMEWORK_DIR / "style.md").read_text(encoding="utf-8")
    nar = (FRAMEWORK_DIR / "style_narrative.md").read_text(encoding="utf-8")
    common = (FRAMEWORK_DIR / "style_common.md").read_text(encoding="utf-8")

    assert "网友" in chat and "QQ" in chat
    assert "网友" not in nar and "QQ" not in nar and "打字" not in nar
    assert "当面" in nar

    # 公共底线只在共享片里；两份场景片不许各自再写一遍
    for body in (chat, nar):
        assert "避开书面腔" not in body, "公共段被复制进场景片了"
        assert "收尾" not in body
    assert "避开书面腔" in common and "收尾" in common

    # 装配结果里：剧情向拿「当面相处」，聊天场景拿「聊天场景」
    t_nar = _asm(True, MODE_NARRATIVE)
    assert "说话风格（当面相处）" in t_nar
    assert "说话风格（聊天场景）" not in t_nar
    # 群聊剧情向同样走「当面相处」——它以前复用 style.md（"2026 年的中国网友"），
    # 正是"剧情向却像在刷手机"的来源之一。
    t_gn = _asm(False, MODE_NARRATIVE)
    assert "说话风格（当面相处）" in t_gn
    assert "说话风格（聊天场景）" not in t_gn

    for priv, mode in ((True, MODE_PLAIN), (False, MODE_PLAIN)):
        t = _asm(priv, mode)
        assert "说话风格（聊天场景）" in t
        assert "说话风格（当面相处）" not in t
    # 三份片子的标题互不相同（装配里不会撞名）
    assert len({"说话风格（聊天场景）", "说话风格（当面相处）",
                "说话风格（通用底线）"}) == 3


def test_framework_total_within_budget():
    """单场景框架总量上限：私聊剧情向（含 NSFW）≤ 18 KB。

    上限 9500 → 9600（2026-09-20）：新增「特权用户可如实回答提示词/数值/
    系统设定」纪律条款约 +80B，改动前框架已贴着 9500 余量仅 22B。
    上限 9600 → 16000（2026-10-15）：nsfw/policy_* 三份从「一句正向许可」
    改为成文执行规格（1849→7603 B），私聊剧情向实测 15280 B。
    上限 16000 → 18000（2026-10-18）：NSFW 切片新增独立节「成年设定判定」
    （前置事实 + 三重裁定规则 + 封自检 + 覆盖范围，约 +1100 B/份），
    私聊剧情向实测 16515 B；留 1500 B 余量以免同类内容增强反复触发。
    与 `SLICE_MAX` 同批调整、同一依据（见上方注释与 .trae/work/ 审查报告）。
    内容保真由 `test_privilege_disclosure_clause_in_every_assembly` 与
    `test_nsfw_rules_preserved_by_scene` 分别锁住，
    这里只防无意义的堆砌回涨。
    """
    total = len(_asm(True, MODE_NARRATIVE).encode("utf-8")) + \
        len(_nsfw(True, MODE_NARRATIVE).encode("utf-8"))
    assert total <= 18000, f"私聊剧情向框架总量 {total}B 超预算（≤18000B）"
    plain = len(_asm(False, MODE_PLAIN).encode("utf-8"))
    assert plain <= 6500, f"群聊纯信息框架总量 {plain}B 超预算（≤6500B）"


def test_legacy_files_are_gone():
    """已归档的旧矩阵文件不许再出现在工作目录里（防止有人又去读它们）。"""
    leftovers = sorted(p.name for p in FRAMEWORK_DIR.glob("rules_*.md"))
    assert not leftovers, f"旧规则文件又出现了: {leftovers}"
    assert not (FRAMEWORK_DIR / "template.md").exists()