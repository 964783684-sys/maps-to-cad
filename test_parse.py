"""验证 osm_to_geojson：解析 out geom、relation 环拼接、超界裁剪。

构造的是与 Overpass 真实返回完全一致的结构。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from map2cad.overpass import osm_to_geojson  # noqa: E402

BBOX = (121.4700, 31.2300, 121.4800, 31.2400)   # 约 960m x 1110m


def g(lon, lat):
    return {"lat": lat, "lon": lon}


osm = {
    "version": 0.6,
    "elements": [
        # 1) 普通闭合建筑
        {"type": "way", "id": 1,
         "tags": {"building": "yes", "name": "测试大楼"},
         "geometry": [g(121.4740, 31.2340), g(121.4745, 31.2340),
                      g(121.4745, 31.2345), g(121.4740, 31.2345),
                      g(121.4740, 31.2340)]},

        # 2) 道路（不闭合，带 name 和 lanes）
        {"type": "way", "id": 2,
         "tags": {"highway": "primary", "name": "人民大道", "lanes": "6"},
         "geometry": [g(121.4710, 31.2350), g(121.4750, 31.2350),
                      g(121.4790, 31.2352)]},

        # 3) relation 建筑：outer 由两段 way 组成，必须拼成一个环
        {"type": "relation", "id": 3,
         "tags": {"type": "multipolygon", "building": "commercial", "name": "环形商场"},
         "members": [
             {"type": "way", "role": "outer", "ref": 31,
              "geometry": [g(121.4750, 31.2320), g(121.4760, 31.2320),
                           g(121.4760, 31.2330)]},
             {"type": "way", "role": "outer", "ref": 32,
              "geometry": [g(121.4760, 31.2330), g(121.4750, 31.2330),
                           g(121.4750, 31.2320)]},
         ]},

        # 4) 超出边界的超长道路（要被裁剪）
        {"type": "way", "id": 4,
         "tags": {"highway": "motorway", "name": "外环高速"},
         "geometry": [g(121.4000, 31.2300), g(121.6000, 31.2400)]},

        # 5) 超出边界的大面（要被裁剪）
        {"type": "way", "id": 5,
         "tags": {"landuse": "residential", "name": "超大小区"},
         "geometry": [g(121.4000, 31.2000), g(121.6000, 31.2000),
                      g(121.6000, 31.2800), g(121.4000, 31.2800),
                      g(121.4000, 31.2000)]},

        # 6) 水系面
        {"type": "way", "id": 6,
         "tags": {"natural": "water", "name": "小湖"},
         "geometry": [g(121.4770, 31.2360), g(121.4780, 31.2360),
                      g(121.4780, 31.2370), g(121.4770, 31.2370),
                      g(121.4770, 31.2360)]},

        # 7) 地名点
        {"type": "node", "id": 7, "lat": 31.2350, "lon": 121.4750,
         "tags": {"place": "suburb", "name": "测试片区"}},

        # 8) 铁路
        {"type": "way", "id": 8,
         "tags": {"railway": "rail", "name": "测试线"},
         "geometry": [g(121.4710, 31.2380), g(121.4790, 31.2380)]},
    ],
}

fc, stats = osm_to_geojson(osm, BBOX, clip=True, log=print)

print("\n--- 检查项 ---")
checks = []


def check(name, cond, extra=""):
    checks.append((name, cond, extra))
    print(("  OK   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))


by_name = {}
for f in fc["features"]:
    nm = f["properties"].get("name")
    if nm:
        by_name.setdefault(nm, []).append(f)

# relation 拼接成一个多边形
rel = by_name.get("环形商场", [])
check("relation 两段 outer 拼成 1 个多边形", len(rel) == 1 and rel[0]["geometry"]["type"] == "Polygon",
      f"实际 {len(rel)} 个")
if rel:
    ring = rel[0]["geometry"]["coordinates"][0]
    check("拼接后的环闭合", ring[0] == ring[-1], ring[0] == ring[-1])

# 超长线被裁剪
mw = by_name.get("外环高速", [])
if mw:
    xs = [c[0] for c in mw[0]["geometry"]["coordinates"]]
    check("超界道路被裁剪进 bbox", min(xs) >= BBOX[0] - 1e-9 and max(xs) <= BBOX[2] + 1e-9,
          f"x 范围 {min(xs):.5f}~{max(xs):.5f}")

# 超界大面被裁剪
d = by_name.get("超大小区", [])
if d:
    xs = [c[0] for c in d[0]["geometry"]["coordinates"][0]]
    ys = [c[1] for c in d[0]["geometry"]["coordinates"][0]]
    inside = (min(xs) >= BBOX[0] - 1e-9 and max(xs) <= BBOX[2] + 1e-9
              and min(ys) >= BBOX[1] - 1e-9 and max(ys) <= BBOX[3] + 1e-9)
    check("超界大面被裁剪进 bbox", inside, f"{min(xs):.4f}~{max(xs):.4f}")

# 道路宽度按车道推算
rd = by_name.get("人民大道", [])
if rd:
    st = rd[0]["properties"]["_style"]
    check("6 车道主干道推算宽度 >= 18m", st["const"] >= 18, f"{st['const']}m / {st['layer']}")

# 分类正确
check("建筑归到「建筑」图层",
      by_name.get("测试大楼", [{}])[0]["properties"]["layer"] == "建筑")
check("湖归到「水系-面」图层",
      by_name.get("小湖", [{}])[0]["properties"]["layer"] == "水系-面")
check("铁路归到「铁路」图层",
      by_name.get("测试线", [{}])[0]["properties"]["layer"] == "铁路")
check("地名点为 Point",
      by_name.get("测试片区", [{}])[0]["geometry"]["type"] == "Point")
# 点的坐标必须是 [经度, 纬度] 两个数字，不能套一层（否则画注记时会崩）
_pt = by_name.get("测试片区", [{}])[0]["geometry"]["coordinates"]
check("点坐标是 [经度, 纬度] 两层结构不嵌套",
      isinstance(_pt, list) and len(_pt) == 2
      and all(isinstance(v, (int, float)) for v in _pt), str(_pt))

ok = all(c[1] for c in checks)
print(f"\n统计：{stats}")
print("全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
