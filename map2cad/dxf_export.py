"""GeoJSON -> DXF（AutoCAD / 中望 / 浩辰 / BricsCAD 等都能直接打开）。

设计要点：
* 单位是米，1 个图形单位 = 1 米，图上量出来的长度就是真实长度
* 每个要素按类别分到中文图层，颜色/线宽/线型都配好
* 道路按 OSM 的宽度标签画成有宽度的多段线，看起来就是一张地图
* 中文注记用 txt.shx + gbcbig.shx 大字体，AutoCAD 打开不乱码
"""
import datetime
import math
import os
import time

import ezdxf

from . import roadnet
from .classify import LAYERS, classify, classify_point
from .geometry import center as _center
from .projection import LocalProjector


def _truthy(v):
    return v is True or str(v).lower() in ("yes", "true", "1")

# 自定义以"米"为单位的线型（默认 CAD 线型太小，1:1 米制图上看不见）
LTYPES = {
    "DASH_M":   ([8.0, 5.0, -3.0], "虚线 米制"),
    "CENTER_M": ([30.0, 20.0, -4.0, 4.0, -2.0], "中心线 米制"),
    "HIDDEN_M": ([6.0, 3.0, -3.0], "隐线 米制"),
}


def _ensure_linetype(doc, name):
    if name in ("CONTINUOUS", "BYLAYER", "BYBLOCK"):
        return "CONTINUOUS"
    pattern, desc = LTYPES.get(name, ([8.0, 5.0, -3.0], name))
    if name not in doc.linetypes:
        try:
            doc.linetypes.add(name, pattern=pattern, description=desc)
        except Exception:  # noqa: BLE001
            return "CONTINUOUS"
    return name


def _ensure_layers(doc):
    for name, (color, ltype, lw) in LAYERS.items():
        lt = _ensure_linetype(doc, ltype)
        if name in doc.layers:
            layer = doc.layers.get(name)
            layer.color = color
            layer.dxf.linetype = lt
        else:
            layer = doc.layers.add(name, color=color, linetype=lt, lineweight=lw)
        layer.dxf.lineweight = lw if lw in (0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53,
                                            60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211) else 13


def _text_angle(p0, p1):
    ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
    if ang > 90:
        ang -= 180
    elif ang < -90:
        ang += 180
    return ang


def _best_label_anchor(pts):
    """在折线上找最长的一段，返回其中点和方向角。"""
    best = (0.0, pts[0], pts[-1])
    for i in range(len(pts) - 1):
        d = math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        if d > best[0]:
            best = (d, pts[i], pts[i + 1])
    _, a, b = best
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), _text_angle(a, b)


def _label_ok(placed, name, anchor, min_dist):
    """同名注记不要贴得太密。

    一条路常常被切成好几段，每段都带着同一个路名；不去重的话，
    「人民大道」会在一百米内重复印四五遍，图面很脏。
    """
    sites = placed.setdefault(name, [])
    for x, y in sites:
        if math.hypot(anchor[0] - x, anchor[1] - y) < min_dist:
            return False
    sites.append((anchor[0], anchor[1]))
    return True


LABEL_LAYER = "标注"

# 标注优先级：数字越大越先摆，摆不下才轮到后面的
_PRIO = {
    "标注": 45,        # 地名 / POI（OSM 上最显眼的那批）
    "道路-高速": 95, "道路-主干": 90, "道路-次干": 80,
    "道路-支路": 70, "道路-街巷": 55, "道路-步行": 50,
    "水系-面": 65, "水系-线": 60, "绿地": 58, "用地": 40, "停车场": 35,
}


def _prio(layer):
    return _PRIO.get(layer, 30)


# 值得优先出现的公共设施；其余店铺、餐饮排最后，图上位置不够就自动省略
_MAJOR_AMENITY = {"school", "university", "college", "hospital", "clinic",
                  "government", "townhall", "museum", "place_of_worship",
                  "police", "fire_station", "post_office", "library",
                  "theatre", "arts_centre", "courthouse", "prison", "embassy"}


def _point_style(props, th):
    """点标注的优先级与字高：地名最大，公共设施其次，店铺最小。"""
    if props.get("place"):
        return 100, th * 1.4
    if props.get("tourism") in {"hotel", "attraction", "museum", "viewpoint"} \
            or props.get("historic") or props.get("amenity") in _MAJOR_AMENITY \
            or props.get("office"):
        return 52, th * 0.9
    return 18, th * 0.8


