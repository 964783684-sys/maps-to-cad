"""经纬度 -> 局部平面直角坐标（单位：米）。

在几公里到十几公里范围内，用"每度经/纬线长度"做局部等距投影，
精度优于 0.1%，足以做 CAD 底图；原点默认取区域中心，x 向东、y 向北。
"""
import math


def meters_per_degree(lat_deg: float):
    """返回该纬度处 1 度经度、1 度纬度对应的米数（WGS84 椭球）。"""
    phi = math.radians(lat_deg)
    m_lat = (111132.954
             - 559.822 * math.cos(2 * phi)
             + 1.175 * math.cos(4 * phi)
             - 0.0023 * math.cos(6 * phi))
    m_lon = (111412.877 * math.cos(phi)
             - 93.5 * math.cos(3 * phi)
             + 0.118 * math.cos(5 * phi))
    return abs(m_lon), abs(m_lat)


class LocalProjector:
    """把 (lon, lat) 投影到以 (lon0, lat0) 为原点的米制平面上。"""

    def __init__(self, lon0: float, lat0: float):
        self.lon0 = lon0
        self.lat0 = lat0
        self.kx, self.ky = meters_per_degree(lat0)

    def __call__(self, lon: float, lat: float):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def unproject(self, x: float, y: float):
        return (x / self.kx + self.lon0, y / self.ky + self.lat0)

    def bbox_size(self, bbox):
        """bbox = (minlon, minlat, maxlon, maxlat) -> (宽, 高) 米。"""
        minlon, minlat, maxlon, maxlat = bbox
        return ((maxlon - minlon) * self.kx, (maxlat - minlat) * self.ky)
