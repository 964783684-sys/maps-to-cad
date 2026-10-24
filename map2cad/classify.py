"""OSM 标签 -> CAD 图层 / 颜色 / 线型 / 线宽 的映射规则。

返回的 style 是一个 dict：
    layer     图层名（中文，CAD 里直接可读）
    color     AutoCAD 颜色号（ACI）
    ltype     线型名（CAD 标准线型，不存在则回落到 CONTINUOUS）
    const     LWPOLYLINE 全局宽度（米）——让道路看起来有路面宽度
    closed    是否闭合
    label     需要在图上标注的文字
"""

# ===== 图层定义：图层名 -> (颜色, 线型, 线宽mm) =====
LAYERS = {
    "建筑":        (8,   "CONTINUOUS", 13),
    "道路-高速":   (1,   "CONTINUOUS", 50),
    "道路-主干":   (1,   "CONTINUOUS", 40),
    "道路-次干":   (2,   "CONTINUOUS", 30),
    "道路-支路":   (3,   "CONTINUOUS", 25),
    "道路-街巷":   (9,   "CONTINUOUS", 18),
    "道路-步行":   (9,   "DASH_M",    13),
    "道路轮廓":    (8,   "CONTINUOUS", 13),
    "铁路":        (30,  "CENTER_M",  25),
    "水系-面":     (5,   "CONTINUOUS", 20),
    "水系-线":     (5,   "CONTINUOUS", 18),
    "绿地":        (94,  "CONTINUOUS", 15),
    "用地":        (253, "CONTINUOUS", 13),
    "停车场":      (251, "DASH_M",    13),
    "行政界线":    (6,   "DASH_M",    25),
    "卫星图":      (8,   "CONTINUOUS", 9),
    "标注":        (7,   "CONTINUOUS", 0),
    "其他":        (8,   "CONTINUOUS", 13),
}

# 各类道路的默认路面宽度（米）；真实数据里有 width / lanes 标签会优先用
ROAD_WIDTH = {
    "motorway": 26, "motorway_link": 12,
    "trunk": 22, "trunk_link": 10,
    "primary": 18, "primary_link": 9,
    "secondary": 14, "secondary_link": 8,
    "tertiary": 10, "tertiary_link": 7,
    "residential": 7, "unclassified": 7, "living_street": 6,
    "service": 5, "track": 3.5,
    "pedestrian": 4, "footway": 2, "path": 2, "steps": 2,
    "cycleway": 2.5, "bridleway": 2,
    "construction": 7, "road": 7,
}

ROAD_LAYER = {
    "motorway": "道路-高速", "motorway_link": "道路-高速",
    "trunk": "道路-主干", "trunk_link": "道路-主干",
    "primary": "道路-主干", "primary_link": "道路-主干",
    "secondary": "道路-次干", "secondary_link": "道路-次干",
    "tertiary": "道路-支路", "tertiary_link": "道路-支路",
    "residential": "道路-街巷", "unclassified": "道路-街巷",
    "living_street": "道路-街巷", "service": "道路-街巷",
    "road": "道路-街巷", "track": "道路-街巷",
    "pedestrian": "道路-步行", "footway": "道路-步行",
    "path": "道路-步行", "steps": "道路-步行",
    "cycleway": "道路-步行", "bridleway": "道路-步行",
}

# ===== 实体填充（照 OSM 标准样式 openstreetmap-carto 的路面色）=====
# 等级：数字越大越「高级」，填充时后画 —— 路口处高等级压住低等级，
# 这就是 OSM 上路网看起来有层次的原因。
ROAD_RANK = {
    "道路-步行": 0,
    "道路-街巷": 1,
    "道路-支路": 2,
    "道路-次干": 3,
    "道路-主干": 4,
    "道路-高速": 5,
}

# 只给「机动车道」填色。步行道在 OSM 上本来就是虚线、不填色，
# 填了反而会把虚线盖住，所以这里故意不给它配色。
ROAD_FILL = {
    "道路-高速": (0xE8, 0x92, 0xA2),   # motorway 粉
    "道路-主干": (0xFC, 0xD6, 0xA4),   # trunk / primary 浅橙
    "道路-次干": (0xF7, 0xFA, 0xBF),   # secondary 浅黄
    "道路-支路": (0xFF, 0xFF, 0xFF),   # tertiary 白
    "道路-街巷": (0xFF, 0xFF, 0xFF),   # residential / service 白
}

GREEN_VALUES = {
    "grass", "forest", "meadow", "recreation_ground", "orchard",
    "vineyard", "cemetery", "village_green", "allotments",
}

WATER_VALUES = {"water", "reservoir", "basin", "river", "canal", "lake", "pond"}

