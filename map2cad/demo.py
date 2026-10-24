"""内置演示数据：一块虚构街区。

用途：
  1. 零网络时也能体验「生成 GeoJSON -> 生成 DXF」全流程
  2. 自检脚本用它验证转换环节
"""
import math

from .classify import classify

DX = 0.0000107   # 约 1 米经度
DY = 0.00000899  # 约 1 米纬度


def _mk(lon0, lat0, tags, gtype, coords):
    _k, style = classify(tags, gtype == "Polygon")
    props = dict(tags)
    props["layer"] = style["layer"]
    props["_style"] = style
    return {"type": "Feature", "properties": props,
            "geometry": {"type": gtype, "coordinates": coords}}


def build_demo_geojson(center_lon=121.4737, center_lat=31.2304, span_m=400):
    """返回一个 FeatureCollection，覆盖约 800m x 800m。"""
    lon0, lat0 = center_lon, center_lat

    def ll(dx_m, dy_m):
        return [lon0 + dx_m * DX, lat0 + dy_m * DY]

    s = span_m
    feat = []

    # 四级道路：南北 + 东西
    specs = [(-0.75 * s, "primary", "纵1路"), (-0.25 * s, "secondary", "纵2路"),
             (0.25 * s, "tertiary", "纵3路"), (0.75 * s, "residential", "纵4路")]
    for off, hw, name in specs:
        feat.append(_mk(lon0, lat0, {"highway": hw, "name": name}, "LineString",
                        [ll(off, -s), ll(off, s)]))
    for i, (off, hw, name) in enumerate(specs):
        feat.append(_mk(lon0, lat0, {"highway": hw, "name": f"横{i+1}路"}, "LineString",
                        [ll(-s, off), ll(s, off)]))

    # 建筑：3x3 街区，每街区 4 栋
    for bx in (-0.5 * s, 0, 0.5 * s):
        for by in (-0.5 * s, 0, 0.5 * s):
            for k in range(4):
                x0 = bx - 40 + k * 18
                y0 = by - 40
                w, h = 14, 22
                ring = [ll(x0, y0), ll(x0 + w, y0), ll(x0 + w, y0 + h),
                        ll(x0, y0 + h), ll(x0, y0)]
                feat.append(_mk(lon0, lat0, {"building": "yes"}, "Polygon", [ring]))

    # 湖
    cx, cy, r = 0.55 * s, 0.55 * s, 60
    lake = [[cx + r * math.cos(a * math.pi / 12), cy + r * math.sin(a * math.pi / 12)]
            for a in range(24)]
    lake.append(lake[0])
    feat.append(_mk(lon0, lat0, {"natural": "water", "name": "示范湖"}, "Polygon",
                    [[ll(x, y) for x, y in lake]]))

    # 河
    feat.append(_mk(lon0, lat0, {"waterway": "river", "name": "示范河"}, "LineString",
                    [ll(-s, -0.375 * s), ll(-0.5 * s, -0.35 * s), ll(0, -0.4 * s),
                     ll(0.5 * s, -0.375 * s), ll(s, -0.3625 * s)]))

    # 绿地
    feat.append(_mk(lon0, lat0, {"leisure": "park", "name": "示范公园"}, "Polygon",
                    [[ll(-0.95 * s, -0.95 * s), ll(-0.45 * s, -0.95 * s),
                      ll(-0.45 * s, -0.625 * s), ll(-0.95 * s, -0.625 * s),
                      ll(-0.95 * s, -0.95 * s)]]))

    # 铁路
    feat.append(_mk(lon0, lat0, {"railway": "rail", "name": "示范线"}, "LineString",
                    [ll(-s, 0.95 * s), ll(-0.25 * s, 0.9 * s),
                     ll(0.5 * s, 0.925 * s), ll(s, 0.9125 * s)]))

    # 地名
    feat.append(_mk(lon0, lat0, {"place": "suburb", "name": "演示片区"}, "Point", ll(0, 0)))

    return {
        "type": "FeatureCollection",
        "name": "内置演示数据",
        "features": feat,
        "bbox": [lon0 - s * DX, lat0 - s * DY, lon0 + s * DX, lat0 + s * DY],
    }
