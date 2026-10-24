"""把一条条道路中心线并成一整片路面，再抽出 OSM 风格的「道路轮廓」。

为什么必须并成一片
------------------
CAD 里如果每条路各画一条带宽度（const_width）的多段线，路口处两条路的
边线会互相穿过去，看上去像两条带子叠在一起。OSM 的路口是连成一片的：
边线沿路口外缘走，中间没有多余的线。

做法（用 shapely 做几何布尔运算）：
  1. 每条中心线按真实路宽向外扩成一块「路面」多边形；
  2. 同一层（桥下/桥上分开）的路面全部 union 成一整片 —— 路口自然融合；
  3. 取这一整片的外边界 + 内部空洞，就是轮廓线；
  4. 桥梁单独成片，并且把**它下面**的路面挖掉桥面（紧贴桥面，不留白），
     于是桥「压」在下面的路上，和 OSM 的跨桥画法一致。

挖的时候有个坑：跨桥的道路会被挖断，但**引桥（上桥的那截路）不能被挖断**，
否则桥和路之间会多一道缝。所以规则是：只挖「穿过桥体内部」的那一段，
端点本来就停在桥上的（引桥、匝道）不挖；**只是靠近桥、没有真正穿过桥体的
路（直线、大弧形）也不会被误断** —— 判断穿不穿桥只看桥面本身，不往外扩。
"""
import math

try:                                    # 缺 shapely 也不至于让整个工具挂掉
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    HAS_SHAPELY = True
except Exception:                       # noqa: BLE001
    HAS_SHAPELY = False

# 桥面两侧留白（米）。历史参数，保留接口兼容；当前画法「紧贴桥面、不留白」，
# 掩膜直接用桥面本身，不再按此值外扩。
BRIDGE_GAP = 1.6

# 面积小于这个的路面碎块 / 空洞不要（缓冲运算会产生芝麻大的碎块）
MIN_AREA = 1.5


# ------------------------------------------------------------------ 基础几何
def _line(pts, min_step=0.02):
    """去重后建 LineString；点太少或总长为 0 就返回 None。

    min_step 不能太小：瓦片坐标量化到 0.5 米，投影后常有相距几毫米的
    "伪顶点"，会让斜接运算挤出长尖刺，CAD 里看就是一根毛刺。
    """
    clean = []
    for p in pts:
        if not clean or math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > min_step:
            clean.append((float(p[0]), float(p[1])))
    if len(clean) < 2:
        return None
    line = LineString(clean)
    return line if line.length > 0.05 else None


def _fillet(pts, radius, turn_deg=22.0):
    """把急转弯打磨成圆角。

    为什么必须做：CAD 里带宽多段线在拐角是"尖角斜接"，一条 22 米宽的路
    在 150° 的急转角上会挤出一根四十米的长刺；几何缓冲虽然可以设上限，
    但设小了轮廓就比路面带宽窄，设大了又是一堆刺。把拐角磨圆，两边都干净。
    """
    if len(pts) < 3 or radius <= 0:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        cos = max(-1.0, min(1.0, cos))
        if math.degrees(math.acos(cos)) < turn_deg:
            out.append(b)
            continue
        d = min(radius, l1 * 0.45, l2 * 0.45)
        if d < 0.2:
            out.append(b)
            continue
        p1 = (b[0] - v1[0] / l1 * d, b[1] - v1[1] / l1 * d)
        p2 = (b[0] + v2[0] / l2 * d, b[1] + v2[1] / l2 * d)
        if math.hypot(p1[0] - out[-1][0], p1[1] - out[-1][1]) > 0.05:
            out.append(p1)
        for t in (0.25, 0.5, 0.75):
            mt = 1.0 - t
            out.append((mt * mt * p1[0] + 2 * mt * t * b[0] + t * t * p2[0],
                        mt * mt * p1[1] + 2 * mt * t * b[1] + t * t * p2[1]))
        out.append(p2)
    out.append(pts[-1])
    return out


def centerline(pts, width):
    """统一入口：清理 + 圆角。路面带宽和轮廓都必须基于这一份线，否则对不上。"""
    line = _line(pts)
    if line is None:
        return None
    half = max(float(width or 0.0), 1.2) / 2.0
    return LineString(_fillet(list(line.coords), max(half * 1.1, 1.0)))


def _buffer(line, width):
    """LineString -> 路面多边形（平头、斜接，跟 CAD 带宽多段线的画法一致）。

    注意 `buffer(0)`：斜接在急转弯处会把多边形搞成"自相交"，而自相交的
    多边形丢进 unary_union 会被**悄悄丢掉** —— 表现就是某条路在轮廓里
    凭空消失。所以这里统一修一遍。
    """
    half = max(float(width or 0.0), 1.2) / 2.0
    try:
        poly = line.buffer(half, cap_style="flat", join_style="mitre",
                           mitre_limit=2.5)
    except Exception:                   # noqa: BLE001
        poly = line.buffer(half)
    if poly is not None and not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def corridor(pts, width):
    """中心线坐标 -> 路面多边形（对外的老接口）。"""
    line = centerline(pts, width)
    if line is None:
        return None
    return _buffer(line, width)


