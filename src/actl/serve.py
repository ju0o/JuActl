"""JuActl board server: JSON API + static web board (stdlib only).

MainPC browser -> http://<asus-tailscale-ip>:8765/ -> live agent board.
No build step, no dependencies. Polling JSON keeps it simple and robust
over Tailscale.

  actl serve [--port 8765] [--host 0.0.0.0]

Endpoints (all JSON except /):
  GET  /                        board HTML (embedded)
  GET  /api/board               agents + panes snapshot
  GET  /api/preview?agent=X     verify row + pane text
  GET  /api/copy?agent=X        last response text (MainPC clipboard via browser)
  POST /api/send   {agent, text} send prompt to pane
  POST /api/remap  {agent, pane} remap agent to pane
  POST /api/refresh             reconcile + rescan
"""
from __future__ import annotations

import json
import hmac
import os
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from actl.agents.extract import extract_last_response
from actl.core.config import backup_config, get_target, load_config, save_config
from actl.core.discovery import STRONG_CONFIDENCE, discover, manual_map
from actl.core.registry import AGENTS, resolve_agent
from actl.core.validation import validate_target
from actl.tui import STATE_KO, _pane_board, _pane_preview, _unmapped_panes, _verify_row

BOARD_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JuActl — MainPC 에이전트 보드 (live)</title>
<style>
  :root { --bg:#0a0a12; --panel:#12121f; --line:#2a2a45; --txt:#e8e8f2; --dim:#6e6e8c;
          --neon:#00f0ff; --mag:#ff2fb3; --ok:#00ff9d; --warn:#ffb300; --bad:#ff3355; }
  * { box-sizing:border-box; font-family:ui-monospace,Consolas,"D2Coding","Malgun Gothic",monospace; }
  body { margin:0; background:var(--bg); color:var(--txt); position:relative; overflow-x:hidden; }
  body::before { content:""; position:fixed; inset:0; pointer-events:none; opacity:.16; z-index:9;
    background:repeating-linear-gradient(0deg, transparent 0 3px, rgba(0,240,255,.08) 4px, transparent 5px); }
  body::after { content:""; position:fixed; inset:0; pointer-events:none; z-index:8; opacity:.12;
    background-image:radial-gradient(rgba(255,255,255,.8) .6px, transparent .8px); background-size:5px 5px; mix-blend-mode:screen; }
  header { padding:12px 16px; border-bottom:1px solid var(--neon); display:flex; gap:14px; align-items:center; background:#05050c; position:relative; overflow:hidden; }
  header::after { content:"ACTL // REMOTE AGENT CONTROL // SIGNAL LOCKED"; position:absolute; right:16px; bottom:2px; color:var(--neon); opacity:.22; font-size:9px; letter-spacing:1px; }
  header h1 { font-size:16px; margin:0; color:var(--neon); position:relative; letter-spacing:1px; text-shadow:2px 0 var(--mag), -2px 0 #00ff9d; }
  header h1::before, header h1::after { content:attr(data-text); position:absolute; inset:0; pointer-events:none; opacity:.55; }
  header h1::before { color:var(--mag); transform:translate(2px,-1px); clip-path:inset(0 0 58% 0); }
  header h1::after { color:#00ff9d; transform:translate(-2px,1px); clip-path:inset(58% 0 0 0); }
  header .st { color:var(--dim); font-size:12px; margin-left:auto; }
  header .live { color:var(--ok); }
  main { display:grid; grid-template-columns:minmax(0,3fr) minmax(280px,2fr); gap:12px; padding:12px 16px; height:calc(100vh - 120px); min-height:480px; }
  .col { background:linear-gradient(135deg,rgba(18,18,31,.98),rgba(9,10,20,.98)); border:1px solid var(--line); border-radius:4px; padding:12px; overflow:auto; position:relative; box-shadow:0 0 0 1px rgba(0,240,255,.04), 0 12px 40px rgba(0,0,0,.22); }
  .col::before { content:"·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·"; position:absolute; top:3px; right:10px; color:var(--neon); opacity:.17; font-size:9px; letter-spacing:4px; pointer-events:none; }
  .col h2 { font-size:12px; color:var(--neon); margin:0 0 8px; }
  #paneTitle { color:var(--neon); font-weight:bold; }
  pre.pane { background:#05050c; border:1px solid var(--line); border-radius:8px; padding:12px; font-size:13px; line-height:1.55; white-space:pre-wrap; min-height:280px; color:#d8ffd8; }
  pre.resp { background:#05050c; border:1px solid var(--mag); border-radius:8px; padding:12px; font-size:13px; white-space:pre-wrap; min-height:120px; }
  .btns { display:flex; gap:8px; margin:10px 0; flex-wrap:wrap; }
  button { background:#1e1e35; color:var(--txt); border:1px solid var(--line); border-radius:8px; padding:10px 16px; cursor:pointer; font-size:13px; font-weight:bold; }
  button:hover { border-color:var(--neon); color:var(--neon); }
  button.primary { background:#003844; color:var(--neon); border-color:var(--neon); }
  button:disabled { opacity:.4; cursor:default; }
  .cards { display:flex; flex-direction:column; gap:8px; }
  .card { border:1px solid var(--line); border-radius:3px; padding:9px 10px; cursor:pointer; background:rgba(12,13,25,.82); position:relative; overflow:hidden; transition:border-color .12s, transform .12s, background .12s; }
  .card::before { content:""; position:absolute; inset:0; pointer-events:none; opacity:.1; background-image:radial-gradient(var(--neon) .6px, transparent .7px); background-size:6px 6px; transform:translateX(18px); }
  .card:hover { border-color:var(--neon); transform:translateX(2px); background:#15152a; }
  .card.sel { border:1px solid var(--neon); box-shadow:3px 0 0 var(--mag), -3px 0 0 #00ff9d; }
  .card .nm { font-weight:bold; font-size:14px; }
  .card .sub { color:var(--dim); font-size:12px; margin-top:4px; white-space:pre-wrap; }
  .pill { display:inline-block; font-size:11px; border:1px solid var(--line); border-radius:2px; padding:2px 8px; margin-left:6px; letter-spacing:.5px; }
  .up{color:var(--ok);} .bad{color:var(--bad);} .warn{color:var(--warn);} .dim{color:var(--dim);} .acc{color:var(--neon);}
  textarea { width:100%; height:90px; background:#05050c; color:var(--txt); border:1px solid var(--line); border-radius:8px; padding:8px; font-size:13px; }
  .log { font-size:12px; color:var(--dim); max-height:160px; overflow:auto; }
  .kbd { border:1px solid var(--line); border-radius:4px; padding:0 5px; font-size:11px; }
  .tools { display:flex; gap:6px; margin-bottom:8px; }
  .tools input, .tools select { flex:1; min-width:0; background:#05050c; color:var(--txt); border:1px solid var(--line); border-radius:6px; padding:7px; }
  .filter { color:var(--dim); font-size:11px; }
  footer { padding:8px 16px; color:var(--dim); font-size:11px; }
  #verify { color:var(--ok); font-size:12px; margin:6px 0; }
  @media (max-width:820px) { main { grid-template-columns:1fr; height:auto; } header::after { display:none; } .col { min-height:420px; } }
  @media (prefers-reduced-motion:reduce) { .card { transition:none; } }
</style>
</head>
<body>
<header>
  <h1 data-text="▓ JuActl 보드 ░">▓ JuActl 보드 ░</h1>
  <span class="st" id="conn">연결 중…</span>
  <span class="st">자동새로고침 <span class="live" id="autoSt">ON</span> (5s) <span class="kbd" id="autoBtn" style="cursor:pointer" onclick="toggleAuto()">전환</span></span>
  <span class="st" id="clock"></span>
</header>
<main>
  <section class="col">
    <h2 id="paneTitle">live pane — 에이전트 클릭</h2>
    <div id="verify"></div>
    <pre class="pane" id="preview">에이전트 카드를 클릭하세요…</pre>
    <div class="btns">
      <button class="primary" onclick="doCopy()">⧉ 복사</button>
      <button onclick="doPrint()">⎙ 출력</button>
      <button onclick="showBoard()">▦ pane보드</button>
      <button onclick="refresh()">↻ 새로고침</button>
    </div>
    <h2>마지막 응답</h2>
    <pre class="resp" id="resp">— 복사/출력 버튼 —</pre>
  </section>
  <section class="col">
    <h2>에이전트</h2>
    <div class="tools"><input id="filter" placeholder="에이전트 검색…" aria-label="에이전트 검색"><select id="stateFilter" aria-label="상태 필터"><option value="ALL">전체</option><option value="UP">정상</option><option value="PROBLEM">문제</option><option value="UNKNOWN">미확인</option></select><span class="filter" id="count"></span></div>
    <div class="cards" id="agents"></div>
    <h2>메시지 전송</h2>
    <textarea id="msg" placeholder="선택한 에이전트 pane에 보낼 메시지… (Ctrl+Enter 전송)"></textarea>
    <div class="btns"><button class="primary" onclick="doSend()">➤ 전송</button></div>
    <h2>pane 보드</h2>
    <div class="log" id="board"></div>
    <h2>이벤트 로그</h2>
    <div class="log" id="log"></div>
  </section>
</main>
<footer>복사 = 브라우저 클립보드 직행 · pane보드 = 전체 tmux 실시간 · 재매핑 = pane 행 클릭</footer>
<script>
let SEL = null, AUTO = true, ROWS = [], TOKEN = sessionStorage.getItem('actl-token') || new URLSearchParams(location.search).get('token') || '';
let FILTER = 'ALL';
if(TOKEN) { sessionStorage.setItem('actl-token', TOKEN); history.replaceState(null, '', location.pathname); }
const KO = {"UP":"정상","DOWN":"꺼짐","MISMATCH":"불일치","UNMAPPED":"미매핑","DETECTED":"감지됨"};
const CLS = {"UP":"up","DOWN":"bad","MISMATCH":"warn","UNMAPPED":"dim","DETECTED":"acc"};
function log(m){ const el=document.getElementById('log'); el.innerHTML=`<div>[${new Date().toLocaleTimeString()}] ${m}</div>`+el.innerHTML; }
function tick(){ document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }
setInterval(tick,1000); tick();
function toggleAuto(){ AUTO=!AUTO; document.getElementById('autoSt').textContent=AUTO?"ON":"OFF"; log("자동새로고침 "+(AUTO?"켬":"끔")); }
function esc(v){ return String(v??"").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c])); }
setInterval(()=>{ if(AUTO) refresh(true); },5000);
async function api(path, opts){
  opts=opts||{}; opts.headers=Object.assign({},opts.headers||{},TOKEN?{'Authorization':'Bearer '+TOKEN}:{});
  const r = await fetch(path, opts);
  const j = await r.json();
  if(r.status===401) { TOKEN=prompt('JuActl 웹 토큰을 입력하세요')||''; if(TOKEN){sessionStorage.setItem('actl-token',TOKEN); return api(path,opts);} }
  if(!j.ok) throw new Error(j.error||("HTTP "+r.status));
  return j.data;
}
async function refresh(quiet){
  try {
    const d = await api('/api/board');
    ROWS = d.agents;
    const good=ROWS.filter(a=>a.state==='UP').length;
    const problem=ROWS.filter(a=>['DOWN','MISMATCH'].includes(a.state)).length;
    const unknown=ROWS.length-good-problem;
    document.getElementById('conn').innerHTML = `<span class="live">● 정상 ${good}</span> · 문제 ${problem} · 미확인 ${unknown} · asus ${d.time}`;
    const box = document.getElementById('agents');
    box.innerHTML = "";
    const q=(document.getElementById('filter').value||'').toLowerCase();
    const visible=ROWS.filter(a=>{
      const textMatch=`${a.display} ${a.agent} ${a.target}`.toLowerCase().includes(q);
      const stateMatch=FILTER==='ALL' || (FILTER==='UP' && a.state==='UP') || (FILTER==='PROBLEM' && ['DOWN','MISMATCH'].includes(a.state)) || (FILTER==='UNKNOWN' && !['UP','DOWN','MISMATCH'].includes(a.state));
      return textMatch && stateMatch;
    });
    document.getElementById('count').textContent=`${visible.length}/${ROWS.length}`;
    visible.forEach(a=>{
      const div = document.createElement('div');
      div.className = "card"+(a.agent===SEL?" sel":"");
      div.dataset.agent = a.agent;
      div.innerHTML = `<span class="nm">${esc(a.display)}</span><span class="pill ${CLS[a.state]||'dim'}">${esc(a.target)} · ${esc(KO[a.state]||a.state)}</span><div class="sub">${esc(a.activity_ko||a.activity||"미확인")} · ${esc(a.result_flag||"-")} · ${esc((a.preview||a.detail||"—").slice(0,80))}</div>`;
      div.onclick = ()=>select(a.agent);
      box.appendChild(div);
    });
    if(!SEL && ROWS.length) select(ROWS[0].agent, true);
    document.getElementById('board').innerHTML = d.boardHtml;
    if(!quiet) log(`새로고침 완료 (live ${d.live})`);
  } catch(e){ log("새로고침 실패: "+e.message); }
}
document.getElementById('filter').addEventListener('input',()=>refresh(true));
document.getElementById('stateFilter').addEventListener('change',e=>{FILTER=e.target.value;refresh(true);});
async function select(agent, silent){
  SEL = agent;
  document.querySelectorAll('#agents .card').forEach(el=>el.classList.toggle('sel', el.dataset.agent===agent));
  try {
    const d = await api('/api/preview?agent='+encodeURIComponent(agent));
    document.getElementById('paneTitle').textContent = `▚ ${d.display} ${d.target} — live`;
    document.getElementById('verify').textContent = d.verify;
    document.getElementById('preview').textContent = d.text;
    if(!silent) log(`${d.display} 미리보기`);
  } catch(e){ document.getElementById('preview').textContent = "실패: "+e.message; }
}
async function doCopy(){
  if(!SEL) return;
  try {
    const d = await api('/api/copy?agent='+encodeURIComponent(SEL));
    await navigator.clipboard.writeText(d.text);
    document.getElementById('resp').textContent = d.text.slice(0,8000);
    log(`${d.display} 복사됨 (${d.text.length}자, 브라우저 클립보드)`);
  } catch(e){ log("복사 실패: "+e.message+" — 출력 버튼으로 수동 복사"); doPrint(); }
}
async function doPrint(){
  if(!SEL) return;
  try {
    const d = await api('/api/copy?agent='+encodeURIComponent(SEL));
    document.getElementById('resp').textContent = d.text.slice(0,8000);
    log(`${d.display} 출력됨`);
  } catch(e){ log("출력 실패: "+e.message); }
}
async function showBoard(){
  document.getElementById('preview').textContent = document.getElementById('board').innerText;
  log("pane 보드 표시 — 행 클릭으로 즉시 매핑");
}
async function doSend(){
  if(!SEL) return;
  const v = document.getElementById('msg').value.trim();
  if(!v){ log("빈 메시지"); return; }
  if(!confirm(`${SEL}에 메시지를 전송할까요?\n\n${v.slice(0,160)}${v.length>160?'…':''}`)) return;
  try {
    const d = await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent:SEL,text:v})});
    document.getElementById('msg').value = "";
    log(`${d.display}에 전송됨 (${d.target})`);
  } catch(e){ log("전송 실패: "+e.message); }
}
document.getElementById('msg').addEventListener('keydown',e=>{ if(e.ctrlKey&&e.key==='Enter') doSend(); });
document.getElementById('board').addEventListener('click', async e=>{
  const row = e.target.closest('[data-pane]');
  if(!row) return;
  try {
    const d = await api('/api/remap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent:row.dataset.agent||SEL, pane:row.dataset.pane})});
    log(`${d.display} → ${d.target} 매핑됨`);
    refresh(true); select(d.agent, true);
  } catch(err){ log("매핑 실패: "+err.message); }
});
refresh();
</script>
</body>
</html>
"""


def _board_data(*, reconcile: bool = False) -> dict:
    import datetime

    config = load_config()
    try:
        from actl.cli import _auto_reconcile

        if reconcile:
            config = _auto_reconcile(config)
    except Exception:
        pass
    from actl.tui import _rows

    rows = _rows(config)
    live = sum(1 for r in rows if r["state"] == "UP")
    from actl.tui import _pane_board

    board = _pane_board(config)
    board_html = ""
    for line in board.splitlines():
        import html as _html
        import re as _re

        m = _re.match(r"\s*\[(.+?)\]\s+(%\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+결과(\S+)\s+(\S+)\s+(.*)", line)
        if m:
            key, pane, cmd, agent, activity, result, state, path = m.groups()
            agent_key = agent if agent in AGENTS else ""
            board_html += (
                f'<div data-pane="{_html.escape(pane, quote=True)}" data-agent="{_html.escape(agent_key, quote=True)}" style="cursor:pointer">'
                f"{_html.escape(f'[{key}] {pane} {cmd} {agent} {activity} 결과{result} {state} {path}')}</div>"
            )
        else:
            board_html += f"<div>{_html.escape(line)}</div>"
    return {
        "agents": [
            {"agent": r["agent"], "display": r["display"], "target": r["target"],
             "state": r["state"], "preview": r["preview"], "detail": r["detail"],
             "activity": r.get("busy", "미확인"), "activity_ko": r.get("busy", "미확인"),
             "result_flag": r.get("result_flag", "-")}
            for r in rows
        ],
        "live": live,
        "boardHtml": board_html,
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
    }


def _canonical_agent(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("agent must be a non-empty string")
    agent = resolve_agent(value)
    if agent is None:
        raise ValueError(f"unknown agent: {value}")
    return agent


def _prompt_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("text must be a non-empty string")
    if len(value) > 100_000:
        raise ValueError("text exceeds 100000 characters")
    return value


def _pane_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("pane must be a non-empty string of at most 200 characters")
    return value


def _preview_data(agent: str) -> dict:
    agent = _canonical_agent(agent)
    config = load_config()
    spec = AGENTS[agent]
    target = get_target(config, agent).target
    return {
        "display": spec.display_name,
        "target": target,
        "verify": _verify_row(agent, target, config),
        "text": _pane_preview(target),
    }


def _copy_data(agent: str) -> dict:
    import hashlib

    agent = _canonical_agent(agent)
    config = load_config()
    spec = AGENTS[agent]
    target = get_target(config, agent).target
    result = extract_last_response(agent, target, config)
    if not result.text:
        raise ValueError(result.detail or "응답 없음")
    from actl.core.audit import record

    record("copy", agent=agent, target=target, ok=True, mode="browser",
           source=result.source, confidence=result.confidence, chars=len(result.text),
           result_hash=hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16])
    return {"display": spec.display_name, "target": target, "text": result.text}


class Handler(BaseHTTPRequestHandler):
    server_version = "JuActlBoard/1.0"
    auth_token = ""

    def log_message(self, *args) -> None:
        pass

    def _json(self, ok: bool, data=None, error: str = "", code: int = 200) -> None:
        body = json.dumps({"ok": ok, "data": data, "error": error}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.auth_token:
            return True
        supplied = self.headers.get("Authorization", "")
        token = supplied[7:] if supplied.startswith("Bearer ") else ""
        if urllib.parse.urlparse(self.path).path == "/":
            token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [token])[0]
        if hmac.compare_digest(token, self.auth_token):
            return True
        self._json(False, error="웹 토큰이 필요합니다", code=401)
        return False

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized():
            return
        try:
            if parsed.path == "/":
                body = BOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/board":
                self._json(True, _board_data())
                return
            if parsed.path == "/api/preview":
                agent = urllib.parse.parse_qs(parsed.query).get("agent", [""])[0]
                self._json(True, _preview_data(agent))
                return
            if parsed.path == "/api/copy":
                agent = urllib.parse.parse_qs(parsed.query).get("agent", [""])[0]
                self._json(True, _copy_data(agent))
                return
            self._json(False, error="unknown endpoint", code=404)
        except ValueError as exc:
            self._json(False, error=str(exc)[:300], code=400)
        except Exception as exc:
            self._json(False, error=f"{type(exc).__name__}: {exc}"[:300], code=500)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
        except Exception:
            self._json(False, error="invalid JSON", code=400)
            return
        try:
            if self.path == "/api/send":
                from actl.cli import _send_to_selected

                agent = _canonical_agent(payload.get("agent"))
                text = _prompt_value(payload.get("text"))
                target = _send_to_selected(load_config(), agent, text)
                spec = AGENTS[agent]
                self._json(True, {"display": spec.display_name, "target": target})
                return
            if self.path == "/api/remap":
                from actl.core.discovery import target_matches

                agent = _canonical_agent(payload.get("agent"))
                pane = _pane_value(payload.get("pane"))
                config = load_config()
                dets = [d for d in discover()
                        if d.agent == agent and d.confidence in STRONG_CONFIDENCE]
                det = next((d for d in dets if target_matches(d.pane, pane)), None)
                if det is None:
                    raise ValueError(f"pane 없음: {pane}")
                updated = manual_map(config, agent, det)
                backup = backup_config()
                save_config(updated)
                from actl.core.audit import record

                record("map", agent=agent, target=det.pane.pane_id, ok=True, source="web")
                spec = AGENTS[agent]
                self._json(True, {"display": spec.display_name, "target": det.pane.pane_id,
                                  "backup": backup.name})
                return
            if self.path == "/api/refresh":
                self._json(True, _board_data(reconcile=True))
                return
            self._json(False, error="unknown endpoint", code=404)
        except ValueError as exc:
            self._json(False, error=str(exc)[:300], code=400)
        except Exception as exc:
            self._json(False, error=f"{type(exc).__name__}: {exc}"[:300], code=500)


def run_serve(host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> int:
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    auth_token = token or (None if loopback else os.environ.get("ACTL_WEB_TOKEN"))
    if not loopback and not auth_token:
        auth_token = secrets.token_urlsafe(24)
        print(f"ACTL_WEB_TOKEN={auth_token}")
    Handler.auth_token = auth_token or ""
    server = ThreadingHTTPServer((host, port), Handler)
    suffix = f"?token={auth_token}" if auth_token else ""
    print(f"JuActl board: http://{host}:{port}/{suffix} (MainPC: http://<asus-ip>:{port}/{suffix})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