def _prop_anchor(props, proj, bounds=None):
    """如果要素自带 OSM 的标注位置（_label_pos），就用它。

    这一条是「文字位置和 OSM 地图一致」的关键：OSM 的标注位置不是几何中心，
    而是它自己算好的落点（路名还会带一个倾斜角）。
    """
    pos = props.get("_label_pos")
    if not pos or len(pos) != 2:
        return None
    try:
        xy = proj(float(pos[0]), float(pos[1]))
    except (TypeError, ValueError):
        return None
    ang = props.get("_label_angle")
    if bounds and not (bounds[0] <= xy[0] <= bounds[2]
                       and bounds[1] <= xy[1] <= bounds[3]):
        return None                 # 落点在图外，退回按几何自己算
    try:
        return xy, float(ang) if ang is not None else 0.0
    except (TypeError, ValueError):
        return xy, 0.0


def _inside(ring, pt):
    """射线法：点是否在多边形内（ring 为 [(x,y), ...]）。"""
    x, y = pt
    ins = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-18) + x1:
                ins = not ins
    return ins


def _poly_label_anchor(pts):
    """给面要素找一个「落在里面、且离边最远」的点。

    OSM 画建筑名也不是简单用外框中心：L 形、凹形楼用外框中心会掉到楼外面，
    名字就飘到马路上了。这里取最大内切圆的圆心（近似）。
    """
    if not pts:
        return None
    c = _center(pts)
    if c and _inside(pts, c):
        return c
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    best, best_d = None, -1.0
    for i in range(1, 14):
        for j in range(1, 14):
            px = x0 + (x1 - x0) * i / 14.0
            py = y0 + (y1 - y0) * j / 14.0
            if not _inside(pts, (px, py)):
                continue
            d = min(_dist_to_edges(pts, (px, py)), 1e9)
            if d > best_d:
                best, best_d = (px, py), d
    return best or c


def _dist_to_edges(pts, p):
    best = 1e18
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx == 0 and dy == 0:
            d = math.hypot(p[0] - a[0], p[1] - a[1])
        else:
            t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
            d = math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))
        if d < best:
            best = d
    return best


def _text_width(txt, h):
    """粗略估一下文字占多宽（中文字宽≈1字高，英文≈0.55）。"""
    w = 0.0
    for ch in str(txt):
        w += h * (1.0 if ord(ch) > 0x2E80 else 0.55)
    return w


def _place_labels(msp, cands, bounds=None):
    """统一摆放标注：同名的别挤在一起，字和字之间也别叠。

    返回实际画出来的数量。
    """
    placed = {}          # 名字 -> 已放位置（同名去重）
    boxes = []           # 已占用的矩形（标题框，用于防重叠）
    n = 0
    for c in sorted(cands, key=lambda c: (-c["prio"], -len(c["text"]))):
        xy = c["xy"]
        if not xy:
            continue
        if bounds and not (bounds[0] <= xy[0] <= bounds[2]
                           and bounds[1] <= xy[1] <= bounds[3]):
            continue
        if not _label_ok(placed, c["text"], xy, c["gap"]):
            continue
        hw = _text_width(c["text"], c["h"]) / 2.0
        hh = c["h"] * 0.9
        rad = math.radians(c["angle"] or 0.0)
        ex = abs(hw * math.cos(rad)) + abs(hh * math.sin(rad))
        ey = abs(hw * math.sin(rad)) + abs(hh * math.cos(rad))
        if _box_taken(boxes, xy, ex, ey):
            continue
        boxes.append((xy[0], xy[1], ex, ey))
        _add_text(msp, c["text"], xy, c["h"], c["angle"] or 0.0, LABEL_LAYER)
        n += 1
    return n


def _box_taken(boxes, xy, ex, ey):
    """新标注和已有标注重叠超过三成，就认为挤不下。"""
    x, y = xy
    for bx, by, bex, bey in boxes:
        ox = min(x + ex, bx + bex) - max(x - ex, bx - bex)
        oy = min(y + ey, by + bey) - max(y - ey, by - bey)
        if ox <= 0 or oy <= 0:
            continue
        inter = ox * oy
        if inter > 0.3 * min(4 * ex * ey, 4 * bex * bey):
            return True
    return False


