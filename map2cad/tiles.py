"""第三条取数通道：公共矢量瓦片（不用代理也能在国肉直连）。

为什么需要它
------------
Overpass 和 OSM 官方接口都在境外，而且会按出口 IP 拒服务。国内网络在没有
可用代理时，这两条路基本是死的。公共矢量瓦片走的是 CDN，国内能直连，
数据同样来自 OpenStreetMap（Shortbread 图层规范），可以满足「描图进 CAD」。

数据规格
--------
* zoom 固定 14（这家的最高级别），瓦片边长约 2 km，1 个坐标单位 ≈ 0.51 m，
  对 1:500 出图够用。
* 图层：buildings / streets / streets_polygons / water_polygons / water_lines /
  land / sites / place_labels / street_labels。
* 字段用 `kind` 表示类型（不是 OSM 的 highway/natural），这里翻译回 OSM 标签，
  后面就能直接复用 osm_to_geojson() 那套分类 + 裁剪 + 样式逻辑。
"""
from __future__ import annotations

import gzip
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import mvt

TILE_URL = "https://tiles.versatiles.org/tiles/osm/{z}/{x}/{y}"
# 调试/离线测试用：MAP2CAD_TILES=http://127.0.0.1:9/tiles/osm/{z}/{x}/{y}
# 让单元测试能把瓦片通道也重定向到本地（或屏蔽掉），不依赖真实网络。
if os.environ.get("MAP2CAD_TILES"):
    TILE_URL = os.environ["MAP2CAD_TILES"].strip()
DEFAULT_ZOOM = 14          # 该源支持的最高级别
MAX_TILES = 48             # 超过就自动降一级 zoom
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- 坐标系
def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return x, y


def tile_x2lon(z, x, px, extent=4096):
    return (x + px / extent) / (2 ** z) * 360.0 - 180.0


def tile_y2lat(z, y, py, extent=4096):
    n = math.pi - 2.0 * math.pi * (y + py / extent) / (2 ** z)
    return math.degrees(math.atan(math.sinh(n)))


def zoom_for(bbox, zoom=DEFAULT_ZOOM, max_tiles=MAX_TILES):
    """挑一个瓦片数不爆炸的级别。"""
    minlon, minlat, maxlon, maxlat = bbox
    z = zoom
    while z > 8:
        x0, y0 = deg2tile(maxlat, minlon, z)
        x1, y1 = deg2tile(minlat, maxlon, z)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= max_tiles:
            break
        z -= 1
    return z