def _union(polys):
    polys = [p for p in polys if p is not None and not p.is_empty]
    if not polys:
        return Polygon()
    if len(polys) == 1:
        return polys[0]
    return unary_union(polys)


def _geoms(geom):
    if geom is None or geom.is_empty:
        return []
    if hasattr(geom, "geoms"):
        return list(geom.geoms)
    return [geom]


# ------------------------------------------------------------------ 轮廓提取
def _ring_pts(coords):
    """去掉挨在一起的重复点和首尾重复点。

    这里**不做取整**：实测取整到毫米会让 4 个环出现 1 毫米级的自相交
    （取整后两个点跑到一起、短边翻了个方向），而 CAD 里自相交的闭合线
    是不能直接 HATCH 填充、也不能转成面的，用户一量就报错。
    坐标保留原始精度，环就一定是简单闭合环。
    """
    out = []
    for x, y in coords:
        p = (float(x), float(y))
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _valid_ring(pts):
    """兜底：万一某个环还是自相交，修一遍再交出去（宁可磨掉半毫米也不能交坏环）。"""
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if poly.is_valid:
        return pts
    fixed = poly.buffer(0)
    if fixed.is_empty:
        return None
    if fixed.geom_type == "MultiPolygon":
        fixed = max(fixed.geoms, key=lambda g: g.area)
    ring = _ring_pts(list(fixed.exterior.coords))
    return ring if len(ring) >= 3 else None


def rings(geom, simplify=0.02, min_area=MIN_AREA):
    """多边形（组）-> 一串环（外轮廓 + 内部空洞），坐标是 (x, y) 米。"""
    out = []
    for poly in _geoms(geom):
        if poly.geom_type != "Polygon" or poly.area < min_area:
            continue
        if simplify:
            poly = poly.simplify(simplify, True)
        ext = _valid_ring(_ring_pts(poly.exterior.coords))
        if ext:
            out.append(ext)
        for hole in poly.interiors:
            if Polygon(hole).area < min_area:
                continue
            ring = _valid_ring(_ring_pts(hole.coords))
            if ring:
                out.append(ring)
    return out


# ------------------------------------------------------------------ 挖桥
def _inside_intervals(line, poly):
    """中心线落在 poly 内部的参数区间（按弧长），已合并重叠。"""
    try:
        inter = line.intersection(poly)
    except Exception:                   # noqa: BLE001
        return []
    spans = []
    total = line.length
    for g in _geoms(inter):
        if g.geom_type != "LineString" or g.length < 1e-6:
            continue
        try:
            a = line.project(Point(g.coords[0]))
            b = line.project(Point(g.coords[-1]))
        except Exception:               # noqa: BLE001
            continue
        a, b = min(a, b), max(a, b)
        a, b = max(0.0, a), min(total, b)
        if b > a:
            spans.append((a, b))
    if not spans:
        return []
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(s) for s in merged]


def _chord_clear(line, t, half, poly):
    """在弧长 t 处切一刀：垂直于线路、跨度 = 一个路宽，这一刀是否已完全离开 poly。"""
    total = line.length
    t = min(max(t, 0.0), total)
    p = line.interpolate(t)
    q1 = line.interpolate(max(0.0, t - 0.6))
    q2 = line.interpolate(min(total, t + 0.6))
    dx, dy = q2.x - q1.x, q2.y - q1.y
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        return True
    nx, ny = -dy / norm, dx / norm
    chord = LineString([(p.x - nx * half, p.y - ny * half),
                        (p.x + nx * half, p.y + ny * half)])
    try:
        inter = chord.intersection(poly)
    except Exception:                   # noqa: BLE001
        return True
    if inter.is_empty:
        return True
    # 只是擦到桥的边（长度几乎为 0）不算切进去，否则会多退半米到一米
    return inter.length < 0.15


def _push_back(line, t, half, poly, forward):
    """沿线路往「离开桥」的方向挪，直到这一刀不再切到桥的留白。"""
    step = max(0.25, half * 0.3)
    total = line.length
    for _ in range(400):
        if _chord_clear(line, t, half, poly):
            return t
        t += step if forward else -step
        if t <= 0.0:
            return 0.0
        if t >= total:
            return total
    return t


