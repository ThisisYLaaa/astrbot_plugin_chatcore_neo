# -*- coding: utf-8 -*-
"""角色名识别与角色卡注入的契约测试。

覆盖三件事：
1. 外号库（`characters.json`）的存储/编辑语义 —— 与旧
   `astrbot_plugin_strinova_news` 的 `roles.json` 同构；
2. 纯逻辑匹配的边界（大小写、单字名、slug 别名、当前人格自己）；
3. 注入契约：命中内容必须走**动态段**（temp），不进 system_prompt 稳定段。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.assembler import AssemblyInput, PromptAssembler  # noqa: E402
from core.characters import (DEFAULT_CHARACTERS, CharacterMatcher,  # noqa: E402
                             CharacterStore, _scan_world_briefs)
from core.paths import PERSONAS_DIR, WORLDS_DIR  # noqa: E402
from core.persona import PersonaService  # noqa: E402

from run_tests import Skip  # noqa: E402

HEADER = "# 消息里提到的角色（资料参考）"


def _tmp_store() -> CharacterStore:
    return CharacterStore(Path(tempfile.mkdtemp(prefix="ccn-char-")) / "characters.json")


def _service() -> PersonaService:
    svc = PersonaService()
    if not svc.all():
        raise Skip("尚未迁移人格数据")
    return svc


def _matcher(svc: PersonaService | None = None) -> CharacterMatcher:
    return CharacterMatcher(_tmp_store(), (svc or _service()).all())


# ---------------------------------------------------------------- 存储层


def test_store_roundtrip_and_atomic_write():
    st = _tmp_store()
    assert st.load() == {}
    st.save({"伊薇特": ["ywt", "小冰"]})
    assert st.load() == {"伊薇特": ["ywt", "小冰"]}
    # 文件必须是可读的 UTF-8 JSON（原子写不留 .tmp 残渣）
    assert st.path.read_text(encoding="utf-8").lstrip().startswith("{")
    assert not list(st.path.parent.glob("*.tmp"))


def test_parse_aliases_splits_and_dedupes():
    got = CharacterStore.parse_aliases("ywt, 小冰、冰冰;ywt\n艾卡 ")
    assert got == ["ywt", "小冰", "冰冰", "艾卡"]


def test_ensure_seeds_missing_keys_without_overwriting():
    st = _tmp_store()
    st.save({"伊薇特": ["ywt"]})
    added = st.ensure(["伊薇特", "拉薇", "拉薇", ""])
    assert added == 1, "只应补 拉薇 一个键"
    roles = st.load()
    assert roles["伊薇特"] == ["ywt"], "已有外号不能被覆盖"
    assert roles["拉薇"] == []


def test_alias_editing_semantics():
    st = _tmp_store()
    st.save({"拉薇": []})
    assert st.add_alias("拉薇", "薇姐") is True
    assert st.add_alias("拉薇", "薇姐") is False, "重复外号应被拒"
    assert st.add_alias("拉薇", "拉薇") is False, "外号不能等于角色名"
    assert st.add_alias("不存在", "x") is False, "未知角色应被拒"
    assert st.delete_alias("拉薇", "薇姐") is True
    assert st.delete_alias("拉薇", "薇姐") is False
    st.set_aliases("拉薇", "薇姐、微姐,拉薇")
    assert st.load()["拉薇"] == ["薇姐", "微姐"], "set_aliases 要过滤掉角色名本身"
    assert st.delete_character("拉薇") is True
    assert "拉薇" not in st.load()


# ---------------------------------------------------------------- 匹配


def test_match_is_case_insensitive_for_ascii():
    m = _matcher()
    assert "伊薇特" in m.match("YVETTE 是谁")
    assert "伊薇特" in m.match("yvette是谁")


def test_match_uses_user_aliases():
    st = _tmp_store()
    st.save({"伊薇特": ["ywt", "小冰"]})
    m = CharacterMatcher(st, _service().all())
    assert "伊薇特" in m.match("小冰在干嘛")
    assert "伊薇特" in m.match("ywt 呢")


def test_single_char_names_need_boundary():
    """单字名默认不参与匹配；显式开放后也只认"左右不挨汉字"的用法。"""
    m = _matcher()
    # 默认 min_len=2：明/信/汐 这类单字名整体不参与
    assert m.match("我明白了") == []
    assert m.match("明 在吗") == []
    # 开放单字名后：日常用词仍不误触发，独立出现才算
    assert "明" not in m.match("我明白了", min_len=1)
    assert "明" not in m.match("说明一下", min_len=1)
    assert "明" in m.match("明 在吗", min_len=1)


def test_slug_aliases_are_not_matchable():
    """pfm_strinova_* / strinova_* 这类目录 slug 不进匹配索引。"""
    m = _matcher()
    kws = {kw for kw, _ in m.keywords()}
    assert not any("_" in kw for kw in kws), [k for k in kws if "_" in k]
    assert "pfm_strinova_yvette" not in kws


# ---------------------------------------------------------------- 卡片


def test_world_brief_is_extracted_from_world_files():
    """非人格角色（如 米雪儿·李）的身份要从 _worlds/ 的「关键人物」里捞到。"""
    briefs = _scan_world_briefs(WORLDS_DIR)
    assert briefs, "没有从世界观资料里扫到任何人物条目"
    assert "米雪儿·李" in briefs
    assert "搜查" in briefs["米雪儿·李"]


def test_render_block_gives_brief_and_persona_relation():
    svc = _service()
    m = CharacterMatcher(_tmp_store(), svc.all())
    doc = svc.get("strinova_ming")
    assert doc is not None
    block = m.render_block("拉薇最近怎么样", doc=doc)
    assert "拉薇" in block
    assert "与当前人格的关系" in block, "命中剧情人物时要带上与当前人格的关系"


def test_render_block_skips_current_persona():
    """被提到的就是当前人格自己时不该注入（她当然认识自己）。"""
    svc = _service()
    m = CharacterMatcher(_tmp_store(), svc.all())
    doc = svc.get("strinova_yvette")
    assert doc is not None
    assert m.render_block("伊薇特在吗", doc=doc) == ""


def test_render_block_respects_limits():
    svc = _service()
    m = CharacterMatcher(_tmp_store(), svc.all())
    text = "伊薇特、拉薇、梅瑞狄斯、白墨"
    block = m.render_block(text, max_hits=2)
    assert sum(1 for ln in block.splitlines() if ln.startswith("- ")) == 2
    long_block = m.render_block(text, max_hits=4, brief_chars=200, max_chars=0)
    short_block = m.render_block(text, max_hits=4, brief_chars=6, max_chars=40)
    assert len(short_block) <= 41, "max_chars 必须生效"
    assert len(short_block) < len(long_block)


def test_render_block_empty_on_no_hit():
    m = _matcher()
    assert m.render_block("今天天气不错") == ""


def test_default_roster_covers_old_plugin_names():
    """默认名册要能承接旧插件的 roles.json 键位。"""
    for name in ("伊薇特", "梅瑞狄斯", "米雪儿·李", "香奈美"):
        assert name in DEFAULT_CHARACTERS


# ---------------------------------------------------------------- 注入契约


def test_characters_go_to_temp_dynamic_not_system_prompt():
    """角色卡必须落在动态段（extra_user_content_parts + temp），不进稳定段。"""
    svc = _service()
    doc = svc.get("strinova_yvette")
    asm = PromptAssembler(framework=False)
    res = asm.build(AssemblyInput(doc=doc, characters="- 拉薇（Lavie）：剪刀手第三执行者"))
    assert HEADER in res.dynamic_text
    assert res.parts.get("characters", 0) > 0
    assert HEADER not in res.system_suffix, "角色卡不能进 system_prompt 前缀"


def test_no_characters_means_no_section():
    svc = _service()
    doc = svc.get("strinova_yvette")
    res = PromptAssembler(framework=False).build(AssemblyInput(doc=doc))
    assert "characters" not in res.parts
    assert HEADER not in res.dynamic_text


# ---------------------------------------------------------------- 旧库导入


def _migration():
    """按路径加载迁移脚本（migrations/ 不是包）。"""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "migrations" / "import_characters.py"
    if not path.is_file():
        raise Skip("没有 import_characters.py")
    spec = importlib.util.spec_from_file_location("_ccn_import_characters", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_import_merge_is_additive_and_idempotent():
    """旧库导入 = 并集：不删已有外号，二次导入为零变更。"""
    m = _migration()
    old = {"伊薇特": ["小熊"], "梅瑞狄斯": ["沙猫"], "艾卡": []}
    cur = {"伊薇特": ["ywt"], "拉薇": ["辣味"]}      # 用户已自填的
    d1 = m.merge(old, cur)
    assert cur["伊薇特"] == ["ywt", "小熊"], "旧外号应追加，不能覆盖用户已填的"
    assert "梅瑞狄斯" in cur and cur["梅瑞狄斯"] == ["沙猫"]
    assert "艾卡" in cur, "旧库里的角色要建键"
    assert d1["added_aliases"] == {"伊薇特": ["小熊"], "梅瑞狄斯": ["沙猫"]}
    assert d1["new_characters"] == ["梅瑞狄斯", "艾卡"]

    d2 = m.merge(old, cur)
    assert d2["added_aliases"] == {} and d2["new_characters"] == [], \
        "二次导入必须零变更（幂等）"


def test_import_drops_alias_equal_to_name():
    m = _migration()
    cur: dict = {}
    d = m.merge({"拉薇": ["拉薇", "辣味"]}, cur)
    assert cur["拉薇"] == ["辣味"]
    assert any("拉薇" in x for x in d["dropped"])


def test_shared_alias_hits_every_character():
    """同一外号挂在多个角色上时，命中的角色全部列出（旧库的既有语义）。

    旧 roles.json 里 奥黛丽/汐 都叫「盾构」、香奈美/心夏 都叫「大狙」。
    这里固化该行为：不静默丢掉其中一个，交由 max_hits 截断。
    """
    st = _tmp_store()
    st.save({"奥黛丽": ["盾构"], "汐": ["盾构"]})
    m = CharacterMatcher(st, _service().all())
    hit = m.match("盾构好用吗")
    assert set(hit) == {"奥黛丽", "汐"}, hit
