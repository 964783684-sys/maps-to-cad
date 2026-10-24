"""卫星影像底图：给 OSM 没画的小区补描建筑用。

为什么需要
----------
OSM 的建筑是志愿者一根线一根线描出来的，老旧小区、单位大院常常整片空白。
光看矢量图没法知道那里有几栋楼、楼是几层进深，所以要把卫片垫在下面照着描。

怎么保证「一一对应」
--------------------
卫星影像是 Web 墨卡托瓦片，瓦片的编号和经纬度是一一对应的，这里按
bbox 算出需要的瓦片范围，拼成一张大图，再**裁到正好是 bbox 那么大**。
DXF 里插入时给的是 `(宽米, 高米)`，所以每个像素对应多少米是算出来的：

    1 像素 = 156543.034 * cos(纬度) / 2^zoom   米

不是估的、也不是图省事直接铺满 —— 影像的四个角和图纸坐标严丝合缝，
描出来的楼可以直接当图用。

用哪家的影像
------------
Esri World Imagery（`server.arcgisonline.com`）。选它的唯一理由是
**它是 WGS-84 真坐标**；国内地图（高德/百度）的卫星图是 GCJ-02 火星坐标，
直接铺过来会整体偏 300~700 米，那就没法用了。
"""
from __future__ import annotations

import math
import os
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE = 256
Z_MAX = 19                 # Esri 大部分城区到 19 级（约 0.25 m/像素）
Z_MIN = 15
MAX_TILES = 900            # 瓦片数上限，超了自动降一级
MAX_PX = 25_000_000        # 拼出来的图最多 2500 万像素（约 4~6 MB JPEG）
BLUR_MPP = 1.0             # 比这还糊（1 米/像素）就提醒用户缩小范围
HARD_MAX_TILES = 1000      # 降到底还超，就直接报错（再拼会吃爆内存）
HARD_MAX_PX = 30_000_000
WORKERS = 6

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# 直连优先（Esri 在国内一般能直连）；直连失败再退回系统/环境变量里的代理。
# 反过来就会踩坑：有些代理软件把境外 CDN 拦了，直连反而是通的。
_direct = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_ctx),
)

HAS_PIL = True
try:
    from PIL import Image
except Exception:               # noqa: BLE001
    HAS_PIL = False


# ------------------------------------------------------------------ 墨卡托
def _tile_xy(lon, lat, z):
    """经纬度 -> 瓦片浮点坐标（整数部分就是瓦片号，小数部分是瓦片内位置）。"""
    n = float(2 ** z)
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


def mpp(lat, z):
    """该纬度、该级别下 1 像素代表多少米（地面距离）。"""
    return 156543.03392804097 * math.cos(math.radians(lat)) / (2 ** z)


def pick_zoom(bbox, max_tiles=MAX_TILES, max_px=MAX_PX):
    """挑级别：从最清楚的 19 级往下找，第一个「瓦片数不爆」的就是最好的。

    不能反过来从低往高找 —— 那样会把 best 一路覆盖成最糊的那一级。
    """
    minlon, minlat, maxlon, maxlat = bbox
    for z in range(Z_MAX, Z_MIN - 1, -1):
        x0, y0 = _tile_xy(minlon, maxlat, z)
        x1, y1 = _tile_xy(maxlon, minlat, z)
        nx = int(math.floor(x1)) - int(math.floor(x0)) + 1
        ny = int(math.floor(y1)) - int(math.floor(y0)) + 1
        if nx * ny <= max_tiles and nx * TILE * ny * TILE <= max_px:
            return z
    return Z_MIN


# ------------------------------------------------------------------ 取瓦片
def _fetch(z, x, y, timeout=8, tries=2):
    url = TILE_URL.format(z=z, x=x, y=y)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
            })
            if i == 0:                      # 第一次：不走代理
                with _direct.open(req, timeout=timeout) as r:
                    raw = r.read()
            else:                           # 第二次：走系统/环境变量代理
                with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                    raw = r.read()
            if len(raw) < 200:
                raise ValueError(f"瓦片只有 {len(raw)} 字节")
            return raw
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(0.15)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def _blank():
    return Image.new("RGB", (TILE, TILE), (58, 58, 58))


