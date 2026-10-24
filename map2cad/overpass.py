"""从 OpenStreetMap 的 Overpass API 抓取矢量数据，转成 GeoJSON。

完全免费、不需要任何 key。
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .classify import classify, classify_point
from .geometry import clip_line, clip_polygon, stitch_rings
from . import netproxy
from .netproxy import build_outlets, probe_outlet

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 MapToCAD/1.1")

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Connection": "close",
}

# 多个免费镜像，逐个尝试
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.openstreetmap.fr/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

# 调试/离线测试用：MAP2CAD_MIRRORS=http://127.0.0.1:9000/api/interpreter
_env_mirrors = os.environ.get("MAP2CAD_MIRRORS")
if _env_mirrors:
    MIRRORS = [u.strip() for u in _env_mirrors.split(",") if u.strip()]

# 服务器"忙/排队"类错误：换镜像、拆小查询比死等更有用
BUSY_CODES = {429, 500, 502, 503, 504}
# 请求本身有问题的错误码：换方法、换镜像都没意义
BAD_CODES = {400, 401, 403, 404, 413, 414, 415}


class FetchError(RuntimeError):
    """带原因分类的抓取异常，方便上层决定"换镜像 / 拆查询 / 换代理"。"""

    def __init__(self, msg, kind="unknown", detail=""):
        super().__init__(msg)
        self.kind = kind      # network / busy / bad / blocked / unknown
        self.detail = detail

# 图层定义（前端勾选项 = 这里的 id）
LAYER_DEFS = {
    "building": {
        "label": "建筑物轮廓",
        "default": True,
        "filters": [
            'way["building"]',
            'relation["building"]',
            'way["building:part"]',
        ],
    },
    "highway": {
        "label": "道路（高速/主干/次干/支路/街巷）",
        "default": True,
        "filters": ['way["highway"]'],
    },
    "water": {
        "label": "水系（河流/湖泊/水库）",
        "default": True,
        "filters": [
            'way["natural"="water"]',
            'relation["natural"="water"]',
            'way["waterway"]',
            'relation["waterway"="riverbank"]',
            'way["landuse"="reservoir"]',
            'relation["landuse"="reservoir"]',
        ],
    },
    "green": {
        "label": "绿地/公园/林地",
        "default": True,
        "filters": [
            'way["landuse"~"^(grass|forest|meadow|recreation_ground|orchard|vineyard|village_green)$"]',
            'way["leisure"~"^(park|garden|pitch|golf_course|nature_reserve|common)$"]',
            'relation["leisure"~"^(park|garden|nature_reserve)$"]',
            'way["natural"~"^(wood|scrub|grassland)$"]',
        ],
    },
    "railway": {
        "label": "铁路/地铁/轻轨",
        "default": True,
        "filters": ['way["railway"~"^(rail|light_rail|subway|tram|narrow_gauge|monorail)$"]'],
    },
    "landuse": {
        "label": "其他用地（居住/商业/工业/农田）",
        "default": False,
        "filters": [
            'way["landuse"~"^(residential|commercial|industrial|retail|farmland|farmyard|quarry|construction|brownfield|military|railway|garages|education)$"]',
            'relation["landuse"~"^(residential|commercial|industrial|farmland)$"]',
        ],
    },
    "parking": {
        "label": "停车场",
        "default": False,
        "filters": ['way["amenity"="parking"]', 'relation["amenity"="parking"]'],
    },
    "boundary": {
        "label": "行政界线",
        "default": False,
        "filters": ['relation["boundary"="administrative"]["admin_level"~"^(8|9|10)$"]'],
    },
    "place": {
        "label": "地名 / 建筑名 / 兴趣点",
        "default": True,
        "filters": [
            'node["place"~"^(city|town|village|suburb|quarter|neighbourhood)$"]',
            # 地图上看到的建筑名、店名，多数是独立的点要素，不是挂在建筑轮廓上
            'node["tourism"]["name"]',
            'node["amenity"]["name"]',
            'node["shop"]["name"]',
            'node["office"]["name"]',
            'node["historic"]["name"]',
        ],
    },
}


# 每个勾选项对应到哪些 CAD 图层。
# 官方地图接口没法按标签过滤（只能整框拿回来），所以抓完必须用这张表筛掉不要的。
CAD_LAYERS = {
    "building": {"建筑"},
    "highway": {"道路-高速", "道路-主干", "道路-次干", "道路-支路", "道路-街巷", "道路-步行"},
    "water": {"水系-面", "水系-线"},
    "green": {"绿地"},
    "railway": {"铁路"},
    "landuse": {"用地"},
    "parking": {"停车场"},
    "boundary": {"行政界线"},
    "place": {"标注"},
}


def build_query(bbox, layers, timeout=180):
    minlon, minlat, maxlon, maxlat = bbox
    parts = []
    for lid in layers:
        d = LAYER_DEFS.get(lid)
        if not d:
            continue
        for f in d["filters"]:
            parts.append(f"{f}({minlat:.7f},{minlon:.7f},{maxlat:.7f},{maxlon:.7f});")
    if not parts:
        raise ValueError("没有选择任何图层")
    body = "\n  ".join(parts)
    return f"[out:json][timeout:{timeout}];\n(\n  {body}\n);\nout geom;"


def build_query_filters(bbox, filters, timeout=180):
    """只用指定的几条过滤器拼查询（用于把重图层拆小）。"""
    minlon, minlat, maxlon, maxlat = bbox
    parts = [f"{f}({minlat:.7f},{minlon:.7f},{maxlat:.7f},{maxlon:.7f});"
             for f in filters]
    if not parts:
        raise ValueError("没有过滤器")
    body = "\n  ".join(parts)
    return f"[out:json][timeout:{timeout}];\n(\n  {body}\n);\nout geom;"


# 单次请求里最多放几条过滤器，超过就拆开（水系有 6 条，一次发很容易被 504）
FILTERS_PER_QUERY = 3


def _make_opener(proxy=None):
    """按出口地址造一个 opener。proxy 为 None 强制直连，不读系统设置。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _host(url):
    try:
        return url.split("/")[2]
    except IndexError:
        return url


