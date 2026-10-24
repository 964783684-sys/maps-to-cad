"""「卫星图」图层自检（完全不联网）。

卫星图这一层唯一要命的地方就是「对不上」。这里用纯数学把它钉死：
  1. 瓦片编号算得对不对（已知经纬度 -> 已知瓦片号）
  2. 级别选择：范围越大级别越低，瓦片数永远不超上限
  3. 插进 DXF 后的坐标：左下角 = 图纸西南角，宽高 = 图框宽高
  4. 每像素多少米 = 拼图时算的那个米/像素（1:1 的核心）
  5. 墨卡托 vs 局部等距投影的非线性误差有多小（这就是"一一对应"的精度）
  6. 范围框和影像四边完全重合
"""
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from map2cad import satellite  # noqa: E402
from map2cad.classify import LAYERS  # noqa: E402
from map2cad.projection import LocalProjector  # noqa: E402

OK, FAIL = [], []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra else ""))


# ============================================================ 1. 瓦片编号
x, y = satellite._tile_xy(0.0, 0.0, 0)
chk("经纬度 0,0 @z0 -> 瓦片中心 (0.5, 0.5)",
    abs(x - 0.5) < 1e-9 and abs(y - 0.5) < 1e-9, f"{x:.6f}, {y:.6f}")

x, y = satellite._tile_xy(0.0, 0.0, 1)
chk("经纬度 0,0 @z1 -> (1.0, 1.0)",
    abs(x - 1.0) < 1e-9 and abs(y - 1.0) < 1e-9, f"{x:.6f}, {y:.6f}")

x, y = satellite._tile_xy(-180.0, 85.05112878, 2)
chk("西北角 @z2 -> (0, 0)", abs(x) < 1e-6 and abs(y) < 1e-6, f"{x:.6f}, {y:.6f}")

x0, _ = satellite._tile_xy(120.0, 31.0, 14)
x1, _ = satellite._tile_xy(121.0, 31.0, 14)
chk("1 度经度 @z14 = 45.5 块瓦片（2^14/360）",
    abs((x1 - x0) - 2 ** 14 / 360.0) < 1e-6, f"{x1-x0:.4f}")

# 米/像素：赤道 z0 应该是 156543 米
chk("赤道 z0 分辨率 156543 米/像素",
    abs(satellite.mpp(0.0, 0) - 156543.034) < 1.0,
    f"{satellite.mpp(0.0, 0):.2f}")
chk("31 度 z18 约 0.51 米/像素", abs(satellite.mpp(31.0, 18) - 0.512) < 0.02,
    f"{satellite.mpp(31.0, 18):.3f}")

# ============================================================ 2. 级别选择
small = [121.4700, 31.2400, 121.4720, 31.2415]       # 约 190m x 165m
big = [121.40, 31.20, 121.50, 31.28]                 # 约 9.5km x 8.8km

z_small = satellite.pick_zoom(small)
z_big = satellite.pick_zoom(big)
chk("小范围用更高级别（更清楚）", z_small > z_big, f"小范围 z{z_small} > 大范围 z{z_big}")
chk("小范围到 19 级", z_small == 19, f"z{z_small}")

for name, bbox in (("小范围", small), ("大范围", big)):
    z = satellite.pick_zoom(bbox)
    x0f, y0f = satellite._tile_xy(bbox[0], bbox[3], z)
    x1f, y1f = satellite._tile_xy(bbox[2], bbox[1], z)
    nx = int(x1f) - int(x0f) + 1
    ny = int(y1f) - int(y0f) + 1
    chk(f"{name} z{z} 瓦片数 {nx}×{ny} 不超上限",
        nx * ny <= satellite.MAX_TILES and nx * 256 * ny * 256 <= satellite.MAX_PX,
        f"{nx * ny} 块 / {nx*256*ny*256/1e6:.0f} 万像素")

# 超大范围必须直接拒绝，不能硬拼（会吃爆内存）
try:
    satellite.build([120.0, 30.0, 122.0, 32.0],
                    os.path.join(tempfile.gettempdir(), "t_sat_huge.jpg"), log=None)
    chk("超大范围会被拒绝（不硬拼）", False, "居然没报错")
except RuntimeError as e:
    chk("超大范围会被拒绝（不硬拼）", "范围太大" in str(e), str(e)[:40])

# ============================================================ 3/4/6. 插进 DXF
from PIL import Image  # noqa: E402

tmp = tempfile.gettempdir()
jpg = os.path.join(tmp, "t_sat_卫星图.jpg")

BBOX = [121.4700, 31.2400, 121.4720, 31.2415]
proj = LocalProjector((BBOX[0] + BBOX[2]) / 2, (BBOX[1] + BBOX[3]) / 2)
W, H = proj.bbox_size(BBOX)

MPP = 0.5                                   # 假设影像 0.5 米/像素
PXW, PXH = int(round(W / MPP)), int(round(H / MPP))
Image.new("RGB", (PXW, PXH), (110, 130, 120)).save(jpg, "JPEG")
info = {"file": jpg, "px": (PXW, PXH), "mpp": MPP, "zoom": 19,
        "bytes": os.path.getsize(jpg), "failed": 0}

