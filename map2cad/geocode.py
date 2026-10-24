"""地名搜索：把「人民广场」变成经纬度。

为什么放在服务端做：
  浏览器直接请求 nominatim.openstreetmap.org 在不少网络下是不通的
  （国内直连失败，挂代理又能通），而服务端已经会自己挑代理，
  所以交给服务端去问，成功率高得多。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .netproxy import build_outlets

API = "https://nominatim.openstreetmap.org/search"

UA = "MapToCAD/1.2 (free OSM vector to DXF converter; low-volume one-off use)"

_cache = {}          # 关键词 -> (时间, 结果)
CACHE_TTL = 600


def _opener(proxy=None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _query(q, proxy, timeout, limit):
    url = (API + "?format=json&limit=%d&accept-language=zh-CN,zh&q=" % limit
           + urllib.parse.quote(q, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with _opener(proxy).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def search(q, proxy=None, limit=6, timeout=12, log=None):
    """返回 [{name, lat, lon, bbox}]，全不通时抛 RuntimeError。"""
    q = (q or "").strip()
    if not q:
        return []

    hit = _cache.get(q)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    last = None
    for label, p in build_outlets(proxy):
        for attempt in range(2):
            try:
                data = _query(q, p, timeout, limit)
                out = []
                for it in data or []:
                    try:
                        bb = [float(x) for x in (it.get("boundingbox") or [])]
                    except (TypeError, ValueError):
                        bb = []
                    out.append({
                        "name": it.get("display_name") or q,
                        "lat": float(it.get("lat")),
                        "lon": float(it.get("lon")),
                        # Nominatim 的 boundingbox 顺序是 [南, 北, 西, 东]
                        "bbox": [bb[2], bb[0], bb[3], bb[1]] if len(bb) == 4 else None,
                    })
                if log:
                    log(f"搜索走出口 {p or '直连'}，命中 {len(out)} 条")
                _cache[q] = (time.time(), out)
                return out
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                break
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}"
                time.sleep(0.8)
        if log:
            log(f"出口 {p or '直连'} 搜索失败（{last}），换下一个")

    raise RuntimeError(f"搜索失败（{last}）。检查网络或代理后重试。")
