# -*- coding: utf-8 -*-
"""离线自检：实例目录 / 人格池 / 特权配置。

    python migrations/selfcheck_instances.py

只读 `plugin_data/chatcore_neo/`，不连 AstrBot、不写任何数据。
退出码 0 = 结构正常。
"""
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent          # 插件目录
DATA = PLUGIN.parent.parent                               # .../data
PDATA = DATA / "plugin_data" / "chatcore_neo"
sys.path.insert(0, str(PLUGIN.parent))
sys.path.insert(0, str(DATA.parent))                      # 让 astrbot 可导入

from core.instances import InstanceStore                  # noqa: E402
from core.paths import INSTANCES_DIR                      # noqa: E402
from core.persona_pool import PersonaPool                 # noqa: E402


class Doc:
    def __init__(self, d):
        self.id = d["id"]
        self.display_name = d.get("display_name", d["id"])
        self.display_name_en = ""
        self.world = d.get("world", "")
        self.aliases = []


def main() -> int:
    cfg_path = DATA / "cmd_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception:
        cfg = {}
    store = InstanceStore(cfg)
    print("discover:", [d["id"] for d in store.discover()])
    print("loaded  :", store.loaded_ids())
    print("default :", store.default_id())

    pool = PersonaPool(INSTANCES_DIR)
    for iid in store.loaded_ids():
        c = store.load_cfg(iid)
        print(f"{iid}: grants={len(pool.grants(iid))} "
              f"privilege={(c or {}).get('privilege_uid', '')}")

    pdir = PDATA / "personas"
    docs = [Doc({"id": p.name}) for p in pdir.iterdir() if p.is_dir()] \
        if pdir.is_dir() else []
    print("personas:", len(docs))
    m = pool.matrix(docs, [{"id": i} for i in store.loaded_ids()])
    print("matrix  :", [(r["id"], len(r["grants"])) for r in m["instances"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