LANDUSE_LAYER = {
    "residential": "用地", "commercial": "用地", "industrial": "用地",
    "retail": "用地", "farmland": "用地", "farmyard": "用地",
    "quarry": "用地", "construction": "用地", "brownfield": "用地",
    "military": "用地", "railway": "用地", "garages": "用地",
    "education": "用地", "religious": "用地",
}


def _num(value, default=0.0):
    """从 '12'、'12 m'、'12.5' 这类字符串里取出数字。"""
    if value is None:
        return default
    try:
        s = str(value).strip().lower().replace("m", "").replace("米", "").strip()
        return float(s)
    except (TypeError, ValueError):
        return default


def _style(layer, const=0.0, closed=False, label=None):
    color, ltype, lw = LAYERS.get(layer, LAYERS["其他"])
    return {
        "layer": layer,
        "color": color,
        "ltype": ltype,
        "lineweight": lw,
        "const": const,
        "closed": closed,
        "label": label,
    }


def classify_point(tags: dict):
    """点要素单独判：有点要素一律是「标注」。

    为什么不能塞进 classify：那里只有 is_closed 一个布尔量，分不清
    「点」和「没闭合的线」，会把带名字的线也误判成标注。
    """
    t = tags or {}
    if t.get("name"):
        return "point", _style("标注", 0.0, False, t["name"])
    if t.get("place"):
        return "point", _style("标注", 0.0, False, None)
    return "point", _style("其他", 0.0, False, None)


def classify(tags: dict, is_closed: bool = False):
    """根据 OSM tags 判断要素类别与样式。返回 (kind, style)。

    kind: 'poly' 面 | 'line' 线 | 'point' 点
    """
    t = tags or {}

    # ---------- 建筑物 ----------
    if "building" in t or "building:part" in t:
        return "poly", _style("建筑", 0.0, True, t.get("name"))

    # ---------- 道路 ----------
    hw = t.get("highway")
    if hw:
        layer = ROAD_LAYER.get(hw)
        if layer:
            width = _num(t.get("width"), 0.0)
            if width <= 0:
                lanes = _num(t.get("lanes"), 0.0)
                width = ROAD_WIDTH.get(hw, 7.0)
                if lanes >= 1:
                    width = max(width, lanes * 3.5)
            return "line", _style(layer, width, False, t.get("name") or t.get("ref"))

    # ---------- 铁路 ----------
    if t.get("railway") in {"rail", "light_rail", "subway", "tram", "narrow_gauge",
                             "monorail", "funicular", "disused"}:
        return "line", _style("铁路", 0.0, False, t.get("name"))

    # ---------- 水系 ----------
    waterway = t.get("waterway")
    if t.get("natural") in WATER_VALUES or t.get("landuse") in {"reservoir", "basin"}:
        return "poly", _style("水系-面", 0.0, True, t.get("name"))
    if waterway in {"riverbank", "dock"}:
        return "poly", _style("水系-面", 0.0, True, t.get("name"))
    if waterway:
        w = {"river": 30, "canal": 15, "stream": 5, "ditch": 3, "drain": 3}.get(waterway, 4)
        w = _num(t.get("width"), w)
        return "line", _style("水系-线", w, False, t.get("name"))
    if t.get("natural") == "coastline":
        return "line", _style("水系-线", 0.0, False, None)

    # ---------- 绿地 ----------
    lu = t.get("landuse")
    leisure = t.get("leisure")
    natural = t.get("natural")
    if lu in GREEN_VALUES or leisure in {"park", "garden", "pitch", "golf_course",
                                         "recreation_ground", "nature_reserve", "common"} \
            or natural in {"wood", "scrub", "grassland", "heath"}:
        return "poly", _style("绿地", 0.0, True, t.get("name"))

    # ---------- 停车场 ----------
    if t.get("amenity") == "parking":
        return "poly", _style("停车场", 0.0, True, t.get("name"))

    # ---------- 其他用地 ----------
    if lu in LANDUSE_LAYER:
        return "poly", _style("用地", 0.0, True, t.get("name"))
    if lu:
        return "poly", _style("用地", 0.0, True, t.get("name"))

    # ---------- 行政界线 ----------
    if t.get("boundary") == "administrative":
        return "line", _style("行政界线", 0.0, False, t.get("name"))

    # ---------- 地名点 ----------
    if t.get("place"):
        return "point", _style("标注", 0.0, False, t.get("name"))

    # ---------- 兜底 ----------
    if is_closed:
        return "poly", _style("其他", 0.0, True, t.get("name"))
    return "line", _style("其他", 0.0, False, t.get("name"))
