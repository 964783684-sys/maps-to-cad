"""自检脚本：用内置演示数据生成 GeoJSON -> DXF，再读回来验证。

不需要联网，用来确认"转换"这一段是好的。双击 自检.bat 也能跑。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from map2cad.demo import build_demo_geojson    # noqa: E402
from map2cad.dxf_export import export_dxf      # noqa: E402

OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

fc = build_demo_geojson()
gj = OUT / "演示数据.geojson"
gj.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"[1/3] GeoJSON：{len(fc['features'])} 个要素 -> {gj.name}")

dxf = OUT / "演示数据.dxf"
res = export_dxf(fc, str(dxf), text_height=2.5, road_mode="band",
                 label_mode="road", info_text=True, log=print)
print(f"[2/3] DXF：{res['polylines']} 条多段线，{res['texts']} 处注记，"
      f"{res['width_m']}x{res['height_m']} 米")

import ezdxf  # noqa: E402
doc = ezdxf.readfile(str(dxf))
msp = doc.modelspace()
kinds = {}
for e in msp:
    kinds[e.dxftype()] = kinds.get(e.dxftype(), 0) + 1
layers = [l.dxf.name for l in doc.layers if l.dxf.name not in ("0", "Defpoints")]
texts = [e.dxf.text for e in msp if e.dxftype() == "TEXT"]

ok = True
def chk(name, cond, extra=""):
    global ok
    print(("   OK   " if cond else "   FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False

print("[3/3] 读回验证：")
chk("DXF 能被重新读取", len(kinds) > 0, kinds)
chk("中文图层名正常", "建筑" in layers and "道路-主干" in layers, f"{len(layers)} 个图层")
chk("中文注记正常", any("路" in t for t in texts), texts[:3])
chk("单位是米", doc.header["$INSUNITS"] == 6)
chk("中文字体样式存在", "CN" in [s.dxf.name for s in doc.styles])

print()
if ok:
    print(f"自检通过。打开 {dxf.name} 看看效果吧。")
else:
    print("自检有问题，请把上面的输出发给技术支持。")
sys.exit(0 if ok else 1)