def split_by_halo(line, halo, width, halo_core=None):
    """按桥的留白把一条中心线切开，返回若干段坐标列表。

    只切「穿过去」的部分；端点本来就在桥上（引桥 / 匝道）的一端不切。
    halo_core 是 halo 往里缩一点点，用来判断「这一刀有没有真的切进去」，
    免得断面正好压在桥边上时来回试探。
    """
    total = line.length
    spans = _inside_intervals(line, halo)
    if not spans:
        return [list(line.coords)]
    # 端点落在桥里的区间不当成「穿过」，那是引桥
    inner = [(a, b) for a, b in spans if a > 0.3 and b < total - 0.3]
    if not inner:
        return [list(line.coords)]

    keep, cursor = [], 0.0
    for a, b in inner:
        if a > cursor:
            keep.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < total:
        keep.append((cursor, total))

    core = halo_core if halo_core is not None else halo
    half = max(float(width or 0.0), 1.2) / 2.0
    out = []
    for a, b in keep:
        if a > 0.0:                      # 起点是被刀切出来的，往后退到断面离开桥
            a = _push_back(line, a, half, core, True)
        if b < total:                    # 终点同理，往前退
            b = _push_back(line, b, half, core, False)
        if b - a < 0.4:
            continue
        pts = _substring(line, a, b)
        if len(pts) >= 2:
            out.append(pts)
    return out or [list(line.coords)]


def _substring(line, a, b):
    """取中心线 [a, b] 弧长段。"""
    from shapely.ops import substring
    return [(x, y) for x, y in substring(line, a, b).coords]


# ------------------------------------------------------------------ 路口截断
def _junction_mask(lower_polys, idxs):
    """找出「路口」：两条路面缓冲面相交的区域，union 成一张路口掩膜。

    路口是 OSM 道路轮廓最讲究的地方：多条路交汇处要连成一片、不能有穿线。
    用「缓冲面两两相交」来定位路口，比「中心线相交」稳健 —— 数据里两条路
    的中心线常常因为量化误差并没有严格交于一点，但路面一定是重叠的。
    """
    if len(lower_polys) < 2:
        return None
    from shapely.strtree import STRtree
    tree = STRtree(lower_polys)
    parts = []
    for i, poly in enumerate(lower_polys):
        if poly is None or poly.is_empty:
            continue
        for j in tree.query(poly):
            if j <= i:
                continue
            other = lower_polys[j]
            if other is None or other.is_empty:
                continue
            try:
                if not poly.intersects(other):
                    continue
                inter = poly.intersection(other)
            except Exception:           # noqa: BLE001
                continue
            if inter.is_empty:
                continue
            # 只认「真的有个路口」：重叠区面积达到某条路半宽的平方量级。
            # 平行相邻的路缓冲面可能擦到一点点边，那不算路口，别乱切。
            try:
                a = inter.area
            except Exception:           # noqa: BLE001
                continue
            if a < 0.5:                # 小于 0.5㎡ 的重叠当噪声
                continue
            parts.append(inter)
    if not parts:
        return None
    return _union(parts)


def _cut_by_mask(line, mask, min_len=0.4):
    """中心线移除落在 mask 内部的部分，返回若干段坐标列表。

    这就是「中心线在路口边界处截断」：线穿过路口的那一截被拿掉，
    路口内部不画穿越线，剩下的几段从路口边界重新起头。
    """
    try:
        rem = line.difference(mask)
    except Exception:                   # noqa: BLE001
        return [list(line.coords)]
    out = []
    for g in _geoms(rem):
        if g.geom_type != "LineString" or g.length < min_len:
            continue
        out.append([(float(x), float(y)) for x, y in g.coords])
    return out or [list(line.coords)]


