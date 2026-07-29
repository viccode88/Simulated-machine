"""簡易 HMI：全廠總覽 + 警報/跳機 + 快照控制（快速恢復測試環境）。"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
from aiohttp import web

BUS_API = os.environ.get("BUS_API", "http://plant-bus:8080")
HISTORIAN_API = os.environ.get("HISTORIAN_API", "http://historian:8081")

PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>火力發電廠模擬器 HMI</title>
<style>
 body{font-family:system-ui,"Noto Sans TC",sans-serif;margin:0;background:#0f1720;color:#dbe6f0}
 header{padding:10px 16px;background:#16212e;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 h1{font-size:16px;margin:0;font-weight:600}
 .pill{padding:2px 8px;border-radius:10px;font-size:12px;background:#243447}
 .pill.bad{background:#7a2222}.pill.warn{background:#7a5a10}.pill.ok{background:#1d5e34}
 main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:12px}
 .card{background:#16212e;border-radius:8px;padding:10px 12px}
 .card h2{font-size:13px;margin:0 0 8px;color:#8fa8c0;text-transform:uppercase;letter-spacing:.5px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td{padding:2px 0}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
 .q-STALE{color:#e0b53c}.q-BAD{color:#e06c6c}
 button{background:#24405e;color:#dbe6f0;border:0;border-radius:5px;padding:5px 10px;
        font-size:12px;cursor:pointer;margin:2px}
 button:hover{background:#31597f}
 input{background:#0f1720;color:#dbe6f0;border:1px solid #24405e;border-radius:4px;padding:4px}
 pre{white-space:pre-wrap;font-size:11px;max-height:220px;overflow:auto;margin:0;color:#9fb4c9}
 .trip{color:#ff8080;font-weight:600}
</style></head><body>
<header>
 <h1>火力發電廠工業控制模擬器</h1>
 <span class="pill" id="simtime">sim 0.0 s</span>
 <span class="pill" id="paused">RUN</span>
 <span class="pill" id="rtf" title="即時倍率：模擬時間前進速度 ÷ 真實時間">×–</span>
 <span class="pill" id="phase">冷態</span>
 <span class="pill" id="gen">0 快照世代</span>
 <button onclick="cmd('/api/sim/pause')">暫停</button>
 <button onclick="cmd('/api/sim/resume')">繼續</button>
 <button onclick="cmd('/api/sim/step',{ticks:10})">單步 10</button>
 <input id="snapname" value="baseline" size="12">
 <button onclick="snapshot('save')">存快照</button>
 <button onclick="snapshot('restore')">還原</button>
 <button onclick="snapshot('restore',true)">還原(清鎖存)</button>
</header>
<main>
 <div class="card"><h2>機組概要</h2><table id="overview"></table></div>
 <div class="card"><h2>設備狀態</h2><table id="devices"></table>
  <h2 style="margin-top:10px">其他參與者</h2><table id="observers"></table></div>
 <div class="card"><h2>程序量</h2><table id="signals"></table></div>
 <div class="card"><h2>事件與第一故障</h2><pre id="events"></pre></div>
 <div class="card"><h2>快照</h2><pre id="snapshots"></pre></div>
</main>
<script>
const STATE_NAMES=["OFF","STARTING","RUNNING","STOPPING","TRIPPED","PURGING","IGNITING",
                   "PRESSURIZING","MAINTENANCE","SAFE_HOLD"];
const KEY=[["boiler.pressure_bar_abs","鍋爐壓力 bar(a)"],["boiler.level_pct","鍋爐水位 %"],
 ["turbine.speed_rpm","汽輪機轉速 RPM"],["generator.electrical_power_mw","發電量 MW"],
 ["condenser.pressure_bar_abs","冷凝器壓力 bar(a)"],["feedwater_tank.level_pct","給水槽水位 %"],
 ["steam_valve.position_pct","主蒸汽閥 %"],["feedwater_pump.flow_kg_s","給水流量 kg/s"],
 ["condensate_pump.flow_kg_s","凝結水流量 kg/s"],["condenser.hotwell_level_pct","熱井水位 %"]];
async function cmd(p,body){await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})});refresh();}
async function snapshot(action,clean){
  const name=document.getElementById('snapname').value;
  await cmd('/api/snapshot/'+action,{name:name,clear_latches:!!clean});
}
function fmt(v){return (Math.abs(v)<10?v.toFixed(3):v.toFixed(2));}
// 即時倍率：用兩次輪詢之間「模擬時間前進量 ÷ 真實時間」估算。
// 自持啟動要 930 模擬秒，倍率掉到 1 以下時畫面看起來會像停住，所以直接標示出來。
let lastSim=null,lastWall=null,rtf=null;
function updateRate(s){
 const now=performance.now()/1000;
 if(lastSim===null){lastSim=s.sim_time;lastWall=now;}
 else if(now-lastWall>0.3){
  const r=(s.sim_time-lastSim)/(now-lastWall);
  rtf=(rtf===null)?r:rtf*0.6+r*0.4; lastSim=s.sim_time; lastWall=now;}
 const el=document.getElementById('rtf');
 if(s.paused){el.textContent='×0 暫停中';el.className='pill warn';}
 else if(rtf===null){el.textContent='×–';el.className='pill';}
 else{el.textContent='即時 ×'+rtf.toFixed(2)+(s.speed&&s.speed!==1?' / 設定 ×'+s.speed:'');
      el.className='pill '+(rtf<0.5?'warn':'ok');}
}
// 自持啟動沒有順序器，靠允許條件自然浮現；把目前階段標出來，
// 免得「才 30 秒、鍋爐還在吹掃」被誤判成設備沒有自持。
function updatePhase(s){
 const S=n=>{const p=s.participants[n];return p?(STATE_NAMES[p.state]||''):'';};
 const V=n=>(s.signals[n]||{}).value||0;
 const mw=V('generator.electrical_power_mw'),rpm=V('turbine.speed_rpm');
 const brk=V('generator.breaker_closed'),bs=S('boiler');
 let phase='冷態啟動中',ok=false;
 if(brk>0.5&&mw>=59){phase='滿載運轉 '+mw.toFixed(1)+' MW';ok=true;}
 else if(brk>0.5){phase='併聯加載中 '+mw.toFixed(1)+' MW';ok=true;}
 else if(rpm>10){phase='汽輪機升速中 '+rpm.toFixed(0)+' RPM';}
 else if(bs==='PRESSURIZING'){phase='鍋爐升壓中';}
 else if(bs==='IGNITING'){phase='鍋爐點火中';}
 else if(bs==='PURGING'){phase='鍋爐吹掃中（30 s）';}
 const el=document.getElementById('phase');
 el.textContent=phase; el.className='pill '+(ok?'ok':'');
 el.title='自持冷啟動全程約 930 模擬秒（即時約 15 分鐘）：'+
  '2 s 自行 START → 30 s 吹掃 → 約 230 s 併聯 → 約 930 s 到 60 MW。'+
  '要快轉用 plantctl speed 10，要直接滿載用 RESTORE_ON_BOOT=steady-60mw。';
}
async function refresh(){
 const s=await (await fetch('/api/state')).json();
 document.getElementById('simtime').textContent='sim '+s.sim_time.toFixed(1)+' s (tick '+s.tick+')';
 const p=document.getElementById('paused');
 p.textContent=s.paused?'PAUSED':'RUN'; p.className='pill '+(s.paused?'warn':'ok');
 document.getElementById('gen').textContent=s.snapshot_generation+' 快照世代';
 updateRate(s); updatePhase(s);
 let o='';
 for(const [k,label] of KEY){const sig=s.signals[k];
  if(!sig)continue;
  o+=`<tr><td>${label}</td><td class="q-${sig.quality}">${fmt(sig.value)}</td></tr>`;}
 document.getElementById('overview').innerHTML=o;
 // historian/HMI 是 observer，沒有設備狀態機；混在設備表裡會被畫成 OFF，
 // 看起來像有一台設備死掉。分開列。
 let d='',ob='';
 const expected=new Set(s.expected_devices||[]);
 for(const [name,info] of Object.entries(s.participants)){
  if(info.role!=='device'&&!expected.has(name)){
   ob+=`<tr><td>${name}</td><td>${(info.role||'?').toUpperCase()} 已連線</td></tr>`; continue;}
  d+=`<tr><td>${name}</td><td class="${info.tripped?'trip':''}">`+
     `${STATE_NAMES[info.state]||info.state}${info.tripped?' TRIP':''}</td></tr>`;}
 for(const name of s.offline_devices){d+=`<tr><td>${name}</td><td class="trip">OFFLINE</td></tr>`;}
 document.getElementById('devices').innerHTML=d;
 document.getElementById('observers').innerHTML=ob||'<tr><td>（無）</td><td></td></tr>';
 let g='';
 for(const [name,sig] of Object.entries(s.signals)){
  g+=`<tr><td>${name}</td><td class="q-${sig.quality}">${fmt(sig.value)}</td></tr>`;}
 document.getElementById('signals').innerHTML=g;
 const ev=await (await fetch('/api/events?limit=25')).json();
 document.getElementById('events').textContent=ev.map(e=>
  `${(e.sim_time||0).toFixed(1)}s ${e.device} ${e.event}`+
  (e.code?` code=${e.code}`:'')+(e.first_out?' [FIRST-OUT]':'')+
  (e.message?` ${e.message}`:'')).reverse().join('\\n');
 const sn=await (await fetch('/api/snapshot')).json();
 document.getElementById('snapshots').textContent=(sn.snapshots||[]).map(m=>
  `${m.name}  sim=${(m.sim_time||0).toFixed(1)}s  devices=${(m.devices||[]).length}`+
  `  ${m.description||''}`).join('\\n');
}
setInterval(refresh,1000);refresh();
</script></body></html>"""


async def proxy(request: web.Request) -> web.Response:
    target = BUS_API + request.match_info["path"]
    if request.query_string:
        target += "?" + request.query_string
    body = await request.text() if request.can_read_body else None
    async with aiohttp.ClientSession() as session:
        async with session.request(request.method, target, data=body,
                                   headers={"Content-Type": "application/json"}) as response:
            payload = await response.text()
            return web.Response(text=payload, status=response.status,
                                content_type=response.content_type)


async def index(_: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_route("*", "/api{path:.*}", proxy)
    return app


async def main() -> None:
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("HTTP_PORT", "8082")))
    await site.start()
    print(json.dumps({"event": "HMI_STARTED", "port": os.environ.get("HTTP_PORT", "8082")}),
          flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
