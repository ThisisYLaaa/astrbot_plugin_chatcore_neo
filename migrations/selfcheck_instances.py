# -*- coding: utf-8 -*-
"""离线自检：实例化 / 人格池 / 配置读取。"""
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
# 本插件数据根：<data>/plugin_data/chatcore_neo（<data> = 插件的祖父目录）
PDATA = Path(os.environ.get("CHATCORE_PDATA")
             or PLUGIN.parent.parent / "plugin_data" / "chatcore_neo")
sys.path.insert(0, str(PLUGIN.parent))
sys.path.insert(0, str(PLUGIN))

from core.paths import INSTANCES_DIR  # noqa: E402
from core.instances import InstanceStore  # noqa: E402
from core.persona_pool import PersonaPool  # noqa: E402
import os


class Doc:
    def __init__(self, d):
        self.id = d["id"]
        self.display_name = d.get("display_name", d["id"])
        self.display_name_en = ""
        self.world = d.get("world", "")
        self.aliases = []


def main() -> int:
    fake = {"platform": [
        {"id": "Banyan", "type": "aiocqhttp", "enable": True},
        {"id": "Sui", "type": "aiocqhttp", "enable": True},
        {"id": "鲨鱼", "type": "aiocqhttp", "enable": False},
    ]}
    st = InstanceStore(fake)
    print("discover:", [d["id"] for d in st.discover()])
    print("loaded  :", st.loaded_ids())
    print("default :", st.default_id())
    print("banyan  :", st.meta("Banyan"))
    print("shayu   :", st.meta("鲨鱼"))

    pool = PersonaPool(INSTANCES_DIR)
    print("Banyan grants:", len(pool.grants("Banyan")))
    print("Sui grants   :", pool.grants("Sui"))
    print("sui has diy_sui:", pool.is_available("Sui", "diy_sui"))
    print("sui has celestia:", pool.is_available("Sui", "celestia"))

    docs = [Doc({"id": p.name, "display_name": p.name})
            for p in (PDATA / "personas").iterdir() if p.is_dir()]
    m = pool.matrix(docs, [{"id": "Banyan"}, {"id": "Sui", "enabled": False}])
    print("matrix insts:", [(r["id"], len(r["grants"])) for r in m["instances"]])
    print("matrix personas:", len(m["personas"]))
    d = pool.resolve_default("Sui", docs, "celestia")
    print("Sui default resolves to:", getattr(d, "id", None))

    cfg_b = json.loads((INSTANCES_DIR / "Banyan" / "config.json").read_text("utf-8-sig"))
    cfg_s = json.loads((INSTANCES_DIR / "Sui" / "config.json").read_text("utf-8-sig"))
    print("Banyan privilege:", cfg_b.get("privilege_uid"),
          "attention:", cfg_b.get("enable_attention"))
    print("Sui    privilege:", cfg_s.get("privilege_uid"),
          "attention:", cfg_s.get("enable_attention"),
          "persona_current:", cfg_s.get("persona_current"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