# 记住上次成功的出口，分图层重试时不用重新探测
_good_outlet = {"key": None, "url": None}

# 本次运行里已经确认到不了 OpenStreetMap 的出口，探测一次就够，别每个图层都重探
_dead_outlets = {}

# 已经失败的镜像主机，本轮不再浪费时间去撞（Overpass 全线拒绝时能省好几分钟）
_dead_hosts = {}

# 这个出口的 Overpass 已经确认没戏（但官方接口可能还行）—— 别再每个图层重探一遍
_no_overpass = {}


def _ordered_outlets(proxy=None):
    if proxy:
        # 用户明确指定了代理，就只走它 —— 再退回直连只会白等
        return [("手动填写的代理", proxy)]
    outs = build_outlets(proxy)
    k = _good_outlet.get("key")
    if k:
        outs.sort(key=lambda it: 0 if (it[1] or "direct") == k else 1)
    return outs


def _attempt(opener, url, payload, get_suffix, timeout, log):
    """对一个镜像先 POST，不行再 GET。

    重试策略很关键：走代理时经常「一连接就被掐断」（0.1 秒就断），这种要立刻重试；
    而「一直等到超时」说明对方根本不回，重试只是白等 —— 直接换镜像。
    """
    err = None
    for method in ("POST", "GET"):
        t_start = time.time()
        try:
            if method == "POST":
                req = urllib.request.Request(url, data=payload, headers=HEADERS)
            else:
                req = urllib.request.Request(url + get_suffix, headers=HEADERS)
            t0 = time.time()
            with opener.open(req, timeout=min(timeout, 30)) as resp:
                raw = resp.read()
            obj = json.loads(raw.decode("utf-8", "replace"))
            if "elements" not in obj:
                raise FetchError("返回内容不是 Overpass 数据（可能被拦截页替换了）", "blocked")
            if log:
                log(f"    ✓ {len(obj['elements'])} 个要素，{len(raw) / 1024:.0f} KB，"
                    f"{time.time() - t0:.1f}s")
            _dead_hosts.pop(_host(url), None)
            return obj
        except FetchError:
            raise
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(400).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            if e.code in BUSY_CODES:
                if log:
                    log(f"    × HTTP {e.code}（服务器排队/超时，换下一个镜像）")
                err = FetchError(f"HTTP {e.code}", "busy", body)
                break
            if e.code in BAD_CODES:
                if log:
                    log(f"    × HTTP {e.code}（这个镜像不收我们的出口）")
                err = FetchError(f"HTTP {e.code}", "bad", body)
                break
            if log:
                log(f"    × HTTP {e.code}")
            err = FetchError(f"HTTP {e.code}", "busy", body)
            break
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))[:90]
            fast = time.time() - t_start < 4
            if log:
                log(f"    × {method} 连不上：{reason}")
            err = FetchError(reason, "network")
            # 快速被掐断 → 立刻再试一次；一直等到超时 → 换方法/换镜像
            if fast and method == "POST" and time.time() - t_start < 4:
                try:
                    with opener.open(urllib.request.Request(
                            url, data=payload, headers=HEADERS),
                            timeout=min(timeout, 20)) as resp:
                        raw = resp.read()
                    obj = json.loads(raw.decode("utf-8", "replace"))
                    if "elements" in obj:
                        if log:
                            log(f"    ✓ 重试成功，{len(obj['elements'])} 个要素")
                        return obj
                except Exception:  # noqa: BLE001
                    pass
        except (TimeoutError, OSError) as e:
            if log:
                log(f"    × {method} 超时/中断：{type(e).__name__}")
            err = FetchError(type(e).__name__, "network")
        except Exception as e:  # noqa: BLE001  含 JSON 解析失败
            if log:
                log(f"    × {method} 失败：{type(e).__name__} {str(e)[:70]}")
            err = FetchError("返回内容无法解析（可能是拦截页）", "blocked")
            break
    raise err or FetchError("未知错误", "unknown")


