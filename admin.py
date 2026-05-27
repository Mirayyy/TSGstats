"""
TSGstats — Local Admin UI
Локальный веб-интерфейс для управления pipeline'ом.

Запуск:
  python admin.py
  Открыть: http://localhost:5000
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import httpx
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

# ── .env loader (без python-dotenv) ──────────────────────────────────────────

_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_client() -> tuple[str, dict]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    return url, headers


# ── API: archives ─────────────────────────────────────────────────────────────

@app.get("/api/archives")
def api_archives():
    from downloader import list_remote_archives, _parse_archive_date

    archives = list_remote_archives()

    url, headers = _sb_client()
    try:
        with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
            resp = client.get("/rest/v1/processed_replays",
                              params={"select": "filename,status,processed_at"})
            status_map = {r["filename"]: r for r in resp.json()}
    except Exception:
        status_map = {}

    result = []
    for name in reversed(archives):  # newest first
        dt = _parse_archive_date(name)
        rec = status_map.get(name, {})
        result.append({
            "filename":     name,
            "date":         dt.isoformat() if dt else None,
            "server":       name.split(".")[0],
            "status":       rec.get("status", "pending"),
            "processed_at": rec.get("processed_at"),
        })
    return jsonify(result)


# ── API: games ────────────────────────────────────────────────────────────────

@app.get("/api/games")
def api_games():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=15) as client:
        resp = client.get("/rest/v1/games", params={
            "select": "id,server,map,mission,played_at,player_count,duration_sec",
            "order": "played_at.desc",
            "limit": "200",
        })
        resp.raise_for_status()
    return jsonify(resp.json())


@app.get("/api/games/<game_id>")
def api_game_detail(game_id):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        game_resp = client.get("/rest/v1/games", params={
            "id": f"eq.{game_id}", "limit": "1",
        })
        stats_resp = client.get("/rest/v1/player_game_stats", params={
            "game_id": f"eq.{game_id}",
            "select":  "kills,deaths,teamkills,suicides,steam_id,players(display_name)",
            "order":   "kills.desc",
        })
    game_resp.raise_for_status()
    stats_resp.raise_for_status()
    games = game_resp.json()
    return jsonify({
        "game":  games[0] if games else None,
        "stats": stats_resp.json(),
    })


@app.delete("/api/games/<game_id>")
def api_delete_game(game_id):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        # CASCADE в схеме удалит player_game_stats автоматически
        resp = client.delete("/rest/v1/games", params={"id": f"eq.{game_id}"})
        resp.raise_for_status()
    return jsonify({"ok": True})


# ── API: processed_replays ────────────────────────────────────────────────────

@app.get("/api/processed")
def api_processed():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        resp = client.get("/rest/v1/processed_replays", params={
            "select": "filename,processed_at,status",
            "order":  "processed_at.desc",
            "limit":  "200",
        })
        resp.raise_for_status()
    return jsonify(resp.json())


@app.delete("/api/processed/errors")
def api_delete_errors():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        resp = client.delete("/rest/v1/processed_replays",
                             params={"status": "eq.error"})
        resp.raise_for_status()
    return jsonify({"ok": True})


@app.delete("/api/processed/<path:filename>")
def api_delete_processed(filename):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        resp = client.delete("/rest/v1/processed_replays",
                             params={"filename": f"eq.{filename}"})
        resp.raise_for_status()
    return jsonify({"ok": True})


# ── SSE helper ────────────────────────────────────────────────────────────────

def _stream(cmd: list[str], extra_env: dict | None = None) -> Response:
    """Запускает команду и стримит stdout как SSE."""
    env = {**os.environ, **(extra_env or {})}

    def generate():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=os.path.dirname(__file__),
        )
        for line in iter(proc.stdout.readline, ""):
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        proc.wait()
        yield f"data: {json.dumps({'__done__': True, 'code': proc.returncode})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── API: pipeline commands ────────────────────────────────────────────────────

@app.get("/api/run/pipeline")
def api_run_pipeline():
    days_back  = request.args.get("days_back", "7")
    max_per_run = request.args.get("max_per_run", "20")
    return _stream(
        [sys.executable, "pipeline.py"],
        {"DAYS_BACK": days_back, "MAX_PER_RUN": max_per_run},
    )


@app.get("/api/run/fetch")
def api_run_fetch():
    archive = request.args.get("archive", "")
    if not archive:
        return jsonify({"error": "archive required"}), 400
    return _stream([sys.executable, "pipeline.py", "--fetch", archive, "--out", "fetched"])


@app.get("/api/run/local")
def api_run_local():
    path   = request.args.get("path", "")
    server = request.args.get("server", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    cmd = [sys.executable, "pipeline.py", "--local", path]
    if server:
        cmd += ["--server", server]
    return _stream(cmd)


@app.get("/api/run/fetch-process")
def api_run_fetch_process():
    """Скачивает архив и сразу обрабатывает — двухшаговый SSE стрим."""
    archive = request.args.get("archive", "")
    if not archive:
        return jsonify({"error": "archive required"}), 400

    def generate():
        safe = archive.replace(".pbo.7z", "")
        dest = os.path.join(os.path.dirname(__file__), "fetched", safe)

        # Шаг 1: fetch
        yield f"data: {json.dumps('── Шаг 1: скачиваем архив ──')}\n\n"
        p1 = subprocess.Popen(
            [sys.executable, "pipeline.py", "--fetch", archive, "--out", "fetched"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=os.path.dirname(__file__),
        )
        log_path = None
        for line in iter(p1.stdout.readline, ""):
            yield f"data: {json.dumps(line.rstrip())}\n\n"
            if "log.txt сохранён:" in line:
                log_path = line.split("log.txt сохранён:")[-1].strip()
        p1.wait()

        if p1.returncode != 0 or not log_path:
            yield f"data: {json.dumps({'__done__': True, 'code': 1})}\n\n"
            return

        # Шаг 2: process
        yield f"data: {json.dumps('── Шаг 2: обрабатываем ──')}\n\n"
        p2 = subprocess.Popen(
            [sys.executable, "pipeline.py", "--local", log_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=os.path.dirname(__file__),
        )
        for line in iter(p2.stdout.readline, ""):
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        p2.wait()
        yield f"data: {json.dumps({'__done__': True, 'code': p2.returncode})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return HTML


# ─────────────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>TSGstats Admin</title>
<style>
:root{
  --bg:#0f1117;--surface:#1a1d2e;--surface2:#222536;
  --border:#2a2d3e;--text:#e2e8f0;--muted:#64748b;
  --green:#22c55e;--red:#ef4444;--yellow:#eab308;
  --blue:#3b82f6;--purple:#8b5cf6;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* top bar */
.topbar{background:var(--surface);border-bottom:1px solid var(--border);
        padding:0 20px;height:48px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.topbar h1{font-size:15px;font-weight:700;letter-spacing:-.3px}
.topbar .spacer{flex:1}

/* layout */
.layout{display:flex;flex:1;overflow:hidden}
.sidebar{width:172px;background:var(--surface);border-right:1px solid var(--border);
         padding:8px 0;flex-shrink:0;display:flex;flex-direction:column;gap:2px}
.nav-item{display:flex;align-items:center;gap:8px;padding:9px 14px;
          color:var(--muted);font-size:13px;cursor:pointer;border-left:2px solid transparent;
          user-select:none}
.nav-item:hover{background:rgba(255,255,255,.04);color:var(--text)}
.nav-item.active{color:var(--blue);background:rgba(59,130,246,.1);
                 border-left-color:var(--blue)}
.nav-sep{height:1px;background:var(--border);margin:6px 8px}

/* content */
.content{flex:1;overflow-y:auto;padding:20px}

/* terminal */
.terminal{height:220px;background:#080b10;border-top:1px solid var(--border);
          display:flex;flex-direction:column;flex-shrink:0}
.term-header{padding:6px 14px;border-bottom:1px solid var(--border);
             display:flex;align-items:center;gap:8px;font-size:11px;
             color:var(--muted);flex-shrink:0}
.term-dot{width:8px;height:8px;border-radius:50%}
.term-body{flex:1;overflow-y:auto;padding:8px 14px;font-family:monospace;
           font-size:12px;line-height:1.7}
.tline{white-space:pre-wrap;word-break:break-all;color:#94a3b8}
.tline.err{color:#fc8181}.tline.ok{color:#68d391}.tline.hdr{color:#7dd3fc;font-weight:600}

/* stats grid */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px}
.stat-val{font-size:26px;font-weight:700;line-height:1}
.stat-lbl{font-size:11px;color:var(--muted);margin-top:6px;text-transform:uppercase;letter-spacing:.5px}

/* toolbar */
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.search{background:var(--surface2);border:1px solid var(--border);color:var(--text);
        padding:7px 11px;border-radius:6px;font-size:13px;outline:none;min-width:220px}
.search:focus{border-color:var(--blue)}
select.filter{background:var(--surface2);border:1px solid var(--border);color:var(--text);
              padding:7px 11px;border-radius:6px;font-size:13px;outline:none;cursor:pointer}
.spacer{flex:1}

/* table */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;padding:8px 10px;color:var(--muted);font-weight:500;
        border-bottom:1px solid var(--border);font-size:11px;
        text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.tbl td{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}
.tbl tr:hover td{background:rgba(255,255,255,.025)}
.tbl tr.clickable{cursor:pointer}
.mono{font-family:monospace;font-size:11px}

/* badges */
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
.b-ok{background:rgba(34,197,94,.15);color:#22c55e}
.b-err{background:rgba(239,68,68,.15);color:#ef4444}
.b-pend{background:rgba(100,116,139,.15);color:#94a3b8}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:5px;padding:6px 12px;border-radius:6px;
     border:1px solid var(--border);background:transparent;color:var(--text);
     font-size:12px;cursor:pointer;transition:background .12s,border-color .12s}
.btn:hover{background:rgba(255,255,255,.07)}
.btn-primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn-primary:hover{background:#2563eb}
.btn-sm{padding:3px 8px;font-size:11px}
.btn-danger{border-color:rgba(239,68,68,.3);color:#ef4444}
.btn-danger:hover{background:rgba(239,68,68,.1)}
.btn-ghost{border-color:transparent;color:var(--muted)}
.btn-ghost:hover{color:var(--text);background:rgba(255,255,255,.06)}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* form */
.form-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.form-row label{font-size:13px;color:var(--muted);min-width:130px}
.inp{background:var(--surface2);border:1px solid var(--border);color:var(--text);
     padding:7px 11px;border-radius:6px;font-size:13px;outline:none}
.inp:focus{border-color:var(--blue)}
.inp-w80{width:80px}

/* card */
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;
      padding:18px;margin-bottom:16px}
.card-title{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;
            letter-spacing:.6px;margin-bottom:14px}

/* detail panel */
.detail{position:fixed;top:0;right:0;width:440px;height:100vh;
        background:var(--surface);border-left:1px solid var(--border);
        padding:20px;overflow-y:auto;z-index:200;
        transform:translateX(100%);transition:transform .2s ease}
.detail.open{transform:translateX(0)}
.detail-close{float:right;cursor:pointer;color:var(--muted);font-size:20px;line-height:1;
              background:none;border:none;padding:0}

/* loading */
.loading{color:var(--muted);font-size:13px;padding:30px 0;text-align:center}
.empty{color:var(--muted);font-size:13px;padding:40px 0;text-align:center}

/* player stats in detail */
.player-row{display:flex;align-items:center;gap:8px;padding:8px 0;
            border-bottom:1px solid rgba(255,255,255,.05)}
.player-name{flex:1;font-size:13px}
.kd-badge{background:var(--surface2);border-radius:4px;padding:2px 7px;font-size:11px;
          font-family:monospace;color:var(--muted)}
.kd-badge b{color:var(--text)}
</style>
</head>
<body>

<div class="topbar">
  <h1>⚙ TSGstats Admin</h1>
  <div class="spacer"></div>
  <button class="btn btn-primary" id="btn-run-pipeline" onclick="quickRunPipeline()">▶ Run Pipeline</button>
</div>

<div class="layout">

  <nav class="sidebar">
    <div class="nav-item active" data-tab="archives" onclick="showTab('archives')">
      📦 Архивы
    </div>
    <div class="nav-item" data-tab="games" onclick="showTab('games')">
      🎮 Игры
    </div>
    <div class="nav-item" data-tab="processed" onclick="showTab('processed')">
      ✅ Обработанные
    </div>
    <div class="nav-sep"></div>
    <div class="nav-item" data-tab="pipeline" onclick="showTab('pipeline')">
      ⚡ Pipeline
    </div>
  </nav>

  <main class="content" id="content">
    <div class="loading">Загрузка...</div>
  </main>

</div>

<div class="terminal">
  <div class="term-header">
    <div class="term-dot" id="term-dot" style="background:var(--muted)"></div>
    <span id="term-title">Terminal</span>
    <div style="flex:1"></div>
    <button class="btn btn-ghost btn-sm" onclick="clearTerm()">Очистить</button>
  </div>
  <div class="term-body" id="term"></div>
</div>

<!-- Detail panel -->
<div class="detail" id="detail-panel">
  <button class="detail-close" onclick="closeDetail()">×</button>
  <div id="detail-content"></div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════
let currentTab = '';
let currentSSE = null;
let allArchives = [];
let allGames = [];

// ═══════════════════════════════════════════════════════════════════
// TERMINAL
// ═══════════════════════════════════════════════════════════════════
function tLog(text, cls='') {
  const el = document.getElementById('term');
  const d = document.createElement('div');
  d.className = 'tline ' + cls;
  d.textContent = text;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}
function clearTerm() { document.getElementById('term').innerHTML = ''; }

function termBusy(title) {
  document.getElementById('term-dot').style.background = 'var(--yellow)';
  document.getElementById('term-title').textContent = title || 'Running…';
}
function termDone(ok) {
  document.getElementById('term-dot').style.background = ok ? 'var(--green)' : 'var(--red)';
  document.getElementById('term-title').textContent = ok ? 'Done ✓' : 'Failed ✗';
}

// ═══════════════════════════════════════════════════════════════════
// SSE STREAM
// ═══════════════════════════════════════════════════════════════════
function streamSSE(url, title, onDone) {
  if (currentSSE) currentSSE.close();
  clearTerm();
  termBusy(title);
  tLog('▶ ' + title, 'hdr');

  const es = new EventSource(url);
  currentSSE = es;

  es.onmessage = e => {
    const data = JSON.parse(e.data);
    if (typeof data === 'string') {
      const cls = (data.includes('ОШИБКА') || data.includes('Error') || data.includes('✗')) ? 'err'
               : (data.includes('Готово') || data.includes('успешно') || data.includes('✓'))  ? 'ok'
               : (data.startsWith('──') || data.startsWith('==='))                             ? 'hdr'
               : '';
      tLog(data, cls);
    } else if (data.__done__) {
      const ok = data.code === 0;
      tLog(ok ? '✓ Завершено успешно' : `✗ Ошибка (код ${data.code})`, ok ? 'ok' : 'err');
      termDone(ok);
      es.close();
      if (onDone) onDone(ok);
    }
  };
  es.onerror = () => { tLog('✗ Соединение прервано', 'err'); termDone(false); es.close(); };
}

// ═══════════════════════════════════════════════════════════════════
// TABS
// ═══════════════════════════════════════════════════════════════════
function showTab(tab) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
  currentTab = tab;
  closeDetail();
  if (tab === 'archives') loadArchives();
  else if (tab === 'games')     loadGames();
  else if (tab === 'processed') loadProcessed();
  else if (tab === 'pipeline')  renderPipeline();
}

// ═══════════════════════════════════════════════════════════════════
// ARCHIVES TAB
// ═══════════════════════════════════════════════════════════════════
async function loadArchives() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем список с сайта…</div>';
  const r = await fetch('/api/archives');
  allArchives = await r.json();
  renderArchives();
}

function renderArchives() {
  const search = (document.getElementById('arch-search')?.value || '').toLowerCase();
  const sf     = document.getElementById('arch-filter')?.value || 'all';

  const list = allArchives.filter(a =>
    (sf === 'all' || a.status === sf) &&
    (!search || a.filename.toLowerCase().includes(search))
  );

  const cnt = s => allArchives.filter(a => a.status === s).length;
  const pend = allArchives.length - cnt('ok') - cnt('error');

  document.getElementById('content').innerHTML = `
    <div class="stats-grid">
      <div class="stat"><div class="stat-val">${allArchives.length}</div><div class="stat-lbl">Всего архивов</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--green)">${cnt('ok')}</div><div class="stat-lbl">Обработано</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--yellow)">${pend}</div><div class="stat-lbl">Ожидает</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--red)">${cnt('error')}</div><div class="stat-lbl">Ошибки</div></div>
    </div>
    <div class="toolbar">
      <input class="search" id="arch-search" placeholder="Поиск по имени…" oninput="renderArchives()" value="${search}">
      <select class="filter" id="arch-filter" onchange="renderArchives()">
        <option value="all"${sf==='all'?' selected':''}>Все</option>
        <option value="pending"${sf==='pending'?' selected':''}>Ожидает</option>
        <option value="ok"${sf==='ok'?' selected':''}>Обработано</option>
        <option value="error"${sf==='error'?' selected':''}>Ошибка</option>
      </select>
      <div class="spacer"></div>
      <button class="btn" onclick="loadArchives()">↺ Обновить</button>
    </div>
    ${list.length === 0 ? '<div class="empty">Нет архивов по выбранным фильтрам</div>' : `
    <table class="tbl">
      <thead><tr>
        <th>Архив</th><th>Дата</th><th>Сервер</th><th>Статус</th><th style="width:120px"></th>
      </tr></thead>
      <tbody>
        ${list.map(a => {
          const date = a.date ? new Date(a.date).toLocaleString('ru',{dateStyle:'short',timeStyle:'short'}) : '—';
          const badge = {ok:'b-ok',error:'b-err',pending:'b-pend'}[a.status]||'b-pend';
          const fn = esc(a.filename);
          const actions = [];
          if (a.status !== 'ok')
            actions.push(`<button class="btn btn-sm btn-primary" onclick='fetchAndProcess("${fn}")' title="Скачать и обработать">▶</button>`);
          actions.push(`<button class="btn btn-sm" onclick='fetchOnly("${fn}")' title="Только скачать">⬇</button>`);
          if (a.status !== 'pending')
            actions.push(`<button class="btn btn-sm btn-danger" onclick='removePending("${fn}")' title="Сбросить статус">✕</button>`);
          return `<tr>
            <td class="mono" style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${fn}">${a.filename}</td>
            <td style="white-space:nowrap">${date}</td>
            <td>${a.server}</td>
            <td><span class="badge ${badge}">${a.status}</span></td>
            <td style="white-space:nowrap;display:flex;gap:4px">${actions.join('')}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`}
  `;
}

function fetchOnly(archive) {
  streamSSE('/api/run/fetch?archive='+enc(archive), 'Fetch: '+shortName(archive));
}
function fetchAndProcess(archive) {
  streamSSE('/api/run/fetch-process?archive='+enc(archive),
    'Fetch+Process: '+shortName(archive),
    ok => { if(ok) loadArchives(); });
}
async function removePending(filename) {
  await fetch('/api/processed/'+enc(filename), {method:'DELETE'});
  await loadArchives();
}

// ═══════════════════════════════════════════════════════════════════
// GAMES TAB
// ═══════════════════════════════════════════════════════════════════
async function loadGames() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем игры…</div>';
  const r = await fetch('/api/games');
  allGames = await r.json();
  renderGames();
}

function renderGames() {
  const search = (document.getElementById('games-search')?.value || '').toLowerCase();
  const list = allGames.filter(g =>
    !search || g.mission.toLowerCase().includes(search) ||
    g.map.toLowerCase().includes(search) || (g.server||'').toLowerCase().includes(search)
  );

  document.getElementById('content').innerHTML = `
    <div class="toolbar">
      <input class="search" id="games-search" placeholder="Поиск по миссии, карте, серверу…"
             oninput="renderGames()" value="${search}">
      <div class="spacer"></div>
      <span style="font-size:12px;color:var(--muted)">${list.length} игр</span>
      <button class="btn" onclick="loadGames()">↺</button>
    </div>
    ${list.length === 0 ? '<div class="empty">Игр нет</div>' : `
    <table class="tbl">
      <thead><tr>
        <th>Дата</th><th>Сервер</th><th>Миссия</th><th>Карта</th>
        <th style="text-align:center">Игроки</th><th>Длит.</th><th></th>
      </tr></thead>
      <tbody>
        ${list.map(g => {
          const date = new Date(g.played_at).toLocaleString('ru',{dateStyle:'short',timeStyle:'short'});
          const dur  = Math.round(g.duration_sec/60)+' мин';
          const gid  = esc(g.id);
          return `<tr class="clickable" onclick='openGameDetail("${gid}")'>
            <td style="white-space:nowrap">${date}</td>
            <td>${g.server||'—'}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${g.mission}</td>
            <td>${g.map}</td>
            <td style="text-align:center">${g.player_count}</td>
            <td>${dur}</td>
            <td onclick="event.stopPropagation()">
              <button class="btn btn-sm btn-danger" onclick='deleteGame("${gid}")' title="Удалить игру">✕</button>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`}
  `;
}

async function openGameDetail(gameId) {
  const panel  = document.getElementById('detail-panel');
  const content = document.getElementById('detail-content');
  panel.classList.add('open');
  content.innerHTML = '<div class="loading">Загружаем детали…</div>';

  const r    = await fetch('/api/games/'+enc(gameId));
  const data = await r.json();
  if (!data.game) { content.innerHTML = '<div class="empty">Не найдено</div>'; return; }

  const g    = data.game;
  const stats = data.stats || [];
  const date  = new Date(g.played_at).toLocaleString('ru');
  const dur   = Math.round(g.duration_sec/60)+' мин';
  const gid   = esc(g.id);

  content.innerHTML = `
    <h2 style="font-size:15px;font-weight:700;margin-bottom:6px">${g.mission}</h2>
    <p style="color:var(--muted);font-size:12px;margin-bottom:4px">${g.map}</p>
    <p style="color:var(--muted);font-size:12px;margin-bottom:16px">${g.server||'—'} · ${date} · ${dur}</p>

    <div style="display:flex;gap:8px;margin-bottom:20px">
      <button class="btn btn-sm btn-danger" onclick='deleteGame("${gid}")'>✕ Удалить игру</button>
    </div>

    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px">
      Статистика игроков (${stats.length})
    </div>

    ${stats.map(s => {
      const name = s.players?.display_name || s.steam_id;
      return `<div class="player-row">
        <div class="player-name">${name}</div>
        <span class="kd-badge">K <b style="color:var(--green)">${s.kills}</b></span>
        <span class="kd-badge">D <b style="color:var(--red)">${s.deaths}</b></span>
        ${s.teamkills > 0 ? `<span class="kd-badge">TK <b style="color:var(--yellow)">${s.teamkills}</b></span>` : ''}
      </div>`;
    }).join('')}
  `;
}

async function deleteGame(gameId) {
  if (!confirm('Удалить игру и всю статистику?')) return;
  await fetch('/api/games/'+enc(gameId), {method:'DELETE'});
  closeDetail();
  await loadGames();
}

// ═══════════════════════════════════════════════════════════════════
// PROCESSED TAB
// ═══════════════════════════════════════════════════════════════════
async function loadProcessed() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем…</div>';
  const r = await fetch('/api/processed');
  const data = await r.json();

  const ok  = data.filter(p=>p.status==='ok').length;
  const err = data.filter(p=>p.status==='error').length;

  document.getElementById('content').innerHTML = `
    <div class="toolbar">
      <span style="font-size:12px;color:var(--muted)">${data.length} записей · ${ok} ok · ${err} ошибок</span>
      <div class="spacer"></div>
      <button class="btn btn-sm btn-danger" onclick="clearErrors()">Удалить все ошибки</button>
      <button class="btn" onclick="loadProcessed()">↺</button>
    </div>
    <table class="tbl">
      <thead><tr><th>Архив</th><th>Обработан</th><th>Статус</th><th></th></tr></thead>
      <tbody>
        ${data.map(p => {
          const date  = new Date(p.processed_at).toLocaleString('ru',{dateStyle:'short',timeStyle:'short'});
          const badge = {ok:'b-ok',error:'b-err'}[p.status]||'b-pend';
          const fn    = esc(p.filename);
          return `<tr>
            <td class="mono" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${fn}">${p.filename}</td>
            <td style="white-space:nowrap">${date}</td>
            <td><span class="badge ${badge}">${p.status}</span></td>
            <td>
              <button class="btn btn-sm btn-danger" onclick='removePending("${fn}")' title="Удалить">✕</button>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;
}

async function clearErrors() {
  if (!confirm('Удалить все записи со статусом error?')) return;
  await fetch('/api/processed/errors', {method:'DELETE'});
  await loadProcessed();
}

// ═══════════════════════════════════════════════════════════════════
// PIPELINE TAB
// ═══════════════════════════════════════════════════════════════════
function renderPipeline() {
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="card-title">Запуск Pipeline (скачать + обработать новые)</div>
      <div class="form-row">
        <label>Дней назад</label>
        <input class="inp inp-w80" type="number" id="p-days" value="7" min="1" max="365">
      </div>
      <div class="form-row">
        <label>Макс. архивов</label>
        <input class="inp inp-w80" type="number" id="p-max" value="20" min="1" max="500">
      </div>
      <button class="btn btn-primary" onclick="startPipeline()">▶ Запустить</button>
    </div>

    <div class="card">
      <div class="card-title">Обработать локальный log.txt</div>
      <div class="form-row">
        <label>Путь к файлу</label>
        <input class="inp" type="text" id="p-local-path" placeholder="D:/Downloads/.../log.txt" style="flex:1">
      </div>
      <div class="form-row">
        <label>Сервер</label>
        <select class="inp" id="p-local-server" style="width:100px">
          <option value="">Авто</option>
          <option>T1</option><option>T2</option><option>T3</option>
        </select>
      </div>
      <button class="btn btn-primary" onclick="processLocal()">▶ Обработать</button>
    </div>

    <div class="card">
      <div class="card-title">Скачать архив без обработки</div>
      <div class="form-row">
        <label>Имя архива</label>
        <input class="inp" type="text" id="p-fetch-name"
               placeholder="T1.2026-05-20-20-27-54.mTSG%4016_....pbo.7z" style="flex:1">
      </div>
      <button class="btn" onclick="fetchByName()">⬇ Скачать</button>
    </div>
  `;
}

function startPipeline() {
  const days = document.getElementById('p-days').value;
  const max  = document.getElementById('p-max').value;
  streamSSE(`/api/run/pipeline?days_back=${days}&max_per_run=${max}`,
    'Pipeline', ok => { if(ok && currentTab==='archives') loadArchives(); });
}
function processLocal() {
  const path   = document.getElementById('p-local-path').value.trim();
  const server = document.getElementById('p-local-server').value;
  if (!path) { alert('Укажите путь к файлу'); return; }
  let url = '/api/run/local?path='+enc(path);
  if (server) url += '&server='+server;
  streamSSE(url, 'Local: '+path.split(/[/\\]/).pop());
}
function fetchByName() {
  const name = document.getElementById('p-fetch-name').value.trim();
  if (!name) { alert('Укажите имя архива'); return; }
  fetchOnly(name);
}

// ═══════════════════════════════════════════════════════════════════
// GLOBAL ACTIONS
// ═══════════════════════════════════════════════════════════════════
function quickRunPipeline() {
  streamSSE('/api/run/pipeline?days_back=7&max_per_run=20',
    'Pipeline', ok => { if(ok && currentTab==='archives') loadArchives(); });
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
}

// ═══════════════════════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════════════════════
const esc = s => s.replace(/\\\\/g,'\\\\').replace(/"/g,'&quot;').replace(/'/g,"\\'");
const enc = s => encodeURIComponent(s);
const shortName = s => s.replace('.pbo.7z','').split('.').slice(0,2).join('.');

// ── Init
showTab('archives');
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("\n  TSGstats Admin UI")
    print("  http://localhost:5000\n")
    app.run(debug=False, port=5000, threaded=True)