# ---------------------------------------------------------------- 取瓦片
def fetch_tile(z, x, y, timeout=12, tries=2):
    """下载并解码一块瓦片；失败抛异常。"""
    url = TILE_URL.format(z=z, x=x, y=y)
    last = None
    for _ in range(max(1, tries)):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/vnd.mapbox-vector-tile,*/*",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            if not raw:
                raise ValueError("空瓦片")
            return mvt.decode(raw)
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(0.3)
    raise RuntimeError(f"瓦片 {z}/{x}/{y} 取不到：{type(last).__name__} {last}")


# ---------------------------------------------------------------- 标签翻译
STREET_KIND = {
    "motorway": "motorway", "trunk": "trunk", "primary": "primary",
    "secondary": "secondary", "tertiary": "tertiary", "minor": "residential",
    "service": "service", "track": "track", "path": "path",
    "footway": "footway", "cycleway": "cycleway", "steps": "steps",
    "pedestrian": "pedestrian", "living_street": "living_street",
    "construction": "construction", "road": "road", "busway": "unclassified",
}

WATER_POLY = {"water", "ocean", "lake", "pond", "river", "canal", "dock", "basin"}

LAND_AS_LANDUSE = {
    "forest", "grass", "meadow", "farmland", "orchard", "vineyard",
    "residential", "commercial", "industrial", "retail", "railway",
    "cemetery", "quarry", "landfill", "allotments", "recreation_ground",
    "military", "brownfield", "construction",
}
LAND_AS_NATURAL = {
    "wood", "scrub", "heath", "grassland", "wetland", "beach", "sand",
    "rock", "bare_rock", "glacier", "shingle", "cliff",
}


def _truthy(v):
    return v is True or v == "yes" or v == "true" or v == 1


def _street_tags(tags):
    kind = tags.get("kind") or ""
    rail = _truthy(tags.get("rail"))
    if rail:
        return {"railway": "rail"}
    hw = STREET_KIND.get(kind)
    if not hw:
        return {}
    if _truthy(tags.get("link")):
        hw += "_link"
    out = {"highway": hw}
    # 桥 / 隧道：DWX 里要靠这两个标记算「路跨过路」的效果
    if _truthy(tags.get("bridge")):
        out["bridge"] = "yes"
    if _truthy(tags.get("tunnel")):
        out["tunnel"] = "yes"
    return out


def _elem(tags, ring, eid, extent, z, tx, ty):
    """把瓦片内的一条几何转成 Overpass 风格的元素（含经纬度）。"""
    pts = []
    for px, py in ring:
        lon = tile_x2lon(z, tx, px, extent)
        lat = tile_y2lat(z, ty, py, extent)
        pts.append({"lon": round(lon, 8), "lat": round(lat, 8)})
    if len(pts) < 2:
        return None
    return {"type": "way", "id": eid, "tags": tags, "geometry": pts}


# POI 上只保留这几类标签：名字 + 类别。housenumber 之类进 CAD 没用，去掉更干净。
POI_KEEP = ("name", "name_en", "tourism", "amenity", "shop", "office",
            "historic", "leisure", "healthcare", "craft")


def _poi_tags(tags):
    return {k: v for k, v in tags.items() if k in POI_KEEP and v}


def tile_to_elements(z, tx, ty, layers):
    """一块瓦片 -> (要素列表, 路名, 面注记)。

    路名 / 面注记都是 OSM 渲染器自己的标注几何，用它当文字的落点，
    位置就和 OSM 地图上看到的完全一致。
    """
    extent = 4096
    out = []
    labels = []
    anchors = []
    eid = (z * 1000000000) + (tx * 100000) + ty * 10

    # 建筑
    if "building" in layers and "buildings" in mvt_layers:
        for f in mvt_layers["buildings"]["features"]:
            for ring in f["geometry"]:
                eid += 1
                el = _elem({"building": "yes"}, ring, eid, extent, z, tx, ty)
                if el:
                    out.append(el)

    # 道路 / 铁路
    if "highway" in layers or "railway" in layers:
        for f in mvt_layers.get("streets", {}).get("features", []):
            t = _street_tags(f["tags"])
            if not t:
                continue
            if "railway" in t and "railway" not in layers:
                continue
            if "highway" in t and "highway" not in layers:
                continue
            for ring in f["geometry"]:
                eid += 1
                el = _elem(t, ring, eid, extent, z, tx, ty)
                if el:
                    out.append(el)

    # 路面（广场、步行街这类面）
    if "highway" in layers:
        for f in mvt_layers.get("streets_polygons", {}).get("features", []):
            t = _street_tags(f["tags"])
            if not t:
                continue
            for ring in f["geometry"]:
                eid += 1
                el = _elem(t, ring, eid, extent, z, tx, ty)
                if el:
                    out.append(el)

    # 水系（面）
    if "water" in layers:
        for f in mvt_layers.get("water_polygons", {}).get("features", []):
            kind = f["tags"].get("kind") or "water"
            tags = ({"landuse": "reservoir"} if kind == "reservoir"
                    else {"natural": "water"})
            for ring in f["geometry"]:
                eid += 1
                el = _elem(tags, ring, eid, extent, z, tx, ty)
                if el:
                    out.append(el)
        for f in mvt_layers.get("water_lines", {}).get("features", []):
            kind = f["tags"].get("kind") or "stream"
            for ring in f["geometry"]:
                eid += 1
                el = _elem({"waterway": kind}, ring, eid, extent, z, tx, ty)
                if el:
                    out.append(el)

    # 绿地 / 用地
    if "green" in layers or "landuse" in layers:
        for lname in ("land", "sites"):
            for f in mvt_layers.get(lname, {}).get("features", []):
                kind = f["tags"].get("kind") or ""
                if not kind:
                    continue
                if kind == "parking":
                    tags = {"amenity": "parking"}
                elif kind in LAND_AS_NATURAL:
                    tags = {"natural": kind}
                elif kind in LAND_AS_LANDUSE:
                    tags = {"landuse": kind}
                elif lname == "sites" and kind in {"school", "university", "college",
                                                  "hospital", "industrial", "retail"}:
                    tags = {"landuse": "education" if kind in
                            {"school", "university", "college"} else kind}
                else:
                    continue
                for ring in f["geometry"]:
                    eid += 1
                    el = _elem(tags, ring, eid, extent, z, tx, ty)
                    if el:
                        out.append(el)

    # 地名点
    if "place" in layers:
        for f in mvt_layers.get("place_labels", {}).get("features", []):
            name = f["tags"].get("name") or f["tags"].get("name_en")
            if not name:
                continue
            for ring in f["geometry"]:
                px, py = ring[0]
                out.append({
                    "type": "node", "id": eid, "tags": {"place": "locality",
                                                        "name": name},
                    "lon": round(tile_x2lon(z, tx, px, extent), 8),
                    "lat": round(tile_y2lat(z, ty, py, extent), 8),
                })
                eid += 1

    # 兴趣点：地图上看到的大多数建筑/店铺/景点名字都在这一层（buildings 图层本身不带名字）
    if "place" in layers or "building" in layers:
        for f in mvt_layers.get("pois", {}).get("features", []):
            t = f["tags"]
            name = t.get("name") or t.get("name_en")
            if not name:
                continue
            for ring in f["geometry"]:
                px, py = ring[0]
                out.append({
                    "type": "node", "id": eid, "tags": _poi_tags(t),
                    "lon": round(tile_x2lon(z, tx, px, extent), 8),
                    "lat": round(tile_y2lat(z, ty, py, extent), 8),
                })
                eid += 1

    # 路名（单独图层，需要回头贴到最近的那条路上）
    if "highway" in layers:
        for lname in ("street_labels", "street_labels_points"):
            for f in mvt_layers.get(lname, {}).get("features", []):
                name = f["tags"].get("name") or f["tags"].get("name_en")
                if not name:
                    continue
                for ring in f["geometry"]:
                    pts = [(tile_x2lon(z, tx, px, extent),
                            tile_y2lat(z, ty, py, extent)) for px, py in ring]
                    labels.append((pts, name))

    # 水系 / 绿地 / 广场这些面，OSM 也给了专门的标注几何（位置是它自己算好的）
    for lname in ("water_polygons_labels", "water_lines_labels",
                  "streets_polygons_labels", "land_labels", "sites_labels"):
        for f in mvt_layers.get(lname, {}).get("features", []):
            name = f["tags"].get("name") or f["tags"].get("name_en")
            if not name:
                continue
            for ring in f["geometry"]:
                pts = [(tile_x2lon(z, tx, px, extent),
                        tile_y2lat(z, ty, py, extent)) for px, py in ring]
                if pts:
                    anchors.append((pts, name))

    return out, labels, anchors


# 供 tile_to_elements 用的模块级变量（每块瓦片前赋值，避免来回传参）
mvt_layers = {}


def _dist_pt_seg(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _mid_anchor(pts):
    """取一条标注线的中点与走向（度）。OSM 就是用这条线决定路名摆在哪、斜多少。"""
    if not pts:
        return None, 0.0
    if len(pts) == 1:
        return pts[0], 0.0
    # 找最长的一段，取其中点
    best, bi = -1.0, 0
    for i in range(len(pts) - 1):
        d = math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        if d > best:
            best, bi = d, i
    a, b = pts[bi], pts[bi + 1]
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    if ang > 90:
        ang -= 180
    elif ang < -90:
        ang += 180
    return mid, ang


def _attach_names(elements, labels):
    """把 street_labels 里的路名贴到最近的同名道路上（标签图层里路名是分开存的）。

    顺便把 OSM 的标注线中点记到 `_label_pos`，画图时按这个点落字，
    位置就和 OSM 地图一致了；没有标注线的才退回「最长段中点」。
    """
    roads = [e for e in elements
             if (e.get("tags") or {}).get("highway")]
    if not roads or not labels:
        return 0
    hit = 0
    for pts, name in labels:
        if not pts:
            continue
        mid, ang = _mid_anchor(pts)
        best, best_d = None, 60.0 / 111000.0 * 2.2      # 约 60 m
        for road in roads:
            if road["tags"].get("name"):
                continue
            geo = road["geometry"]
            for i in range(len(geo) - 1):
                a = (geo[i]["lon"], geo[i]["lat"])
                b = (geo[i + 1]["lon"], geo[i + 1]["lat"])
                d = _dist_pt_seg(mid, a, b)
                if d < best_d:
                    best, best_d = road, d
        if best is not None:
            best["tags"]["name"] = name
            best["tags"]["_label_pos"] = [round(mid[0], 8), round(mid[1], 8)]
            best["tags"]["_label_angle"] = round(ang, 2)
            hit += 1
    return hit


def _point_in_ring(pt, ring):
    """射线法判断点是否在多边形内。"""
    x, y = pt
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-18) + x1
            if x < xin:
                inside = not inside
    return inside


def _attach_area_labels(elements, anchors):
    """给水系 / 绿地 / 广场这些面找到 OSM 自己的标注位置。"""
    if not anchors:
        return 0
    polys = [e for e in elements
             if e.get("tags") and len(e.get("geometry") or []) >= 3
             and not e["tags"].get("building")]        # 建筑名字走 POI，不在这里抢
    hit = 0
    for pts, name in anchors:
        mid, ang = _mid_anchor(pts)
        best, best_area = None, 1e18
        for el in polys:
            if el["tags"].get("name"):
                continue
            ring = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if not _point_in_ring(mid, ring):
                continue
            xs = [c[0] for c in ring]
            ys = [c[1] for c in ring]
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            if area < best_area:          # 取最小的那个面，避免贴到大区块上
                best, best_area = el, area
        if best is not None:
            best["tags"]["name"] = name
            best["tags"]["_label_pos"] = [round(mid[0], 8), round(mid[1], 8)]
            best["tags"]["_label_angle"] = round(ang, 2)
            hit += 1
    return hit


# ---------------------------------------------------------------- 对外入口
def _names_from_pois(features):
    """把 POI 点的名字挂到它所在的建筑上。

    只挂属性、不额外落字：文字还是画在 POI 那个点上（OSM 也是这么摆的），
    这样既能让建筑要素带上 `name`，又不会出现「一栋楼两个名字」。
    """
    blds = []
    for f in features:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if p.get("layer") != "建筑" or g.get("type") != "Polygon":
            continue
        ring = g["coordinates"][0]
        xs = [c[0] for c in ring]
        ys = [c[1] for c in ring]
        blds.append((min(xs), min(ys), max(xs), max(ys), ring, p))
    if not blds:
        return 0

    hit = 0
    for f in features:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if p.get("layer") != "标注" or g.get("type") != "Point" or not p.get("name"):
            continue
        lon, lat = g["coordinates"]
        for x0, y0, x1, y1, ring, bp in blds:
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            if not _point_in_ring((lon, lat), ring):
                continue
            if not bp.get("name"):
                bp["name"] = p["name"]
                bp["_no_label"] = True      # 建筑不再单独落字，POI 点上已经有
                hit += 1
            break
    return hit


def fetch_geojson(bbox, layers, log=None, timeout=90, clip=True):
    """抓一块区域的矢量瓦片并转成 GeoJSON。失败抛 RuntimeError。"""
    from .overpass import filter_by_layers, osm_to_geojson

    global mvt_layers
    minlon, minlat, maxlon, maxlat = bbox
    z = zoom_for(bbox)
    x0, y0 = deg2tile(maxlat, minlon, z)
    x1, y1 = deg2tile(minlat, maxlon, z)
    tiles = [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    if log:
        log(f"矢量瓦片通道：z{z}，{len(tiles)} 块瓦片（国内可直连，不需要代理）")

    t0 = time.time()
    elements, all_labels, all_anchors = [], [], []
    errors = []

    def work(t):
        zz, xx, yy = t
        try:
            return t, fetch_tile(zz, xx, yy, timeout=max(6, min(15, timeout // 4)))
        except Exception as e:      # noqa: BLE001
            return t, e

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(work, tiles))

    for t, res in results:
        if isinstance(res, Exception):
            errors.append(str(res))
            continue
        mvt_layers = res
        els, labels, anchors = tile_to_elements(t[0], t[1], t[2], layers)
        elements.extend(els)
        all_labels.extend(labels)
        all_anchors.extend(anchors)

    if not elements:
        raise RuntimeError("矢量瓦片没取到数据" + (f"（{errors[0][:80]}）" if errors else ""))

    named = _attach_names(elements, all_labels)
    named += _attach_area_labels(elements, all_anchors)
    pois = sum(1 for e in elements
               if e.get("type") == "node" and (e.get("tags") or {}).get("name"))
    if log:
        log(f"  取回 {len(elements)} 个要素（{time.time()-t0:.1f}s），"
            f"其中 {named} 个面/线补上了名字，{pois} 个兴趣点带名字")

    fc, stats = osm_to_geojson({"elements": elements}, bbox, clip=clip, log=None)
    fc, stats = filter_by_layers(fc, layers)
    got = _names_from_pois(fc["features"])
    if log and got:
        log(f"  有 {got} 栋建筑从兴趣点拿到了名字")
    fc["meta"] = {
        "ok_layers": list(layers), "bad_layers": [],
        "source": "tiles", "tiles": len(tiles), "zoom": z,
        "failed_tiles": len(errors),
        "named_buildings": got,
    }
    if log:
        log(f"  按勾选图层筛完：{len(fc['features'])} 个要素")
    return fc, stats
