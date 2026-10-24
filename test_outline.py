"""「道路轮廓」图层自检（完全不联网）。

检查四件 OSM 道路轮廓最关键的事：
  1. 十字路口：两条路并成**一整片**（轮廓 1 个环，面积 = 并集面积，
     而不是两条带子各画一圈）
  2. 环形路（中间有院子）：外轮廓 + 内部空洞都要抽出来
  3. 立交跨线：桥面单独成片，桥下的路被挖断，两侧各留 1.6 米白边
  4. 关掉开关时，不多画一条轮廓线
"""
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from map2cad.dxf_export import export_dxf  # noqa: E402
from map2cad.projection import LocalProjector  # noqa: E402

try:
    from shapely.geometry import Polygon
    HAS_SHAPELY = True
except Exception:  # noqa: BLE001
    HAS_SHAPELY = False

OK, FAIL = [], []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra else ""))


def feat(coords, props, gtype="LineString"):
    return {"type": "Feature",
            "geometry": {"type": gtype, "coordinates": coords},
            "properties": props}


def run(features, bbox, **kw):
    """跑一次导出，返回 (result, doc)。"""
    fc = {"type": "FeatureCollection", "bbox": bbox, "features": features}
    out = os.path.join(tempfile.gettempdir(), "t_outline.dxf")
    res = export_dxf(fc, out, text_height=2.5, label_mode="none",
                     info_text=False, **kw)
    return res, ezdxf.readfile(out)


def layer_rings(doc):
    """取「道路轮廓」图层上的闭合多段线，返回 [(是否闭合, 点数, [(x,y)...])]。"""
    out = []
    for e in doc.modelspace().query('LWPOLYLINE[layer=="道路轮廓"]'):
        pts = [(p[0], p[1]) for p in e.get_points()]
        out.append((bool(e.closed), len(pts), pts))
    return out


def area_of(pts):
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def road(coords, w=8.0, hw="primary", layer="道路-主干", **extra):
    p = {"highway": hw, "layer": layer, "width": w}
    p.update(extra)
    return feat(coords, p)


# ============================================================ 1. 十字路口
# 两条 8 米宽的路在原点十字相交，长度各 100 米。
# 合起来的面积 = 800 + 800 - 8*8 = 1536 平方米。
# 如果轮廓没并起来，会是 1600 平方米 + 2 个环 —— 一眼就能看出来。
LAT0 = 31.2300
K = 1e-5
cross = [
    road([[121.4700, LAT0], [121.4720, LAT0]], 8.0),          # 东西向
    road([[121.4710, LAT0 - 0.0005], [121.4710, LAT0 + 0.0005]], 8.0),  # 南北向
]
res, doc = run(cross, [121.4700, LAT0 - 0.0005, 121.4720, LAT0 + 0.0005])
rings = layer_rings(doc)

proj = LocalProjector(121.4710, LAT0)
w_m = math.hypot(*[b - a for a, b in zip(proj(121.4700, LAT0),
                                         proj(121.4720, LAT0))])
h_m = math.hypot(*[b - a for a, b in zip(proj(121.4710, LAT0 - 0.0005),
                                         proj(121.4710, LAT0 + 0.0005))])
expect = w_m * 8 + h_m * 8 - 64

chk("十字路口并成 1 个环", len(rings) == 1, f"{len(rings)} 个环")
chk("轮廓线是闭合的", all(r[0] for r in rings))
got = sum(area_of(r[2]) for r in rings)
chk("轮廓面积 = 两路并集面积（不是各画一圈）",
    abs(got - expect) < expect * 0.01,
    f"{got:.0f} vs {expect:.0f} ㎡")
chk("路口处没有多余的内部环（没有空洞）", len(rings) == 1)
chk("「道路轮廓」层出现在统计里", res["layers"].get("道路轮廓", 0) == 1,
    str(res["layers"].get("道路轮廓")))