doc = ezdxf.new("R2013", setup=True)
msp = doc.modelspace()
satellite.attach(doc, msp, info, W, H)

img = list(msp.query("IMAGE"))[0]
ins = tuple(img.dxf.insert)
chk("影像插在「卫星图」图层上", img.dxf.layer == "卫星图", img.dxf.layer)
chk("图层已定义（不用手动建）", "卫星图" in doc.layers)
chk("左下角 = 图纸西南角",
    abs(ins[0] + W / 2) < 1e-6 and abs(ins[1] + H / 2) < 1e-6,
    f"({ins[0]:.2f}, {ins[1]:.2f}) vs ({-W/2:.2f}, {-H/2:.2f})")

ux, uy, _ = img.dxf.u_pixel
vx, vy, _ = img.dxf.v_pixel
chk("影像宽度 = 图框宽度",
    abs(ux * PXW - W) < 1e-6, f"{ux*PXW:.3f} vs {W:.3f} m")
chk("影像高度 = 图框高度",
    abs(vy * PXH - H) < 1e-6, f"{vy*PXH:.3f} vs {H:.3f} m")
chk("没有旋转（u 朝东、v 朝北）",
    abs(uy) < 1e-9 and abs(vx) < 1e-9 and ux > 0 and vy > 0)

chk("横向 1 像素 = 0.5 米", abs(ux - MPP) < 0.01, f"{ux:.4f} 米/像素")
chk("纵向 1 像素 = 0.5 米（像素是方的）", abs(vy - MPP) < 0.01, f"{vy:.4f} 米/像素")
chk("整幅影像的米/像素与拼图时算的一致",
    abs(W / PXW - MPP) < 0.01 and abs(H / PXH - MPP) < 0.01,
    f"{W/PXW:.3f} / {H/PXH:.3f}")

idefs = list(doc.objects.query("IMAGEDEF"))
chk("影像定义写了相对文件名（不带盘符）",
    len(idefs) == 1 and os.path.basename(idefs[0].dxf.filename) == "t_sat_卫星图.jpg"
    and not os.path.isabs(idefs[0].dxf.filename),
    idefs[0].dxf.filename if idefs else "(没有)")
chk("影像定义里的像素尺寸对（和 jpg 实际尺寸一致）",
    idefs and tuple(idefs[0].dxf.image_size)[:2] == (float(PXW), float(PXH)),
    str(tuple(idefs[0].dxf.image_size)[:2]) if idefs else "")

frames = [e for e in msp.query("LWPOLYLINE") if e.dxf.layer == "卫星图"]
chk("有且只有一个范围框", len(frames) == 1, f"{len(frames)} 个")
if frames:
    e = frames[0]
    pts = [(p[0], p[1]) for p in e.get_points()]
    chk("范围框是闭合的", bool(e.closed))
    corners = {(-W / 2, -H / 2), (W / 2, -H / 2), (W / 2, H / 2), (-W / 2, H / 2)}
    got = {(round(a, 6), round(b, 6)) for a, b in pts}
    want = {(round(a, 6), round(b, 6)) for a, b in corners}
    chk("范围框四角 = 影像四角", got == want, f"{len(got)} 个角")

# ============================================================ 5. 投影误差
# DXF 用「局部等距」投影（经纬度线性），卫星图是墨卡托（纬度非线性）。
# 把影像线性铺满图框时，中间纬度会不会偏？量化一下最大偏差。
lat0 = (BBOX[1] + BBOX[3]) / 2
R = 6378137.0


def merc_y(lat):
    return R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


y_top, y_bot = merc_y(BBOX[3]), merc_y(BBOX[1])
worst = 0.0
for i in range(201):
    lat = BBOX[1] + (BBOX[3] - BBOX[1]) * i / 200.0
    frac = (lat - BBOX[1]) / (BBOX[3] - BBOX[1])          # 线性铺图的假设
    y_lin = y_bot + (y_top - y_bot) * frac
    worst = max(worst, abs(y_lin - merc_y(lat)))
chk("墨卡托 vs 等距投影的最大偏差 < 0.15 米（160m 范围）",
    worst < 0.15, f"{worst*100:.1f} 厘米")

# 大范围时误差会变大 —— 顺便确认量级，方便解释
bigdev = 0.0
b2 = [121.40, 31.20, 121.50, 31.28]
yt, yb = merc_y(b2[3]), merc_y(b2[1])
for i in range(201):
    lat = b2[1] + (b2[3] - b2[1]) * i / 200.0
    frac = (lat - b2[1]) / (b2[3] - b2[1])
    bigdev = max(bigdev, abs(yb + (yt - yb) * frac - merc_y(lat)))
chk("9km 范围偏差仍在 3 米内（够用，会在日志里提示）",
    bigdev < 3.0, f"{bigdev*100:.0f} 厘米")

print()
if FAIL:
    print(f"有 {len(FAIL)} 项没通过：" + "、".join(FAIL))
    sys.exit(1)
print(f"全部通过（{len(OK)} 项）")
