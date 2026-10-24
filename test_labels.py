"""标注层自检（完全不联网）。

检查四件事，这四件正是「DXF 里看不到名字」最容易出错的地方：
  1. 建筑名字能从兴趣点（POI）挂到建筑轮廓上
  2. DXF 里所有文字都在「标注」图层
  3. 文字坐标 == OSM 给的标注点（误差 < 0.05 米），不是拍脑袋算的中心
  4. label_mode="none" 时真的一处文字都不产生
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from map2cad import tiles  # noqa: E402
from map2cad.dxf_export import LABEL_LAYER, export_dxf  # noqa: E402
from map2cad.projection import LocalProjector  # noqa: E402

OK, FAIL = [], []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra else ""))


BBOX = [121.4700, 31.2300, 121.4720, 31.2320]
RING = [[121.4700, 31.2300], [121.4720, 31.2300],
        [121.4720, 31.2320], [121.4700, 31.2320], [121.4700, 31.2300]]


def feat(gtype, coords, props):
    return {"type": "Feature",
            "geometry": {"type": gtype, "coordinates": coords},
            "properties": props}


fc = {"type": "FeatureCollection", "bbox": BBOX, "features": [
    # 一栋没有名字的建筑
    feat("Polygon", [RING], {"building": "yes", "layer": "建筑"}),
    # 落在建筑里的兴趣点（OSM 上「测试大厦」这个名字就挂在这个点上）
    feat("Point", [121.4710, 31.2310], {"name": "测试大厦", "tourism": "hotel",
                                        "layer": "标注"}),
    # 一条路，带 OSM 自己的标注位置
    feat("LineString", [[121.4701, 31.2305], [121.4720, 31.2305]],
         {"highway": "primary", "layer": "道路-主干", "name": "测试路",
          "_label_pos": [121.4710, 31.2305], "_label_angle": 8.0}),
    # 同一条路的另一段，标注位置一模一样 —— 应该被去重
    feat("LineString", [[121.4701, 31.2315], [121.4720, 31.2315]],
         {"highway": "primary", "layer": "道路-主干", "name": "测试路",
          "_label_pos": [121.4710, 31.2305], "_label_angle": 8.0}),
]}

# ---------------- 1. POI 名字挂到建筑上 ----------------
hit = tiles._names_from_pois(fc["features"])
bld = fc["features"][0]["properties"]
chk("建筑从兴趣点拿到了名字", bld.get("name") == "测试大厦", str(bld.get("name")))
chk("建筑标记了不再单独落字", bld.get("_no_label") is True)
chk("统计返回 1", hit == 1, str(hit))

# ---------------- 2/3. 导出并核对图层与坐标 ----------------
tmp = Path(tempfile.mkdtemp()) / "labels.dxf"
res = export_dxf(fc, str(tmp), label_mode="all", info_text=False)
doc = ezdxf.readfile(str(tmp))
msp = doc.modelspace()
texts = [e for e in msp if e.dxftype() == "TEXT"]

chk("DXF 里有「标注」图层", LABEL_LAYER in doc.layers, LABEL_LAYER)
chk("所有文字都在标注图层",
    all(t.dxf.layer == LABEL_LAYER for t in texts),
    str({t.dxf.layer for t in texts}))
names = [t.dxf.text for t in texts]
chk("建筑名出现在图上", "测试大厦" in names, str(names))
chk("路名出现在图上", "测试路" in names, str(names))
chk("同一路名只标一次（去重生效）", names.count("测试路") == 1, str(names))

proj = LocalProjector((BBOX[0] + BBOX[2]) / 2, (BBOX[1] + BBOX[3]) / 2)
ex, ey = proj(121.4710, 31.2305)
road_t = next(t for t in texts if t.dxf.text == "测试路")
dx = abs(road_t.dxf.insert.x - ex)
dy = abs(road_t.dxf.insert.y - ey)
chk("路名坐标 = OSM 标注点（误差 < 0.05 m）", dx < 0.05 and dy < 0.05,
    f"Δx={dx:.3f} Δy={dy:.3f}")
chk("路名跟着路的方向旋转", abs(abs(road_t.dxf.rotation) - 8.0) < 0.01,
    f"{road_t.dxf.rotation}")

ex2, ey2 = proj(121.4710, 31.2310)
poi_t = next(t for t in texts if t.dxf.text == "测试大厦")
chk("兴趣点文字落在兴趣点上（误差 < 0.05 m）",
    abs(poi_t.dxf.insert.x - ex2) < 0.05 and abs(poi_t.dxf.insert.y - ey2) < 0.05,
    f"Δ={abs(poi_t.dxf.insert.x - ex2):.3f},{abs(poi_t.dxf.insert.y - ey2):.3f}")

# ---------------- 4. 关掉标注 ----------------
tmp2 = Path(tempfile.mkdtemp()) / "none.dxf"
res2 = export_dxf(fc, str(tmp2), label_mode="none", info_text=False)
chk("label_mode=none 时没有文字", res2["texts"] == 0, str(res2["texts"]))

try:
    os.remove(tmp)
    os.remove(tmp2)
except OSError:
    pass

print()
print("全部通过" if not FAIL else f"有 {len(FAIL)} 项没通过")
sys.exit(0 if not FAIL else 1)