# 新行为：中心线在路口边界截断 → 两条路各断成 2 段，共 4 段带宽线。
# 旧行为是「两条连续带子」，现在路口内不画穿越线。
chk("两条路在路口各截成 2 段（共 4 段，路口内不画穿越线）",
    res["polylines"] - res["outline_rings"] == 4,
    f"{res['polylines'] - res['outline_rings']} 条")

# ============================================================ 2. 环形路
# 四条路围成 60×60 的方框，中间空出 44×44 的院子 —— 轮廓要 2 个环。
d = 0.0003
loop = [
    road([[121.4700, LAT0], [121.4700 + d, LAT0]], 8.0),
    road([[121.4700 + d, LAT0], [121.4700 + d, LAT0 + d]], 8.0),
    road([[121.4700 + d, LAT0 + d], [121.4700, LAT0 + d]], 8.0),
    road([[121.4700, LAT0 + d], [121.4700, LAT0]], 8.0),
]
res2, doc2 = run(loop, [121.4700, LAT0, 121.4700 + d, LAT0 + d])
rings2 = layer_rings(doc2)
chk("环形路抽出 2 个环（外轮廓 + 内部院子）", len(rings2) == 2, f"{len(rings2)} 个环")
sides = sorted(area_of(r[2]) for r in rings2)
chk("外轮廓比内轮廓大（没弄反）", len(sides) == 2 and sides[1] > sides[0] * 1.2,
    f"{sides[0]:.0f} / {sides[1]:.0f} ㎡")

# ============================================================ 3. 立交跨线
# 东西向的是桥（bridge=yes），南北向从桥下穿过。
# 期望：桥面 1 片 + 被挖断的南北路 2 段 = 3 个环；
# 南北路在离桥边 1.6 米处断开。
bridge_road = road([[121.4700, LAT0], [121.4720, LAT0]], 8.0, bridge="yes")
under_road = road([[121.4710, LAT0 - 0.0005], [121.4710, LAT0 + 0.0005]], 8.0)
res3, doc3 = run([bridge_road, under_road],
                 [121.4700, LAT0 - 0.0005, 121.4720, LAT0 + 0.0005])
rings3 = layer_rings(doc3)

# 桥上那条 / 桥下那条分别是哪个图层？两条都是 道路-主干，只能靠几何判断：
# 南北向那条的每一段都应该是竖直的窄条（宽度 8 米左右）。
vertical = []
for closed, n, pts in rings3:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if (max(ys) - min(ys)) > (max(xs) - min(xs)) * 1.5:
        vertical.append((min(ys), max(ys), max(xs) - min(xs)))

chk("跨线分成 3 片（桥面 + 上下两段）", len(rings3) == 3, f"{len(rings3)} 片")
chk("桥下的路断成两段", len(vertical) == 2, f"{len(vertical)} 段")
if len(vertical) == 2:
    vertical.sort(key=lambda v: v[0])      # 按下端位置排序：先南后北
    south, north = vertical
    mid = proj(121.4710, LAT0)[1]
    # 南段的北端、北段的南端，就是被挖断的那两个断面。
    # 用户要求不留白：断面紧贴桥面两侧，缝隙正好等于桥宽 8 米。
    chk("桥下两段紧贴桥面两侧断开（缝隙 = 桥宽 8 米，无留白）",
        7.6 < (north[0] - south[1]) < 8.4,
        f"缝隙 {north[0] - south[1]:.2f} m")
    chk("断面紧贴桥边（离桥中心 = 半桥宽 4 米，不留白）",
        abs(abs(north[0] - mid) - 4.0) < 0.35,
        f"北断面 {abs(north[0] - mid):.2f} m，"
        f"南断面 {abs(south[1] - mid):.2f} m")

chk("桥面轮廓是独立一片（没有被桥下的路连上去）", len(rings3) == 3)