def build(bbox, out_path, zoom=None, log=None, quality=82):
    """抓取 bbox 范围内的卫星影像，拼成一张 JPEG，返回定位信息。

    返回 dict：file / px / mpp / zoom / tiles / failed
    取不到就抛 RuntimeError（调用方负责降级，不让整个导出失败）。
    """
    if not HAS_PIL:
        raise RuntimeError("没有图像库 Pillow，装不了卫星图")

    minlon, minlat, maxlon, maxlat = bbox
    z = zoom or pick_zoom(bbox)
    x0f, y0f = _tile_xy(minlon, maxlat, z)      # 左上（西北）
    x1f, y1f = _tile_xy(maxlon, minlat, z)      # 右下（东南）
    tx0, ty0 = int(math.floor(x0f)), int(math.floor(y0f))
    tx1, ty1 = int(math.floor(x1f)), int(math.floor(y1f))
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    tiles = [(tx0 + i, ty0 + j) for j in range(ny) for i in range(nx)]

    if log:
        log(f"  卫星图：z{z}，{nx}×{ny}＝{len(tiles)} 块瓦片，"
            f"约 {mpp((minlat+maxlat)/2, z):.2f} 米/像素")

    # 硬上限：范围太大了就不能硬拼。
    # 不加这道闸，2°×2° 的范围会拼出 4 万格宽、25 亿像素的图，直接吃爆内存。
    if len(tiles) > HARD_MAX_TILES or nx * TILE * ny * TILE > HARD_MAX_PX:
        km_x = (maxlon - minlon) * 111.32 * math.cos(math.radians(minlat))
        km_y = (maxlat - minlat) * 110.9
        raise RuntimeError(
            f"范围太大（约 {km_x:.1f}×{km_y:.1f} 公里），卫星图要 {len(tiles)} 块瓦片。"
            f"请把范围缩到 3 公里见方以内，或者关掉卫星图底图")

    t0 = time.time()
    canvas = Image.new("RGB", (nx * TILE, ny * TILE), (58, 58, 58))
    failed, err = 0, None

    def work(t):
        x, y = t
        try:
            return t, _fetch(z, x, y)
        except Exception as e:      # noqa: BLE001
            return t, e

    done = 0
    last_log = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for t, res in ex.map(work, tiles):
            x, y = t
            if isinstance(res, Exception):
                failed += 1
                err = err or res
                # 前 6 块全挂，基本是网络不通，别让用户干等
                done += 1
                if done >= 6 and failed == done:
                    raise RuntimeError(f"卫星图取不到：{res}")
                continue
            try:
                from io import BytesIO
                im = Image.open(BytesIO(res)).convert("RGB")
                if im.size != (TILE, TILE):
                    im = im.resize((TILE, TILE))
                canvas.paste(im, ((x - tx0) * TILE, (y - ty0) * TILE))
            except Exception:       # noqa: BLE001
                failed += 1
            done += 1
            # 每 ~1 秒汇报一次进度，前端能实时看到「抓了几块瓦片」
            if log and time.time() - last_log > 1.0:
                last_log = time.time()
                log(f"    卫星图抓取中… {done}/{len(tiles)} 块瓦片")

    if failed and failed == len(tiles):
        raise RuntimeError(f"卫星图瓦片全部取不到：{err}")

    # 裁到正好是 bbox 的范围：这一步是「一一对应」的关键，
    # 裁出来的每个像素都严格对应一个经纬度区间。
    left = (x0f - tx0) * TILE
    top = (y0f - ty0) * TILE
    right = (x1f - tx0) * TILE
    bottom = (y1f - ty0) * TILE
    box = (int(round(left)), int(round(top)),
           int(round(right)), int(round(bottom)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        raise RuntimeError("范围太小，拼不出卫星图")
    img = canvas.crop(box)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "JPEG", quality=quality, optimize=True, progressive=True)

    info = {
        "file": out_path,
        "px": img.size,               # (宽, 高) 像素
        "mpp": mpp((minlat + maxlat) / 2, z),
        "zoom": z,
        "tiles": len(tiles),
        "failed": failed,
        "bytes": os.path.getsize(out_path),
        "seconds": round(time.time() - t0, 1),
        "bbox": [minlon, minlat, maxlon, maxlat],
        "blurry": mpp((minlat + maxlat) / 2, z) > BLUR_MPP,
        # 反查用：经纬度 -> 图像素坐标 需要这两个锚点
        "tile0": [tx0, ty0],
        "crop": [box[0], box[1]],
    }
    if log:
        log(f"    拼好 {img.size[0]}×{img.size[1]} 像素，"
            f"{info['bytes']/1048576:.1f} MB（{info['seconds']}s"
            + (f"，{failed} 块缺失" if failed else "") + "）")
        if info["blurry"]:
            log(f"    提醒：范围偏大，卫星图约 {info['mpp']:.1f} 米/像素，"
                f"只能当参照；要描楼建议分小块导出")
    return info


# ------------------------------------------------------------------ 放进 DXF
def attach(doc, msp, info, w_m, h_m, layer="卫星图", frame=True):
    """把拼好的影像插进 DXF，左下角对齐图纸西南角，尺寸正好是 (w, h) 米。

    DXF 的影像只能是「外部参照」——文件里存的是路径，不是像素数据。
    所以这里只写文件名（相对路径），jpg 和 dxf 放同一个文件夹就能显示；
    换个文件夹打开还找得到，是因为 AutoCAD 会在图纸所在目录里找。
    """
    rel = os.path.basename(info["file"])
    if layer not in doc.layers:
        try:
            doc.layers.add(layer, color=8)
        except Exception:           # noqa: BLE001
            pass
    img_def = doc.add_image_def(filename=rel, size_in_pixel=tuple(info["px"]))
    img = msp.add_image(
        image_def=img_def,
        insert=(-w_m / 2.0, -h_m / 2.0),
        size_in_units=(w_m, h_m),
        rotation=0,
        dxfattribs={"layer": layer},
    )
    try:
        doc.set_raster_variables(frame=0, quality=1, units="m")
    except Exception:               # noqa: BLE001
        pass
    if frame:
        # 范围框：影像没加载出来时，起码能看到「底图应该在哪」
        msp.add_lwpolyline(
            [(-w_m / 2.0, -h_m / 2.0), (w_m / 2.0, -h_m / 2.0),
             (w_m / 2.0, h_m / 2.0), (-w_m / 2.0, h_m / 2.0)],
            close=True,
            dxfattribs={"layer": layer, "color": 8,
                        "linetype": "CONTINUOUS"},
        )
    return img

