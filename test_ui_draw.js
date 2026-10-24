// 用真实 Chrome 走一遍「竖图 / 横图 / 自由」框选，验证 UI 与比例
const PAGE = process.argv[2];
const DBG = process.argv[3] || 9222;

let seq = 0, ws = null;
const waiters = new Map();

function connect(url) {
  return new Promise((res, rej) => {
    ws = new WebSocket(url);
    ws.onopen = () => res();
    ws.onerror = e => rej(new Error('ws error'));
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
    };
  });
}
function send(method, params) {
  const id = ++seq;
  return new Promise(res => { waiters.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
}
async function ev(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result && r.result.result ? r.result.result.value : undefined;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // 找一个已打开的页面
  const list = await (await fetch(`http://127.0.0.1:${DBG}/json/list`)).json();
  const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
  if (!page) throw new Error('没有可用的页面');
  await connect(page.webSocketDebuggerUrl);
  await send('Runtime.enable', {});

  // 等 Leaflet 和 bindUI 就绪
  for (let i = 0; i < 60; i++) {
    const ok = await ev('typeof map==="object" && !!map && typeof drawRatio!=="undefined" && !!document.getElementById("btnDrawP")');
    if (ok) break;
    await sleep(500);
  }
  const ready = await ev('typeof map==="object" && !!map');
  console.log('页面就绪：', ready);
  if (!ready) throw new Error('地图没起来（Leaflet CDN 可能没加载出来）');

  let bad = 0;
  const chk = (ok, msg, extra) => { console.log((ok ? '  ✓ ' : '  ✗ ') + msg + (extra ? '  ' + extra : '')); if (!ok) bad++; };

  // —— 1. 按钮存在、默认都是亮的吗 ——
  const btns = await ev(`['btnDraw','btnDrawP','btnDrawL'].map(id=>{
      const b=document.getElementById(id);
      return id+'|'+b.textContent.trim().replace(/\\s+/g,' ')+'|'+(b.classList.contains('on')?'on':'-');
    })`);
  console.log('按钮：', btns);
  chk(btns.length === 3, '三个框选按钮都在');

  // 工具函数：在页面里按 Leaflet 事件模拟一次拖拽
  const drag = (lat1, lng1, lat2, lng2) => ev(`(function(){
      const a=L.latLng(${lat1},${lng1}), b=L.latLng(${lat2},${lng2});
      map.fire('mousedown',{latlng:a});
      map.fire('mousemove',{latlng:b});
      map.fire('mouseup',{latlng:b});
      const bb=bbox; if(!bb) return {none:true};
      const [w,s,e,n]=bb;
      const kx=111320*Math.cos((s+n)/2*Math.PI/180), ky=110570;
      const W=(e-w)*kx, H=(n-s)*ky;
      return {W:W,H:H,ratio:W/H, drawMode:drawMode,
              on:['btnDraw','btnDrawP','btnDrawL'].filter(i=>document.getElementById(i).classList.contains('on')),
              info:document.getElementById('bboxInfo').innerText.replace(/\\n/g,' | ')};
    })()`);

  const P = { w: 410.2726, h: 509.2377 }, Ls = { w: 417.8744, h: 256.3716 };

  // —— 2. 竖图 ——
  console.log('\n竖图框选：');
  let st = await ev(`document.getElementById('btnDrawP').click();
      ({mode:drawMode, name:drawRatio&&drawRatio.name,
        on:document.getElementById('btnDrawP').classList.contains('on'),
        hint:document.getElementById('drawHint').innerText})`);
  chk(st.mode === true && st.name === '竖图' && st.on, '点了以后进入竖图模式、按钮变亮', st.hint);
  let r1 = await drag(31.2304, 121.4737, 31.2350, 121.4800);
  const err1 = Math.abs(r1.ratio / (P.w / P.h) - 1);
  chk(err1 < 1e-4, `拖出来的框比例 = 竖图（误差 ${(err1 * 100).toFixed(5)}%）`, r1.W.toFixed(1) + ' × ' + r1.H.toFixed(1) + ' m');
  chk(r1.drawMode === false && r1.on.length === 0, '松手后自动退出绘制状态，按钮复原');
  chk(/竖图/.test(r1.info), '信息栏认出了竖图比例', r1.info);

  // —— 3. 横图 ——
  console.log('\n横图框选：');
  st = await ev(`document.getElementById('btnDrawL').click();
      ({mode:drawMode, name:drawRatio&&drawRatio.name,
        on:document.getElementById('btnDrawL').classList.contains('on')})`);
  chk(st.mode === true && st.name === '横图' && st.on, '点了以后进入横图模式、按钮变亮');
  let r2 = await drag(31.2304, 121.4760, 31.2330, 121.4800);
  const err2 = Math.abs(r2.ratio / (Ls.w / Ls.h) - 1);
  chk(err2 < 1e-4, `拖出来的框比例 = 横图（误差 ${(err2 * 100).toFixed(5)}%）`, r2.W.toFixed(1) + ' × ' + r2.H.toFixed(1) + ' m');
  chk(/横图/.test(r2.info), '信息栏认出了横图比例', r2.info);

  // —— 4. 往四个方向拉，比例都不能变 ——
  console.log('\n四个方向拉（比例都得锁住）：');
  for (const [n, a, b] of [['右上', [31.2304, 121.4760], [31.2333, 121.4795]],
                           ['左下', [31.2333, 121.4795], [31.2304, 121.4750]],
                           ['左上', [31.2304, 121.4795], [31.2333, 121.4750]],
                           ['右下', [31.2333, 121.4750], [31.2290, 121.4800]]]) {
    await ev(`document.getElementById('btnDrawP').click()`);
    const r = await drag(a[0], a[1], b[0], b[1]);
    const e = Math.abs(r.ratio / (P.w / P.h) - 1);
    chk(e < 1e-4, `竖图 ${n}  比例误差 ${(e * 100).toFixed(5)}%`, r.W.toFixed(1) + ' × ' + r.H.toFixed(1));
  }

  // —— 5. 自由框选不受影响 ——
  console.log('\n自由框选：');
  await ev(`document.getElementById('btnDraw').click()`);
  const r3 = await drag(31.2304, 121.4760, 31.2314, 121.4785);
  const expect = (0.0025 * 111320 * Math.cos((31.2304 + 31.2314) / 2 * Math.PI / 180)) / (0.0010 * 110570);
  chk(Math.abs(r3.ratio / expect - 1) < 0.002, '自由框选还是按鼠标走的，没被比例绑架',
      r3.W.toFixed(1) + ' × ' + r3.H.toFixed(1) + ' = ' + r3.ratio.toFixed(3) + '（期望 ' + expect.toFixed(3) + '）');

  // —— 6. 再点一次 = 取消；Esc 也能取消 ——
  await ev(`document.getElementById('btnDrawP').click()`);
  const on2 = await ev(`document.getElementById('btnDrawP').click(); ({mode:drawMode, on:document.getElementById('btnDrawP').classList.contains('on')})`);
  chk(on2.mode === false && on2.on === false, '同一个按钮点第二下 = 取消绘制');
  await ev(`document.getElementById('btnDrawP').click()`);
  const esc = await ev(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); ({mode:drawMode, on:document.querySelectorAll('.ratio-btn.on').length})`);
  chk(esc.mode === false && esc.on === 0, '按 Esc 能退出绘制模式');

  console.log(bad ? `\n有 ${bad} 项没过` : '\n全部通过');
  ws.close();
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error('出错：', e.message); process.exit(2); });