def fetch_raw(query, proxy=None, timeout=120, log=None, budget=240):
    """把一条 Overpass 查询发出去，自动挑选能用的网络出口和镜像。

    出口顺序：手动填的 > 环境变量 > Windows 系统代理 > 本机常见端口 > 直连。
    每个出口先用极轻量的 /api/status 探一下，不通的直接跳过，不干等。
    """
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    get_suffix = "?data=" + urllib.parse.quote(query, safe="")
    deadline = time.time() + budget

    outlets = _ordered_outlets(proxy)
    if log:
        log("网络出口顺序：" + " → ".join(lb for lb, _ in outlets))

    last_err = None
    tried_any = False
    busy_hit = False

    for out_label, proxy_url in outlets:
        if time.time() > deadline:
            if log:
                log("… 超时时间到了，停止继续尝试")
            break
        key = proxy_url or "direct"

        # Overpass 在这个出口上刚才已经确认没戏，直接交给官方接口
        if time.time() - _no_overpass.get(key, 0) < 300:
            raise FetchError("这个出口的 Overpass 已确认不通，直接走官方接口", "no_overpass")

        # 这个出口本轮已经确认不行了，别再花时间（分图层时会反复进来）
        dead_at = _dead_outlets.get(key)
        if dead_at and time.time() - dead_at < 90:
            if log:
                log(f"  [{out_label}] 刚才已经确认这个出口不通，跳过")
            continue

        # 上次已确认能用的出口就不再探测，省掉每次重试的等待
        ok_url = _good_outlet.get("url") if _good_outlet.get("key") == key else None
        if ok_url is None:
            ok_url = probe_outlet(proxy_url, MIRRORS, timeout=8)
            if ok_url is None:
                # 没有能用的 Overpass 镜像。先看这个出口能不能到官方接口：
                # 能到就直接交给官方接口，别在这儿一个个盲试镜像浪费时间
                if _osm_alive(proxy_url):
                    _no_overpass[key] = time.time()
                    raise FetchError(
                        "所有 Overpass 镜像都拒绝或不回（换节点也没用，是对方不收），"
                        "改用 OSM 官方接口", "no_overpass")
                _dead_outlets[key] = time.time()
                if log:
                    log(f"  [{out_label}] {proxy_url or '直连'} 到不了 OpenStreetMap，跳过")
                continue
            if log:
                log(f"  [{out_label}] 探测到能用的镜像：{_host(ok_url)}")

        tried_any = True
        opener = _make_opener(proxy_url)
        if log:
            log(f"  [{out_label}] {proxy_url or '直连'} 开始抓")

        # 探测通的镜像排第一个，其余留作后备，别一个接一个盲试
        tried_urls = ([ok_url] + [u for u in MIRRORS if u != ok_url]) if ok_url else MIRRORS

        for url in tried_urls:
            if time.time() > deadline:
                break
            if _dead_hosts.get(_host(url)) and time.time() - _dead_hosts[_host(url)] < 300:
                continue
            if log:
                log(f"  → {_host(url)}")
            remain = max(8.0, deadline - time.time())
            try:
                obj = _attempt(opener, url, payload, get_suffix,
                               min(timeout, 30, remain), log)
                _good_outlet["key"] = key
                _good_outlet["url"] = url
                if log:
                    log(f"  抓取成功：出口 {proxy_url or '直连'}，镜像 {_host(url)}")
                return obj
            except FetchError as e:
                last_err = e
                busy_hit = busy_hit or e.kind == "busy"
                if e.kind in ("network", "bad"):
                    _dead_hosts[_host(url)] = time.time()
                continue

    if not tried_any:
        raise FetchError(
            "没有任何网络出口能到达 OpenStreetMap：直连和自动检测到的代理全都不通。",
            "network")
    if busy_hit:
        raise FetchError(
            "服务器全部返回繁忙或超时（HTTP 5xx）—— 缩小范围或用分块模式能绕过。",
            "busy")
    raise FetchError(f"抓取失败：{last_err}", "network")