# ============================================================ 4. 关掉开关
res4, doc4 = run(cross, [121.4700, LAT0 - 0.0005, 121.4720, LAT0 + 0.0005],
                 road_outline=False)
chk("关掉后没有轮廓线", len(layer_rings(doc4)) == 0)
chk("关掉后统计里没有「道路轮廓」", res4.get("outline_rings") == 0,
    str(res4.get("outline_rings")))

# ============================================================ 5. 几何自洽
# 轮廓围起来的面积，应该等于「路面带宽算出来的面积」。
# 这一条能抓住「轮廓和路面错位」这类最难看的毛病。
if HAS_SHAPELY and len(rings) == 1:
    poly = Polygon(rings[0][2])
    chk("轮廓多边形本身合法（没有自相交）", poly.is_valid)
    chk("轮廓面积 ≈ 带宽面积", abs(poly.area - expect) < expect * 0.01,
        f"{poly.area:.0f} vs {expect:.0f} ㎡")

# ============================================================ 6. 按等级实体填充
# 一级路（主干）横穿一条小路（街巷）。OSM 的画法是：小路先填，主干后填盖上去。
main_road = road([[121.4700, LAT0], [121.4720, LAT0]], 8.0, hw="primary")
path_road = road([[121.4710, LAT0 - 0.0005], [121.4710, LAT0 + 0.0005]], 8.0,
                 hw="residential")
res6, doc6 = run([path_road, main_road],
                 [121.4700, LAT0 - 0.0005, 121.4720, LAT0 + 0.0005],
                 road_fill=True)     # 填充现在是可选项（默认不填）
hatches = list(doc6.modelspace().query("HATCH"))


def hatch_paths(h):
    out = []
    for p in h.paths:
        try:
            pts = [(v[0], v[1]) for v in p.vertices]
        except Exception:  # noqa: BLE001
            continue
        if len(pts) >= 3:
            out.append(pts)
    return out


def parity_area(h):
    """按 CAD 的奇偶规则算这块填充实际填了多少面积。"""
    g = None
    for pts in hatch_paths(h):
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        g = poly if g is None else g.symmetric_difference(poly)
    return g.area if g is not None else 0.0


def band_area(doc, layer):
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    bufs = []
    for e in doc.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]'):
        w = e.dxf.get("const_width", 0) or 0
        pts = [(p[0], p[1]) for p in e.get_points()]
        if w > 0 and len(pts) >= 2:
            bufs.append(LineString(pts).buffer(w / 2, cap_style="flat",
                                             join_style="mitre", mitre_limit=2.5))
    return unary_union(bufs).area if bufs else 0.0


