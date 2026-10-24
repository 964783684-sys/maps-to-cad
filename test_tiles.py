"""矢量瓦片通道自检（完全不联网）。

用一段手工编码的 MVT 二进制，验证：
  1. protobuf/几何解码正确（含 MoveTo/LineTo/ClosePath 与 zigzag）
  2. 瓦片内整数坐标 → 经纬度换算正确（误差 < 1 米）
  3. Shortbread 的 kind 能正确翻译成 OSM 标签，落到正确的 CAD 图层
  4. 路名能从 street_labels 贴回最近的道路
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from map2cad import mvt, tiles  # noqa: E402
from map2cad.overpass import filter_by_layers, osm_to_geojson  # noqa: E402

OK = []
FAIL = []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra else ""))


# ---------------- 手工编码一个最小瓦片 ----------------
def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(fid, wt):
    return varint((fid << 3) | wt)


def blob(fid, data):
    return tag(fid, 2) + varint(len(data)) + data


def zz(n):
    return (n << 1) ^ (n >> 63) if n >= 0 else ((-n) << 1) - 1


def enc_value_str(s):
    return blob(1, s.encode())


def enc_value_int(v):
    return tag(3, 0) + varint(v)


def enc_feature(fid, tags_idx, ftype, geom):
    b = tag(1, 0) + varint(fid)
    packed = b"".join(varint(t) for t in tags_idx)
    b += blob(2, packed)
    b += tag(3, 0) + varint(ftype)
    b += blob(4, b"".join(varint(g) for g in geom))
    return b


def enc_layer(name, keys, values, feats, extent=4096):
    b = blob(1, name.encode())
    for f in feats:
        b += blob(2, f)
    for k in keys:
        b += blob(3, k.encode())
    for v in values:
        b += blob(4, v)
    b += tag(5, 0) + varint(extent)
    return blob(3, b)


# 一个正方形建筑：(100,100)->(300,100)->(300,300)->闭合
# 注意 MVT 的坐标是 zigzag 编码后的"增量"，所以 100 -> 200，200 -> 400
#   9 = MoveTo 1 点， 18 = LineTo 2 点， 15 = ClosePath
# 矩形只需 3 个显式顶点，ClosePath 会自动补回起点，解出来是 4 个点。
geom = [9, 200, 200, 18, 400, 0, 0, 400, 15]
feat = enc_feature(1, [0, 0], 3, geom)
layer = enc_layer("buildings", ["kind"], [enc_value_str("building")], [feat])
tile_bytes = layer

layers = mvt.decode(tile_bytes)
chk("解出 buildings 图层", "buildings" in layers, str(list(layers)))
f = layers["buildings"]["features"][0]
chk("几何解成 1 个闭合环", len(f["geometry"]) == 1 and len(f["geometry"][0]) == 4,
    str(f["geometry"]))
chk("首尾点相同（闭合）", f["geometry"][0][0] == f["geometry"][0][-1])
chk("顶点坐标正确", f["geometry"][0][:3] == [(100, 100), (300, 100), (300, 300)],
    str(f["geometry"][0]))
chk("kind 属性解出", f["tags"].get("kind") == "building", str(f["tags"]))

# ---------------- 坐标换算 ----------------
z, tx, ty = 14, 13720, 6694
ext = 4096
lon = tiles.tile_x2lon(z, tx, 2048, ext)
lat = tiles.tile_y2lat(z, ty, 2048, ext)
chk("瓦片中心经度在 (121.46, 121.49)", 121.46 < lon < 121.49, f"{lon:.5f}")
chk("瓦片中心纬度在 (31.22, 31.24)", 31.22 < lat < 31.24, f"{lat:.5f}")

# 瓦片边长 ≈ 2092m，1 单位 ≈ 0.51m
step = 1
lon_a = tiles.tile_x2lon(z, tx, 1000, ext)
lon_b = tiles.tile_x2lon(z, tx, 1000 + step, ext)
m_per_unit = (lon_b - lon_a) * 111320 * math.cos(math.radians(lat))
chk("1 坐标单位 ≈ 0.5 米", 0.45 < m_per_unit < 0.56, f"{m_per_unit:.3f} m")

# ---------------- kind -> OSM 标签 -> CAD 图层 ----------------
cases = [
    ({"kind": "trunk"}, {"highway": "trunk"}, "道路-主干"),
    ({"kind": "primary"}, {"highway": "primary"}, "道路-主干"),
    ({"kind": "secondary", "link": True}, {"highway": "secondary_link"}, "道路-次干"),
    ({"kind": "minor"}, {"highway": "residential"}, "道路-街巷"),
    ({"kind": "service"}, {"highway": "service"}, "道路-街巷"),
    ({"kind": "pedestrian"}, {"highway": "pedestrian"}, "道路-步行"),
    ({"rail": True}, {"railway": "rail"}, "铁路"),
]
good = True
for src, want, want_layer in cases:
    got = tiles._street_tags(src)
    if got != want:
        good = False
        print(f"       翻译不符：{src} -> {got}，期望 {want}")
chk("道路 kind → OSM 标签翻译正确", good)

# 落到图层
from map2cad.classify import classify  # noqa: E402
lay_of = {}
for src, _want, want_layer in cases:
    t = tiles._street_tags(src)
    _k, style = classify(t, False)
    lay_of[src["kind"] if "kind" in src else "rail"] = style["layer"]
chk("trunk 落到 道路-主干", lay_of["trunk"] == "道路-主干", lay_of["trunk"])
chk("pedestrian 落到 道路-步行", lay_of["pedestrian"] == "道路-步行", lay_of["pedestrian"])
chk("rail 落到 铁路", lay_of["rail"] == "铁路", lay_of["rail"])

# ---------------- 路名贴回最近道路 ----------------
els = [
    {"type": "way", "id": 1, "tags": {"highway": "primary"},
     "geometry": [{"lon": 121.4700, "lat": 31.2300},
                  {"lon": 121.4720, "lat": 31.2300}]},
    {"type": "way", "id": 2, "tags": {"highway": "residential"},
     "geometry": [{"lon": 121.4700, "lat": 31.2400},
                  {"lon": 121.4720, "lat": 31.2400}]},
]
labels = [([(121.4703, 31.23005), (121.4710, 31.23005)], "南京西路")]
n = tiles._attach_names(els, labels)
chk("路名贴到了最近的道路上", n == 1 and els[0]["tags"].get("name") == "南京西路",
    str(els[0]["tags"]))
chk("远处的道路没被误贴", "name" not in els[1]["tags"], str(els[1]["tags"]))

# ---------------- 端到端：元素 -> GeoJSON -> 图层筛选 ----------------
fc, stats = osm_to_geojson({"elements": els}, (121.469, 31.229, 121.473, 31.241),
                           clip=True, log=None)
chk("瓦片元素能转成 GeoJSON", len(fc["features"]) == 2, str(stats))
fc2, stats2 = filter_by_layers(fc, ["highway"])
chk("按图层勾选筛选正常", len(fc2["features"]) == 2, str(stats2))
fc3, _ = filter_by_layers(fc, ["building"])
chk("不勾道路时筛得掉", len(fc3["features"]) == 0)

# ---------------- 缩放级别选择 ----------------
z_small = tiles.zoom_for((121.47, 31.23, 121.475, 31.235))
chk("1km² 用最高级别 z14", z_small == 14, f"z{z_small}")
z_big = tiles.zoom_for((121.0, 31.0, 122.0, 32.0))
chk("超大范围自动降级", z_big < 14, f"z{z_big}")

print()
print("全部通过" if not FAIL else f"有 {len(FAIL)} 项没通过")
sys.exit(0 if not FAIL else 1)