def _osm_alive(proxy, timeout=6, tries=2):
    """这个出口能不能到 OSM 官方接口（Overpass 全挂时全靠它）。

    这条路经常"第一次被掐断、第二次才通"，所以多试几次，别一次失败就判死刑。
    """
    from .osmapi import probe as osm_probe
    return osm_probe(proxy, timeout=timeout, tries=tries)


def _probe_mirror(opener, url, timeout=8):
    """用一条真实小查询判断某个镜像能不能用（别用 /api/status，那路径常被单独拦）。"""
    payload = urllib.parse.urlencode({"data": netproxy.PROBE_QUERY}).encode("utf-8")
    host = _host(url)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=payload, headers=HEADERS)
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
        obj = json.loads(raw.decode("utf-8", "replace"))
        els = obj.get("elements") or []
        if "elements" not in obj:
            return {"host": host, "ok": False, "ms": int((time.time() - t0) * 1000),
                    "msg": "返回的不是数据（被替换成拦截页）"}
        total = (els[0].get("tags") or {}).get("total") if els else None
        if total is not None and str(total) == "0":
            return {"host": host, "ok": False, "ms": int((time.time() - t0) * 1000),
                    "msg": "这个镜像没有该地区的数据（区域镜像）"}
        return {"host": host, "ok": True, "ms": int((time.time() - t0) * 1000),
                "msg": "可用"}
    except urllib.error.HTTPError as e:
        busy = e.code in BUSY_CODES
        return {"host": host, "ok": False, "ms": int((time.time() - t0) * 1000),
                "msg": (f"服务器繁忙 HTTP {e.code}（可重试）" if busy
                        else f"拒绝服务 HTTP {e.code}（这个站不收你的出口）")}
    except Exception as e:  # noqa: BLE001
        return {"host": host, "ok": False, "ms": int((time.time() - t0) * 1000),
                "msg": f"连不上：{type(e).__name__}"}