def _simplify_for(n_roads):
    """路越多，轮廓/填充的锯齿就越该收敛。

    大范围地图上，1 厘米的锯齿在 1:500 图纸上是 0.02 毫米，肉眼绝对看不见，
    但顶点数能少一半 —— 这就是「大图打开不卡」最有效的一刀。
    """
    if n_roads > 1500:
        return 0.12
    if n_roads > 600:
        return 0.05
    return 0.02


def _ring_area(pts):
    """多边形面积（鞋带公式），用来给填充块排大小。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# 下面这几个上限是专门为「CAD 打开不卡」定的：
# 填充是最容易拖慢 CAD 的东西，块数和顶点数必须封顶，宁可少填几块碎角。
FILL_MIN_AREA = 3.0       # 平方米：比这个还小的碎块不填（1:500 图上 <1 mm²）
FILL_MAX_PATHS = 1200     # 一个填充最多多少个边界块
FILL_MAX_VERTS = 60000    # 一个填充最多多少顶点


def _pick_fill_paths(rings):
    """按面积从大到小挑，挑到上限为止（碎块直接丢掉，CAD 才不会卡）。"""
    items = []
    for r in rings:
        if len(r) < 3:
            continue
        a = _ring_area(r)
        if a < FILL_MIN_AREA:
            continue
        items.append((a, r))
    items.sort(key=lambda t: -t[0])
    keep, nv = [], 0
    for _, r in items:
        if len(keep) >= FILL_MAX_PATHS or nv + len(r) > FILL_MAX_VERTS:
            break
        keep.append(r)
        nv += len(r)
    return keep


def _draw_road_fills(msp, fills, log=None):
    """按等级实体填充：低等级先建、高等级后建。

    CAD 是按「建实体的先后」画的，所以后建的高等级自然压住低等级 ——
    OSM 上主干压支路、支路压小路，就是这个道理。

    整级共用**一个** HATCH 实体（不是每块路一个），这是不卡的关键：
    一个 300 块面的填充，块数只算 1 个实体，CAD 重新生成只要一次。
    """
    from .classify import ROAD_FILL, ROAD_RANK

    n = 0
    for layer in sorted(fills, key=lambda g: ROAD_RANK.get(g, 0)):
        rgb = ROAD_FILL.get(layer)
        if not rgb or not fills.get(layer):
            continue
        paths = _pick_fill_paths(fills[layer])
        if not paths:
            continue
        try:
            h = msp.add_hatch(dxfattribs={"layer": "道路轮廓"})
            h.set_solid_fill(rgb=rgb)
            # 注意：set_solid_fill 会把 hatch_style 改成 1（只填最外圈），
            # 那样院子、天井这些洞会被一起填死。必须显式改回 0（普通），
            # 让「外环填、内环空」按奇偶规则走。
            h.dxf.hatch_style = 0
            for r in paths:
                h.paths.add_polyline_path(r, is_closed=True)
            n += 1
            if log:
                log(f"    填充 {layer}：{len(paths)} 块 / "
                    f"{sum(len(r) for r in paths)} 点")
        except Exception as e:      # noqa: BLE001
            if log:
                log(f"    填充 {layer} 失败（{type(e).__name__}: {str(e)[:40]}）")
    return n


def export_dxf(geojson, out_path, text_height=2.5, road_mode="band",
               label_mode="all", info_text=True, road_outline=True,
               road_fill=False, satellite=False, log=None):
    """主入口。返回统计 dict。

    road_fill   道路按等级实体填充（默认关：填充会盖住 `道路-xx` 的边线）
    satellite   卫星图底图（默认关；开的话会在 dxf 旁边多存一张 jpg）
    """
    feats = geojson.get("features", [])
    bbox = geojson.get("bbox")
    if not bbox:
        xs, ys = [], []
        for f in feats:
            for c in _iter_coords(f["geometry"]):
                xs.append(c[0]); ys.append(c[1])
        bbox = [min(xs), min(ys), max(xs), max(ys)]

    lon0 = (bbox[0] + bbox[2]) / 2
    lat0 = (bbox[1] + bbox[3]) / 2
    proj = LocalProjector(lon0, lat0)
    w, h = proj.bbox_size(tuple(bbox))
    bounds = (-w / 2 - 5, -h / 2 - 5, w / 2 + 5, h / 2 + 5)   # 图面范围，标注别跑出去

    # ---- 卫星图底图：OSM 没画的小区，照着卫片自己补描 ----
    # 放在建 doc 之前抓：抓不到就先降级，别把 DXF 写一半
    sat = None
    if satellite:
        try:
            from . import satellite as satmod
            sat_path = os.path.splitext(out_path)[0] + "_卫星图.jpg"
            sat = satmod.build(tuple(bbox), sat_path, log=log)
        except Exception as e:      # noqa: BLE001
            sat = None
            if log:
                log(f"  卫星图底图跳过（{type(e).__name__}: {str(e)[:70]}）")

    doc = ezdxf.new("R2013", setup=True)
    doc.header["$INSUNITS"] = 6          # 6 = 米
    doc.header["$MEASUREMENT"] = 1       # 公制
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3

    # 中文字体样式
    try:
        if "CN" not in doc.styles:
            style = doc.styles.add("CN", font="txt.shx")
            style.dxf.bigfont = "gbcbig.shx"
    except Exception:  # noqa: BLE001
        pass

    _ensure_layers(doc)
    msp = doc.modelspace()

    stats = {}
    n_poly = n_line = n_text = n_ring = 0
    skipped = 0
    cands = []                       # 待摆放的标注（先收集，再统一排布）

    def bump(layer):
        stats[layer] = stats.get(layer, 0) + 1

    def add_label(text, xy, angle, height, prio, gap):
        cands.append({"text": text, "xy": xy, "angle": angle,
                      "h": height, "prio": prio, "gap": gap})

    # ---- 卫星图一定要第一个塞进去：CAD 按实体先后叠放，先放的在最下面 ----
    if sat:
        try:
            from . import satellite as satmod
            satmod.attach(doc, msp, sat, w, h)
            stats["卫星图"] = 1
            if log:
                log(f"  卫星图已插入「卫星图」图层（{sat['px'][0]}×{sat['px'][1]} 像素，"
                    f"{sat['mpp']:.2f} 米/像素，和图纸坐标一一对应）")
        except Exception as e:      # noqa: BLE001
            if log:
                log(f"  卫星图插入失败（{type(e).__name__}: {str(e)[:60]}）")

    # ---- 先把每个要素的样式算好；路网轮廓那一步要用同一份宽度 ----
    prepared = []
    for f in feats:
        geom = f.get("geometry") or {}
        props = f.get("properties") or {}
        gtype = geom.get("type")
        if props.get("_style"):
            style = props["_style"]
        elif gtype == "Point":
            style = classify_point(props)[1]
        else:
            style = classify(props, gtype == "Polygon")[1]
        if style["layer"] not in LAYERS:
            style = dict(style, layer="其他")
        prepared.append({
            "f": f, "geom": geom, "props": props, "gtype": gtype,
            "style": style,
            "bridge": (gtype == "LineString"
                       and str(style["layer"]).startswith("道路")
                       and _truthy(props.get("bridge"))),
        })

    # ---- 道路轮廓：把整个路网并成一整片再抽边界（OSM 的路口就是这么连的） ----
    net, trim, clean = None, {}, {}
    # 「只画中心线」模式下路面本来就没有宽度，再套一圈轮廓就成了胖胖的怪东西
    if road_outline and road_mode != "band":
        if log:
            log("  选了「只画中心线」，道路轮廓这一版跳过")
    if road_outline and road_mode == "band":
        items = []
        for i, p in enumerate(prepared):
            if p["gtype"] != "LineString" or not str(p["style"]["layer"]).startswith("道路"):
                continue
            pts = [proj(x, y) for x, y in p["geom"]["coordinates"]]
            if len(pts) < 2:
                continue
            items.append({"idx": i, "pts": pts,
                          "w": p["style"]["const"] or 6.0,
                          "bridge": p["bridge"],
                          "g": p["style"]["layer"]})
        if len(items) > 15000:
            if log:
                log(f"  道路太多（{len(items)} 条），这一版先不出道路轮廓")
        elif items:
            t0 = time.time()
            try:
                net = roadnet.build(items, simplify=_simplify_for(len(items)),
                                    want_fills=road_fill)
                trim = net.get("trim") or {}
                clean = net.get("clean") or {}
                if log:
                    st = net["stats"]
                    log(f"  道路轮廓：{len(items)} 条路并成 {st['rings']} 个下层环"
                        f" + {st.get('bridge_rings', 0)} 个桥面环"
                        f"（桥 {st['bridges']} 处，路口/桥截断 {st['cut']} 条路，"
                        f"{time.time()-t0:.1f}s）")
            except Exception as e:      # noqa: BLE001
                net = None
                if log:
                    log(f"  道路轮廓没算出来（{type(e).__name__}: {str(e)[:60]}），"
                        f"这次只出路面")

    # 桥梁最后画，才能压住下面的路（先画会被后画的盖掉）
    order = ([i for i, p in enumerate(prepared) if not p["bridge"]]
             + [i for i, p in enumerate(prepared) if p["bridge"]])

    for i in order:
        p = prepared[i]
        f, geom, props, gtype, style = p["f"], p["geom"], p["props"], p["gtype"], p["style"]
        layer = style["layer"]

        attribs = {
            "layer": layer,
            "color": style["color"],
            "linetype": _ensure_linetype(doc, style["ltype"]),
        }

        if gtype == "Polygon":
            for ring in geom["coordinates"]:
                pts = [proj(x, y) for x, y in ring]
                if len(pts) < 3:
                    continue
                try:
                    msp.add_lwpolyline(pts, close=True, dxfattribs=attribs)
                    n_poly += 1
                    bump(layer)
                except Exception:  # noqa: BLE001
                    skipped += 1
            name = props.get("name")
            if name and label_mode == "all" and not props.get("_no_label"):
                anchor = _prop_anchor(props, proj, bounds)
                if anchor:
                    xy, ang = anchor
                else:
                    xy, ang = _poly_label_anchor(
                        [proj(x, y) for x, y in geom["coordinates"][0]]), 0.0
                if xy:
                    add_label(name, xy, ang, text_height, _prio(layer), 25.0)

        elif gtype == "LineString":
            pts = [proj(x, y) for x, y in geom["coordinates"]]
            if len(pts) < 2:
                continue
            # 被桥挖断的路，按挖好的几段分别画，桥面两侧才留得出白边；
            # 其余按清理过的中心线画，保证路面带宽和轮廓线严丝合缝。
            chunks = (trim.get(i) or
                      [clean.get(i) or pts])
            d = dict(attribs)
            if road_mode == "band" and style["const"] > 0:
                d["const_width"] = style["const"]
                d["lineweight"] = 0
            for chunk in chunks:
                if len(chunk) < 2:
                    continue
                try:
                    msp.add_lwpolyline(chunk, close=False, dxfattribs=d)
                    n_line += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
            bump(layer)

            name = props.get("name") or props.get("ref")
            if name and label_mode in ("road", "all") and layer.startswith("道路"):
                anchor = _prop_anchor(props, proj, bounds)
                if anchor:
                    xy, ang = anchor
                else:
                    xy, ang = _best_label_anchor(pts)
                add_label(name, xy, ang, text_height, _prio(layer), 60.0)

        elif gtype == "Point":
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            name = props.get("name")
            if name and label_mode in ("all", "road"):
                pr, hh = _point_style(props, text_height)
                add_label(name, proj(lon, lat), 0.0, hh, pr, 12.0)

    # ---- 按等级实体填充：低等级先填、高等级后填，路口自然被高等级压住 ----
    n_fill = 0
    if net and road_fill and net.get("fills"):
        n_fill = _draw_road_fills(msp, net["fills"], log=log)

    # ---- 道路轮廓线（画在填充之后，压在填充上才看得见） ----
    # 下层路网（路口融合、桥下已挖断）+ 上层桥面（独立成片）都画。
    if net and net.get("outline"):
        a = {"layer": "道路轮廓", "color": LAYERS["道路轮廓"][0],
             "linetype": "CONTINUOUS"}
        for ring in net["outline"]:
            pts = ring[:-1] if len(ring) > 2 and ring[0] == ring[-1] else ring
            if len(pts) < 3:
                continue
            try:
                msp.add_lwpolyline(pts, close=True, dxfattribs=a)
                n_ring += 1
                bump("道路轮廓")
            except Exception:  # noqa: BLE001
                skipped += 1
    # 上层桥面：独立成片，画在下层轮廓之上，和 OSM 一样桥「压」在下面路上
    if net and net.get("bridge_outline"):
        a = {"layer": "道路轮廓", "color": LAYERS["道路轮廓"][0],
             "linetype": "CONTINUOUS"}
        for ring in net["bridge_outline"]:
            pts = ring[:-1] if len(ring) > 2 and ring[0] == ring[-1] else ring
            if len(pts) < 3:
                continue
            try:
                msp.add_lwpolyline(pts, close=True, dxfattribs=a)
                n_ring += 1
                bump("道路轮廓")
            except Exception:  # noqa: BLE001
                skipped += 1

    # ---- 统一摆放标注：先放重要的（地名 > 主干路 > 其他路 > 建筑/POI） ----
    n_text = _place_labels(msp, cands, bounds)
    if n_text:
        stats[LABEL_LAYER] = stats.get(LABEL_LAYER, 0) + n_text

    # ---- 图面信息 ----
    if info_text:
        src = ("数据来源: OpenStreetMap contributors (ODbL 免费开放数据)"
               + ("　卫星影像: Esri World Imagery" if sat else ""))
        _add_text(msp,
                  f"{src}  范围: {w:.0f}m x {h:.0f}m  单位: 米  "
                  f"生成: {datetime.date.today()}",
                  (0, -h / 2 - text_height * 3), max(text_height * 0.9, 1.2), 0,
                  LABEL_LAYER)

    # ---- 视图 / 范围 ----
    half_w = w / 2 + 10
    half_h = h / 2 + 10
    try:
        doc.header["$EXTMIN"] = (-half_w, -half_h, 0)
        doc.header["$EXTMAX"] = (half_w, half_h, 0)
        doc.header["$LIMMIN"] = (-half_w, -half_h)
        doc.header["$LIMMAX"] = (half_w, half_h)
        doc.set_modelspace_vport(height=max(h, w) * 1.15, center=(0, 0))
    except Exception:  # noqa: BLE001
        pass

    doc.saveas(out_path)
    result = {
        "file": out_path,
        "features": len(feats),
        "polylines": n_poly + n_line + n_ring,
        "texts": n_text,
        "outline_rings": n_ring,
        "fill_blocks": n_fill,
        "skipped": skipped,
        "width_m": round(w, 1),
        "height_m": round(h, 1),
        "layers": stats,
        "satellite": None if not sat else {
            "file": os.path.basename(sat["file"]),
            "path": sat["file"],
            "px": list(sat["px"]),
            "mpp": round(sat["mpp"], 3),
            "zoom": sat["zoom"],
            "mb": round(sat["bytes"] / 1048576.0, 2),
            "failed_tiles": sat["failed"],
        },
    }
    if log:
        extra = f"，{n_fill} 个道路填充" if n_fill else ""
        if sat:
            extra += f"，卫星图 {sat['px'][0]}×{sat['px'][1]}"
        log(f"DXF 已生成：{n_poly + n_line} 条多段线，{n_text} 处注记{extra}")
    return result


def _add_text(msp, txt, pt, height, rotation, layer, dxfattribs_extra=None):
    attribs = {"layer": layer, "height": height, "rotation": rotation, "style": "CN"}
    if dxfattribs_extra:
        attribs.update(dxfattribs_extra)
    t = msp.add_text(str(txt)[:120], dxfattribs=attribs)
    try:
        from ezdxf.enums import TextEntityAlignment
        t.set_placement(pt, align=TextEntityAlignment.MIDDLE_CENTER)
    except Exception:  # noqa: BLE001
        t.dxf.insert = (pt[0], pt[1])
    return t


def _iter_coords(geom):
    gtype = geom.get("type")
    if gtype == "Point":
        yield tuple(geom["coordinates"])
    elif gtype in ("LineString", "MultiPoint"):
        yield from geom["coordinates"]
    elif gtype in ("Polygon", "MultiLineString"):
        for part in geom["coordinates"]:
            yield from part
    elif gtype == "MultiPolygon":
        for poly in geom["coordinates"]:
            for ring in poly:
                yield from ring