def full_road_area(feats):
    """按「完整中心线 + 宽度」算该批道路的真实路面面积（路口连成一片）。

    和 band_area 的区别：band_area 读的是 DXF 里已经被路口截断的带宽线，
    路口处会少一块；这里用原始坐标直接 buffer，路口自然重叠、连成一片，
    才是填充（HATCH）该对的基准。
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    proj = LocalProjector(121.4710, LAT0)
    bufs = []
    for f in feats:
        coords = f["geometry"]["coordinates"]
        pts = [proj(x, y) for x, y in coords]
        w = float(f["properties"].get("width", 8.0))
        bufs.append(LineString(pts).buffer(w / 2, cap_style="flat",
                                           join_style="mitre", mitre_limit=2.5))
    return unary_union(bufs).area if bufs else 0.0


chk("两条不同等级的路 → 2 个填充实体（每级一个）", len(hatches) == 2,
    f"{len(hatches)} 个")
if len(hatches) == 2:
    hi, lo = hatches[1], hatches[0]          # 后建的 = 高等级
    chk("先建低等级（街巷），后建高等级（主干）",
        (lo.dxf.get("true_color") or 0) == 0xFFFFFF
        and (hi.dxf.get("true_color") or 0) == 0xFCD6A4,
        f"#{lo.dxf.get('true_color') or 0:06X} → #{hi.dxf.get('true_color') or 0:06X}")
    chk("填充是实心（solid_fill=1）", all(h.dxf.solid_fill == 1 for h in hatches))
    chk("填充样式是「普通」（洞才会被挖出来）",
        all(h.dxf.hatch_style == 0 for h in hatches),
        str([h.dxf.hatch_style for h in hatches]))
    if HAS_SHAPELY:
        for h, layer, feats in ((lo, "道路-街巷", [path_road]),
                                (hi, "道路-主干", [main_road])):
            a, b = parity_area(h), full_road_area(feats)
            chk(f"{layer} 填充面积 = 该级完整路面面积（路口连成一片）",
                abs(a - b) < max(2.0, b * 0.005),
                f"{a:.0f} vs {b:.0f} ㎡")

# 填了色，路面本身的带宽线就压在下面看不见了 —— 这也是 OSM 的样子
chk("填充盖住路面后，路口里不再有穿线",
    len(hatches) == 2 and parity_area(hatches[0]) > 0)

# 院子（环形路）中间必须留空
res7, doc7 = run(loop, [121.4700, LAT0, 121.4700 + d, LAT0 + d], road_fill=True)
h7 = list(doc7.modelspace().query("HATCH"))
chk("环形路的填充有 2 条边界（外圈 + 院子）",
    len(h7) == 1 and len(hatch_paths(h7[0])) == 2,
    f"{len(hatch_paths(h7[0])) if h7 else 0} 条")
if h7 and HAS_SHAPELY:
    a = parity_area(h7[0])
    b = full_road_area(loop)
    chk("填了院子还是空的（面积对得上）", abs(a - b) < max(2.0, b * 0.005),
        f"{a:.0f} vs {b:.0f} ㎡")

# ============================================================ 7. 默认不填充
res8, doc8 = run(cross, [121.4700, LAT0 - 0.0005, 121.4720, LAT0 + 0.0005])
chk("默认不生成填充（没有 HATCH 实体）",
    len(list(doc8.modelspace().query("HATCH"))) == 0)
chk("默认统计里 fill_blocks = 0", res8.get("fill_blocks") == 0)
chk("默认轮廓线照常生成", len(layer_rings(doc8)) == 1)

# ============================================================ 8. 路口中心线截断
# 十字路口两条路，带宽线（道路-主干）应该在路口边界截断：
# 每条路变成 2 段，4 段的端点都停在路口边缘（离中心约半个路口宽），
# 路口内部不再有任何一条带宽线穿过去。
def road_lines(doc, layer="道路-主干"):
    """取该图层的带宽多段线（const_width > 0），返回 [(起,终)...] 投影坐标。"""
    out = []
    for e in doc.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]'):
        w = e.dxf.get("const_width", 0) or 0
        if w <= 0:
            continue
        pts = [(p[0], p[1]) for p in e.get_points()]
        out.append((pts[0], pts[-1]))
    return out

segs = road_lines(doc)     # cross 的两条路，都在 道路-主干
chk("十字路口带宽线截成 4 段（每条路 2 段）", len(segs) == 4, f"{len(segs)} 段")
if len(segs) == 4:
    # 路口中心在 (0,0)，路宽 8m → 半个路口 ≈ 4m；每条路被路口截成两段，
    # 每段有一个端点停在路口边缘（~4m），另一个是道路原本的远端终点。
    mid = proj(121.4710, LAT0)          # 路口中心投影坐标
    # 对每段，取「离中心更近的那个端点」= 截断点；它必须停在路口边缘 ~4m
    cut_ends = []
    for s in segs:
        a, b = s
        da = math.hypot(a[0] - mid[0], a[1] - mid[1])
        db = math.hypot(b[0] - mid[0], b[1] - mid[1])
        cut_ends.append(min(da, db))
    chk("每条截断段都有一个端点停在路口边缘（离中心 ≈ 半路宽 4m）",
        all(3.0 < g < 6.0 for g in cut_ends),
        f"截断点离中心 {[round(g, 1) for g in sorted(cut_ends)]} m")
    chk("截断点没有越过路口中心（不画穿越线）",
        all(g > 0.5 for g in cut_ends),
        f"{[round(g, 1) for g in cut_ends]}")

# ============================================================ 9. 上下层不连通
# 桥（上层）跨过下层路：桥面轮廓必须独立成片，不能跟下层路 union 成一块。
# 这里再验一次「桥面独立」+「下层断开」，用桥宽更大的情形确保留白对。
wide_bridge = road([[121.4700, LAT0], [121.4720, LAT0]], 12.0, bridge="yes")
under2 = road([[121.4710, LAT0 - 0.0005], [121.4710, LAT0 + 0.0005]], 8.0)
res9, doc9 = run([wide_bridge, under2],
                 [121.4700, LAT0 - 0.0005, 121.4720, LAT0 + 0.0005])
rings9 = layer_rings(doc9)
chk("桥（12m 宽）跨下层路 → 3 片（桥面 + 上下两段）",
    len(rings9) == 3, f"{len(rings9)} 片")

# ============================================================ 10. 贴近但不穿桥的道路不被误断
# 桥面：东西向、宽 8m（纵向半宽 4m ≈ 0.000036°），中心线在 LAT0。
# 一条「贴近但不穿桥」的路：南北向，经度在桥的经度范围中间（121.4710），
# 但只延伸到桥面南侧边缘之外一点点、不进入桥体（南端停在离桥中心 6m 外）。
# 旧实现掩膜外扩 1.6m 留白（半宽 4→5.6m），会把这条只是「靠近」的路误判为穿桥切断。
bridge_road = road([[121.4700, LAT0], [121.4720, LAT0]], 8.0, bridge="yes")
# 桥面纵向范围 LAT0±4m(≈0.000036°)；这条路的北端停在 LAT0-0.00006°（≈6.7m，
# 在桥面之外 2.7m，但落在旧 halo 的 5.6m 范围内），南端更远。
nearby = road([[121.4710, LAT0 - 0.00012], [121.4710, LAT0 - 0.00006]], 6.0)
# 这条在桥东端头之外（经度 121.4720+0.00005 ≈ 桥端东 5.6m，落在旧 halo 范围），不穿桥体
beyond = road([[121.47205, LAT0 - 0.00004], [121.47205, LAT0 + 0.00004]], 6.0)
res10, doc10 = run([bridge_road, nearby, beyond],
                   [121.4690, LAT0 - 0.0005, 121.4740, LAT0 + 0.0005])
# 两条贴近/擦过的路都应保持完整（各自 1 片），桥面独立成片
# 轮廓环数：桥面 1 片 + 两条完整路各 1 片 = 3 片
rings10 = layer_rings(doc10)
chk("贴近但不穿桥的路不被误断（仍是完整一条）",
    len(rings10) == 3, f"{len(rings10)} 片（应为 3：桥面 + 两条完整路）")
# 更直接地看带宽线：两条非桥路（宽 6m）各自应只有 1 段（没被截断）；
# 桥（宽 8m）本身也算 1 段，所以共 3 段，而非「贴近路被切成 2 段」的 5 段。
n_nearby = 0
for e in doc10.modelspace().query('LWPOLYLINE[layer=="道路-主干"]'):
    if abs((e.dxf.get("const_width", 0) or 0) - 6.0) < 0.01:
        n_nearby += 1
chk("两条贴近/擦过的路带宽线各自完整（各 1 段，未被截断）",
    n_nearby == 2, f"{n_nearby} 段")

print()
if FAIL:
    print(f"有 {len(FAIL)} 项没通过：" + "、".join(FAIL))
    sys.exit(1)
print(f"全部通过（{len(OK)} 项）")