# ------------------------------------------------------------------ 对外入口
def build(roads, gap=BRIDGE_GAP, simplify=0.02, want_fills=False):
    """roads: [{"idx": 序号, "pts": [(x,y)...], "w": 路宽米,
                "bridge": bool, "g": 等级名（道路-主干 之类，可不填）}]

    画法和 OSM 对齐：
      * 路口 —— 面状整体表示：路面缓冲面两两相交处 union 成路口面，
        中心线在路口边界处截断，路口内部不画穿越线，道路与路口连成一片；
      * 高架/桥 —— 上层独立线路：桥面单独并成一片（不跟下层连），
        上层桥面当掩膜擦掉下层路面，下层在桥两侧断开、断口紧贴桥面
        （不留白），桥下不画下层连续线；只擦真正穿过桥体的路，
        贴近但不穿桥的路保持完整；
      * 上下层道路不建立连通关系。

    返回
        outline         下层路网（路口融合、桥下已挖断）的轮廓环
        bridge_outline  上层桥面独立轮廓环（与下层不连通）
        fills           {等级名: [环, ...]}（要 want_fills 才算）
        trim            {idx: [切开后的几段坐标]}：被路口/桥截断的路，导出用它画带宽线
        clean           {idx: [清理后的中心线坐标]}
        stats           数量统计
    """
    if not HAS_SHAPELY:
        return {"outline": [], "fills": {}, "bridge_outline": [], "trim": {},
                "clean": {}, "stats": {"error": "没有几何库"}}

    below, bridge_items, cleaned = [], [], {}
    for r in roads:
        line = centerline(r.get("pts") or [], r.get("w"))
        if line is None:
            continue
        cleaned[r["idx"]] = [(x, y) for x, y in line.coords]
        (bridge_items if r.get("bridge") else below).append((r, line))

    group_polys = {}

    def add(group, poly):
        if poly is not None and not poly.is_empty and want_fills:
            group_polys.setdefault(group or "", []).append(poly)

    # ---------- 上层：桥面独立并成一片（不跟下层混） ----------
    bridge_polys = []
    for r, line in bridge_items:
        poly = _buffer(line, r.get("w"))
        if poly is not None:
            bridge_polys.append(poly)
            add(r.get("g"), poly)
    bridge_geom = _union(bridge_polys)

    # ---------- 桥掩膜：用桥面本身擦下层（不留白，下层紧贴桥面） ----------
    # 用户要求下层路与上层桥面交接处紧贴、不留 1.6 米制图留白。
    # 所以掩膜直接取桥面（不再 buffer gap 外扩）；只做一点点负缓冲，
    # 保证下层断口正好贴齐桥面轮廓，不会因浮点误差差出头发丝。
    halo = halo_core = None
    if not bridge_geom.is_empty:
        halo = bridge_geom
        try:
            halo_core = halo.buffer(-0.03)
        except Exception:               # noqa: BLE001
            halo_core = halo

    # ---------- 下层：先算路面缓冲面，再找路口 ----------
    full_polys = []                     # 每条路的完整路面（轮廓面 union 用）
    lower_polys = []
    for r, line in below:
        poly = _buffer(line, r.get("w"))
        full_polys.append(poly)
        lower_polys.append(poly)
        add(r.get("g"), poly)           # 填充按完整路面分组（环形路才抽得出院子）

    junction = _junction_mask(lower_polys, [r["idx"] for r, _ in below])

    # ---------- 截断中心线：先按路口截，再按桥掩膜切 ----------
    # 这一刀只影响「带宽多段线」（道路-xx 彩色路面），让它在路口/桥下断开；
    # 轮廓面用的是完整路面 union（下面单独算），路口照样连成一片。
    trim, n_cut = {}, 0
    for r, line in below:
        pieces = [list(line.coords)]
        # 第一步：路口截断（路口内部不画穿越线）
        if junction is not None and not junction.is_empty:
            try:
                if line.intersects(junction):
                    pieces = _cut_by_mask(line, junction)
            except Exception:           # noqa: BLE001
                pieces = [list(line.coords)]
        # 第二步：桥掩膜切（跨桥的路在桥两侧断开）
        if halo is not None and not halo.is_empty:
            try:
                merged = []
                for seg in pieces:
                    seg_line = LineString(seg)
                    if seg_line.intersects(halo):
                        merged.extend(split_by_halo(seg_line, halo,
                                                    r.get("w"), halo_core))
                    else:
                        merged.append(seg)
                pieces = merged or [list(line.coords)]
            except Exception:           # noqa: BLE001
                pass
        if pieces != [list(line.coords)]:
            trim[r["idx"]] = pieces
            if len(pieces) > 1:
                n_cut += 1

    # ---------- 下层最终面：完整路面并成一片（路口连成一片），再挖掉桥掩膜 ----------
    below_geom = _union(full_polys)
    if halo is not None and not halo.is_empty and not below_geom.is_empty:
        try:
            below_geom = below_geom.difference(halo)
        except Exception:               # noqa: BLE001
            pass

    out_rings = rings(below_geom, simplify)
    bridge_rings = rings(bridge_geom, simplify)

    # ---------- 分级填充：每级单独求并（后画的高等级自然盖住低等级） ----------
    fills = {}
    for g, ps in group_polys.items():
        if not g:
            continue
        try:
            r = rings(_union(ps), simplify)
        except Exception:               # noqa: BLE001
            continue
        if r:
            fills[g] = r

    return {
        "outline": out_rings,
        "fills": fills,
        "clean": cleaned,
        "bridge_outline": bridge_rings,
        "_geom": below_geom,
        "_bridge_geom": bridge_geom,
        "trim": trim,
        "stats": {
            "roads": len(roads),
            "below": len(below),
            "bridges": len(bridge_polys),
            "cut": n_cut,
            "rings": len(out_rings),
            "bridge_rings": len(bridge_rings),
            "fills": sum(len(v) for v in fills.values()),
        },
    }