def test_mirrors(proxy=None, timeout=8):
    """网络诊断：找出哪个出口能到 OSM 官方接口，再看公共矢量瓦片状态。

    已经不用 Overpass 镜像了（镜像经常整站拒绝某些出口，纯属浪费时间），
    这里只测两条真正会用的通道：OSM 官方接口 + 国内可直连的矢量瓦片。
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import netproxy
    from .osmapi import probe as osm_probe

    outlets = build_outlets(proxy)

    def check(item):
        lb, u = item
        if _osm_alive(u, timeout=6, tries=3):
            return (lb, u, "官方接口")
        return (lb, u, None)

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(outlets)))) as ex:
        checks = list(ex.map(check, outlets))

    out_info = [{"label": lb, "proxy": u, "ok": r is not None, "via": r} for lb, u, r in checks]
    active = next(((lb, u) for lb, u, r in checks if r), None)

    # 国内可直连的矢量瓦片：不管有没有代理都测一下，它是最后的保底通道
    t_tile = time.time()
    tiles_ok, tiles_msg = False, "连不上"
    try:
        from .tiles import fetch_tile, deg2tile
        deg2tile(31.2304, 121.4737, 14)
        fetch_tile(14, *deg2tile(31.2304, 121.4737, 14), timeout=8, tries=1)
        tiles_ok, tiles_msg = True, "可用（国内可直连，不需要代理）"
    except Exception as e:      # noqa: BLE001
        tiles_msg = f"连不上：{type(e).__name__}"
    tile_row = {"host": "公共矢量瓦片 tiles.versatiles.org（免代理·兜底）",
                "ok": tiles_ok, "ms": int((time.time() - t_tile) * 1000),
                "msg": tiles_msg}

    if active is None:
        return {"results": [tile_row], "outlets": out_info, "active": None,
                "ok": tiles_ok,
                "hint": ("没有可用代理，但「公共矢量瓦片」通道可用，"
                         "抓取时会自动走它（数据同样来自 OpenStreetMap）"
                         if tiles_ok else
                         "直连、自动检测到的代理、以及国内矢量瓦片都不通，"
                         "请检查网络，或在「网络代理」里手动填代理地址")}

    # 官方接口单独测一行
    t0 = time.time()
    osm_ok = osm_probe(active[1], timeout=min(10, timeout + 2), tries=2)
    results = [{
        "host": "api.openstreetmap.org（官方接口·主通道）",
        "ok": osm_ok,
        "ms": int((time.time() - t0) * 1000),
        "msg": "可用（抓取直接走这条）" if osm_ok else "连不上",
    }]

    results.append(tile_row)

    return {
        "results": results,
        "outlets": out_info,
        "active": {"label": active[0], "proxy": active[1]},
        "ok": any(r["ok"] for r in results),
    }


def _center(pts):
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def osm_to_geojson(osm, bbox, clip=True, log=None):
    """把 Overpass 的 out geom 结果转成 GeoJSON FeatureCollection。

    * 所有长边会被裁剪到 bbox 内，避免图纸跑到区域外
    * 面要素先做闭合判定，relation 会先拼接成环
    """
    minlon, minlat, maxlon, maxlat = bbox
    rect = (minlon, minlat, maxlon, maxlat)
    features = []
    seen = set()
    stats = {}

    def add(geom_type, coords, props):
        if not coords:
            return
        props = dict(props)
        if geom_type == "Point":
            kind_class, style = classify_point(props)
        else:
            kind_class, style = classify(props, geom_type == "Polygon")
        props["layer"] = style["layer"]
        props["_style"] = style
        features.append({
            "type": "Feature",
            "geometry": {"type": geom_type, "coordinates": coords},
            "properties": props,
        })
        stats[style["layer"]] = stats.get(style["layer"], 0) + 1

    for el in osm.get("elements", []):
        etype = el.get("type")
        tags = el.get("tags") or {}
        key = (etype, el.get("id"))
        if key in seen:
            continue
        seen.add(key)

        if etype == "node":
            lon, lat = el.get("lon"), el.get("lat")
            if lon is None:
                continue
            # 瓦片是按整块取的，块内的点会跑到框选范围外，要裁掉
            if clip and (lon < minlon or lon > maxlon
                         or lat < minlat or lat > maxlat):
                continue
            props = dict(tags)
            props["_id"] = el.get("id")
            add("Point", [lon, lat], props)
            continue

        if etype == "way":
            geo = el.get("geometry") or []
            pts = [(p["lon"], p["lat"]) for p in geo if p.get("lon") is not None]
            if len(pts) < 2:
                continue
            closed = (len(pts) >= 4 and pts[0] == pts[-1])
            props = dict(tags)
            props["_id"] = el.get("id")
            _kind, style = classify(props, closed)

            if style["closed"] or closed:
                ring = pts if closed else pts + [pts[0]]
                ring_c = clip_polygon(ring, rect) if clip else ring
                if len(ring_c) >= 3:
                    add("Polygon", [ring_c], props)
                elif not clip:
                    add("Polygon", [ring], props)
            else:
                lines = clip_line(pts, rect) if clip else [pts]
                for ln in lines:
                    add("LineString", [(round(p[0], 8), round(p[1], 8)) for p in ln], props)
            continue

        if etype == "relation":
            members = el.get("members") or []
            outer, inner = [], []
            for m in members:
                if m.get("type") != "way" or not m.get("geometry"):
                    continue
                pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p.get("lon") is not None]
                if len(pts) < 2:
                    continue
                if m.get("role") == "inner":
                    inner.append(pts)
                else:
                    outer.append(pts)
            if not outer:
                continue
            props = dict(tags)
            props["_id"] = el.get("id")
            props["_relation"] = True
            for ring in stitch_rings(outer):
                ring_c = clip_polygon(ring, rect) if clip else ring
                if len(ring_c) >= 3:
                    add("Polygon", [ring_c], props)
            for ring in stitch_rings(inner):
                ring_c = clip_polygon(ring, rect) if clip else ring
                if len(ring_c) >= 3:
                    # 内环单独画一遍，视觉上和洞一致
                    add("Polygon", [ring_c], props)
            continue

    fc = {
        "type": "FeatureCollection",
        "name": "OSM 矢量地图",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "bbox": [minlon, minlat, maxlon, maxlat],
        "features": features,
    }
    stats = dict(sorted(stats.items(), key=lambda kv: -kv[1]))
    if log:
        log(f"共 {len(features)} 个要素：" + "、".join(f"{k} {v}" for k, v in stats.items()))
    return fc, stats


def _merge_osm(chunks):
    """把多次查询的 elements 合并，按 (type, id) 去重。"""
    out = []
    seen = set()
    for o in chunks:
        for el in o.get("elements", []):
            key = (el.get("type"), el.get("id"))
            if key in seen:
                continue
            seen.add(key)
            out.append(el)
    return {"elements": out}


def _tiles(bbox):
    """把框切成 4 块，用于服务器嫌查询太大时降级。"""
    w, s, e, n = bbox
    mlon, mlat = (w + e) / 2, (s + n) / 2
    return [
        [w, s, mlon, mlat], [mlon, s, e, mlat],
        [w, mlat, mlon, n], [mlon, mlat, e, n],
    ]


def _label(lid):
    return LAYER_DEFS.get(lid, {}).get("label", lid)


def fetch_osm(bbox, layers, proxy=None, log=None, timeout=120, budget=420):
    """抓 OSM 原始数据，遇到 5xx/超时会自动降级，不会一句"失败"就结束。

    降级顺序：
      ① 一次请求全部图层（图层少时才划算）
      ② 分图层，每个图层单独查
      ③ 图层内过滤器太多（如水系 6 条）就再拆成几组
      ④ 还不行就把区域切成 4 小块

    返回 (osm_dict, 成功图层, 失败图层)
    """
    deadline = time.time() + budget

    def left():
        return max(20, deadline - time.time())

    total_filters = sum(len(LAYER_DEFS.get(l, {}).get("filters") or []) for l in layers)
    max_layer_filters = max(
        [len(LAYER_DEFS.get(l, {}).get("filters") or []) for l in layers] or [0])

    # ① 图层少、过滤器不多时，一次请求最省事
    #   只要有单个图层自己就超限（比如水系 6 条），整块请求必然被打回，直接跳过
    if total_filters <= FILTERS_PER_QUERY * 2 and max_layer_filters <= FILTERS_PER_QUERY:
        if log:
            log(f"第 1 轮：一次请求 {len(layers)} 个图层（{total_filters} 条过滤器）")
        try:
            obj = fetch_raw(build_query(bbox, layers, timeout=180), proxy,
                            timeout=timeout, log=log, budget=left())
            return obj, list(layers), []
        except FetchError as e:
            if e.kind in ("network", "no_overpass"):
                raise
            if log:
                log(f"  → 整块没成功（{e.kind}），改成分图层逐个抓（这个更稳）")
    elif log:
        log(f"这次共 {total_filters} 条过滤器，直接分图层抓更稳（跳过整块请求）")

    # ② 分图层：每次只查一个，服务器压力小很多
    chunks, ok_layers, bad_layers = [], [], []
    for lid in layers:
        if time.time() > deadline:
            bad_layers.append(lid)
            if log:
                log(f"  … 时间用完，「{_label(lid)}」跳过")
            continue

        filters = LAYER_DEFS.get(lid, {}).get("filters") or []
        if not filters:
            bad_layers.append(lid)
            continue
        # ③ 图层内过滤器太多就先拆组
        if len(filters) > FILTERS_PER_QUERY:
            groups = [filters[i:i + FILTERS_PER_QUERY]
                      for i in range(0, len(filters), FILTERS_PER_QUERY)]
        else:
            groups = [filters]

        if log:
            extra = f"，拆成 {len(groups)} 次请求" if len(groups) > 1 else ""
            log(f"第 2 轮：「{_label(lid)}」（{len(filters)} 条过滤器{extra}）")

        got = miss = 0
        for gi, g in enumerate(groups, 1):
            if time.time() > deadline:
                miss += 1
                continue
            tag = f"{gi}/{len(groups)}" if len(groups) > 1 else str(gi)
            try:
                chunks.append(fetch_raw(
                    build_query_filters(bbox, g, 180), proxy, timeout=timeout,
                    log=log, budget=min(150, left())))
                got += 1
                continue
            except FetchError as e:
                if e.kind in ("network", "no_overpass"):
                    raise
                if log:
                    log(f"  → 第 {tag} 组失败（{e.kind}），切成 4 小块再试")

            # ④ 切四块
            tile_ok = 0
            tile_miss = 0
            for ti, tb in enumerate(_tiles(bbox), 1):
                if time.time() > deadline:
                    tile_miss += 1
                    break
                try:
                    chunks.append(fetch_raw(
                        build_query_filters(tb, g, 120), proxy, timeout=90,
                        log=log, budget=min(90, left())))
                    tile_ok += 1
                except FetchError as e:
                    if e.kind in ("network", "no_overpass"):
                        raise
                    # 失败的小块别静默丢掉：立刻重试一次，再不行才记缺失。
                    if log:
                        log(f"    第 {ti}/4 块没成（{e.kind}），重试一次")
                    try:
                        chunks.append(fetch_raw(
                            build_query_filters(tb, g, 120), proxy, timeout=90,
                            log=log, budget=min(90, left())))
                        tile_ok += 1
                    except FetchError as e2:
                        tile_miss += 1
                        if log:
                            log(f"    第 {ti}/4 块重试也没成，这块区域会缺数据")
            if tile_ok:
                got += 1
                if log:
                    log(f"  ✓ 第 {tag} 组靠 {tile_ok}/4 块补齐了"
                        + (f"（缺 {tile_miss} 块，对应区域数据不完整）" if tile_miss else ""))
            else:
                miss += 1

        if got:
            ok_layers.append(lid)
            if miss and log:
                log(f"  ⚠ 「{_label(lid)}」只抓到 {got}/{got + miss} 部分")
        else:
            bad_layers.append(lid)
            if log:
                log(f"  ✗ 「{_label(lid)}」这次完全没抓到")

    if not chunks:
        raise FetchError(
            "所有图层都抓失败了：网络出口能用，但服务器一直返回繁忙。"
            "建议稍等 1 分钟重试，或把框选范围缩到 1 公里以内。", "busy")

    return _merge_osm(chunks), ok_layers, bad_layers


def _has_live_proxy(proxy=None):
    """判断"是不是真的有一个在监听的代理"。

    注意：注册表里留着代理地址不代表它活着（加速器退出后常常只把开关改成 0，
    地址还留着）。这里只看端口在不在监听，不做联网探测，所以很快。
    """
    global _PROXY_CACHE
    if proxy:
        return not netproxy._local_port_closed(proxy)
    now = time.time()
    if _PROXY_CACHE.get("val") is not None and now - _PROXY_CACHE.get("t", 0) < 30:
        return _PROXY_CACHE["val"]
    val = False
    try:
        for _label, p in build_outlets(None):
            if p and not netproxy._local_port_closed(p):
                val = True
                break
    except Exception:                   # noqa: BLE001
        val = False
    _PROXY_CACHE.update(t=now, val=val)
    return val


_PROXY_CACHE = {"t": 0.0, "val": None}


def filter_by_layers(fc, layers):
    """按用户勾选的图层筛要素（官方接口会把整框数据全给，必须筛）。"""
    allowed = set()
    for lid in layers:
        allowed |= CAD_LAYERS.get(lid, set())
    kept = [f for f in fc["features"] if f["properties"].get("layer") in allowed]
    stats = {}
    for f in kept:
        lay = f["properties"]["layer"]
        stats[lay] = stats.get(lay, 0) + 1
    fc["features"] = kept
    return fc, dict(sorted(stats.items(), key=lambda kv: -kv[1]))


def fetch_geojson(bbox, layers, proxy=None, clip=True, log=None, timeout=240):
    """抓取入口：直接走 OSM 官方接口，国内矢量瓦片当最后兜底。

      1. OSM 官方地图接口（api.openstreetmap.org，同一份 OpenStreetMap 数据）
      2. 官方接口也不通时，回头试公共矢量瓦片（国内可直连，免代理）

    为什么不再走 Overpass：Overpass 的镜像经常整站拒绝某些出口 IP（尤其挂
    VPN 时），每次都要白等几分钟超时才落到官方接口，纯属浪费时间。官方接口
    拿的就是同一份数据，直接走它更省时省事。
    """
    # 容错：没指定图层就抓全部（避免上层漏传参数时直接崩）
    if not layers:
        layers = [k for k, v in LAYER_DEFS.items() if v.get("default", True)]

    tiles_tried = False
    tile_box = {}           # 后台预取的瓦片结果 {"r": (fc, stats)} / {"e": 异常}
    bg = {}

    def _bg_tiles():
        """后台先把瓦片抓回来。等官方接口失败时，它已经备好了。"""
        try:
            from .tiles import fetch_geojson as tiles_fetch
            fc_t, stats_t = tiles_fetch(bbox, layers, log=None,
                                        timeout=min(60, max(20, timeout)),
                                        clip=clip)
            if fc_t["features"]:
                tile_box["r"] = (fc_t, stats_t)
        except Exception as e:          # noqa: BLE001
            tile_box["e"] = e

    def _take_bg(wait=0):
        th = bg.get("th")
        if th is not None:
            th.join(timeout=wait)
        return tile_box.get("r")

    def _try_tiles(step_label="矢量瓦片", wait=0):
        """国内可直连的兜底通道。成功返回 (fc, stats)，失败返回 None。"""
        nonlocal tiles_tried
        tiles_tried = True
        hit = _take_bg(wait)
        if hit:
            if log:
                log(f"{step_label}：用后台已备好的公共矢量瓦片（免代理）")
                log(f"  共 {len(hit[0]['features'])} 个要素")
            return hit
        if log:
            log(f"{step_label}：公共矢量瓦片（国内可直连，不需要代理）")
        try:
            from .tiles import fetch_geojson as tiles_fetch
            fc_t, stats_t = tiles_fetch(bbox, layers, log=log,
                                        timeout=min(60, max(20, timeout)),
                                        clip=clip)
            if fc_t["features"]:
                return fc_t, stats_t
            if log:
                log("  → 瓦片通道返回空数据")
        except Exception as e:          # noqa: BLE001
            if log:
                log(f"  → 瓦片通道不可用（{type(e).__name__}: {str(e)[:90]}）")
        return None

    # ---- 后台先备着瓦片：官方接口万一不通，能省掉干等 ----
    bg["th"] = threading.Thread(target=_bg_tiles, daemon=True)
    bg["th"].start()

    # ---- 主通道：OSM 官方地图接口 ----
    from .osmapi import SourceError, fetch_elements

    outlets = _ordered_outlets(proxy)
    if log:
        log("第 1 步：OSM 官方接口 api.openstreetmap.org")

    try:
        elems, used, tiles, fail_tiles = fetch_elements(
            bbox, outlets, log=log, timeout=min(timeout, 90),
            budget=max(120, timeout))
    except SourceError as e:
        # 官方接口不通 → 用国内可直连的矢量瓦片兜底
        if log:
            log(f"  官方接口不通（{str(e)[:80]}），改用公共矢量瓦片（国内可直连）")
        hit = _try_tiles("第 2 步", wait=8)
        if hit:
            return hit
        raise FetchError(
            "OSM 官方接口和公共矢量瓦片两条通道都没取到数据"
            f"（最后错误：{str(e)[:120]}）。\n"
            "最常见的原因是本机网络没能连到境外服务器，而且国内瓦片站也被挡了。\n"
            "可以这样处理：① 打开代理软件并确认是「全局/系统代理」模式，"
            "再在「网络代理」里填地址（例如 http://127.0.0.1:7890）；"
            "② 换个节点重试；③ 把框选范围缩到 1 公里以内。", e.kind)

    _good_outlet["key"] = used or "direct"
    if log:
        log(f"  官方接口取回 {len(elems)} 个原始要素（{tiles} 块"
            + (f"，{fail_tiles} 块缺失" if fail_tiles else "") + "）")

    fc, stats = osm_to_geojson({"elements": elems}, bbox, clip=clip, log=log)
    fc, stats = filter_by_layers(fc, layers)
    if log:
        log(f"  按勾选的 {len(layers)} 个图层筛选后：{len(fc['features'])} 个要素")
    fc["meta"] = {"ok_layers": list(layers), "bad_layers": [],
                  "source": "osm-api", "tiles": tiles,
                  "failed_tiles": fail_tiles}
    return fc, stats


def raw_to_geojson(osm, bbox, clip=True, log=None):
    """给外部传入的 OSM JSON 用（比如用户自己从 overpass-turbo 下的）。"""
    return osm_to_geojson(osm, bbox, clip=clip, log=log)
