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

# ── .env loader ───────────────────────────────────────────────────────────────

_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())


# ── Global error handler ──────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_error(e):
    import traceback
    return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_client() -> tuple[str, dict]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY не заданы. "
            "Создайте .env файл рядом с admin.py."
        )
    return url, {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_raise(resp) -> None:
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(f"Supabase {resp.status_code}: {body}")


def _sb_json(resp) -> list:
    """Returns parsed JSON list, or [] on non-list response."""
    try:
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── API: stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    url, headers = _sb_client()
    ch = {**headers, "Prefer": "count=exact"}

    def count(table, params=None):
        r = client.get(f"/rest/v1/{table}",
                       params={"select": "*", "limit": "0", **(params or {})},
                       headers=ch)
        cr = r.headers.get("Content-Range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0

    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        return jsonify({
            "games":         count("games"),
            "players":       count("players"),
            "processed_ok":  count("processed_replays", {"status": "eq.ok"}),
            "processed_err": count("processed_replays", {"status": "eq.error"}),
        })


# ── API: archives ─────────────────────────────────────────────────────────────

@app.get("/api/archives")
def api_archives():
    from downloader import list_remote_archives, _parse_archive_info

    # Remote site archives
    try:
        remote_names = set(list_remote_archives())
    except Exception as e:
        print(f"WARNING: не удалось получить список архивов с сайта: {e}")
        remote_names = set()

    # DB processed_replays — includes archives that may have been removed from site
    status_map: dict = {}
    try:
        url, headers = _sb_client()
        with httpx.Client(base_url=url, headers=headers, timeout=15) as client:
            resp = client.get("/rest/v1/processed_replays",
                              params={"select": "filename,status,processed_at",
                                      "limit": "5000"})
            data = _sb_json(resp)
            status_map = {r["filename"]: r for r in data}
    except Exception as e:
        print(f"WARNING: не удалось получить статусы обработки: {e}")

    # Union: remote archives + processed-but-no-longer-on-site
    all_names = remote_names | set(status_map.keys())

    result = []
    for name in all_names:
        info = _parse_archive_info(name)
        dt   = info["date"]
        rec  = status_map.get(name, {})
        result.append({
            "filename":     name,
            "date":         dt.isoformat() if dt else None,
            "server":       info["server"],
            "mission_type": info["mission_type"],
            "mission_name": info["mission_name"],
            "player_count": info["player_count"],
            "map":          info["map"],
            "status":       rec.get("status", "pending"),
            "processed_at": rec.get("processed_at"),
            "on_site":      name in remote_names,
        })

    result.sort(key=lambda a: a["date"] or "", reverse=True)
    return jsonify(result)


# ── API: games ────────────────────────────────────────────────────────────────

@app.get("/api/games")
def api_games():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=20) as client:
        resp = client.get("/rest/v1/games", params={
            "select": "id,server,map,mission,played_at,player_count,duration_sec",
            "order":  "played_at.desc",
            "limit":  "2000",
        })
        _sb_raise(resp)
    return jsonify(resp.json())


@app.get("/api/games/<game_id>")
def api_game_detail(game_id):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        gr = client.get("/rest/v1/games", params={"id": f"eq.{game_id}", "limit": "1"})
        sr = client.get("/rest/v1/player_game_stats", params={
            "game_id": f"eq.{game_id}",
            "select":  "kills,deaths,teamkills,suicides,steam_id,players(display_name)",
            "order":   "kills.desc",
        })
    _sb_raise(gr); _sb_raise(sr)
    games = gr.json()
    return jsonify({"game": games[0] if games else None, "stats": sr.json()})


@app.delete("/api/games/<game_id>")
def api_delete_game(game_id):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        _sb_raise(client.delete("/rest/v1/games", params={"id": f"eq.{game_id}"}))
    return jsonify({"ok": True})


@app.post("/api/games/bulk-action")
def api_games_bulk_action():
    """Bulk операции: delete selected or delete all."""
    data   = request.get_json() or {}
    action = data.get("action", "")
    ids    = data.get("ids", [])
    all_   = data.get("all", False)

    if action != "delete":
        return jsonify({"error": f"unknown action: {action}"}), 400

    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=30) as client:
        if all_:
            r = client.get("/rest/v1/games", params={"select": "id", "limit": "10000"})
            _sb_raise(r)
            all_ids = [g["id"] for g in _sb_json(r)]
            if all_ids:
                _sb_raise(client.delete("/rest/v1/games",
                                        params={"id": f"in.({','.join(all_ids)})"}))
            return jsonify({"ok": True, "deleted": len(all_ids)})
        elif ids:
            _sb_raise(client.delete("/rest/v1/games",
                                    params={"id": f"in.({','.join(ids)})"}))
            return jsonify({"ok": True, "deleted": len(ids)})
        else:
            return jsonify({"error": "no ids and all=false"}), 400


# ── API: players ──────────────────────────────────────────────────────────────

@app.get("/api/players")
def api_players():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=15) as client:
        pr = client.get("/rest/v1/players", params={
            "select": "steam_id,display_name,updated_at",
            "order":  "display_name.asc",
            "limit":  "2000",
        })
        _sb_raise(pr)
        lr = client.get("/rest/v1/leaderboard", params={"select": "steam_id,games_played"})
    lb = {r["steam_id"]: r["games_played"] for r in (_sb_json(lr) if lr.is_success else [])}
    players = pr.json()
    for p in players:
        p["games_count"] = lb.get(p["steam_id"], 0)
    return jsonify(players)


@app.patch("/api/players/<steam_id>")
def api_update_player(steam_id):
    data = request.get_json() or {}
    name = data.get("display_name", "").strip()
    if not name:
        return jsonify({"error": "display_name required"}), 400
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        _sb_raise(client.patch("/rest/v1/players",
                               params={"steam_id": f"eq.{steam_id}"},
                               json={"display_name": name,
                                     "updated_at": datetime.now(timezone.utc).isoformat()}))
    return jsonify({"ok": True})


@app.delete("/api/players/<steam_id>")
def api_delete_player(steam_id):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        _sb_raise(client.delete("/rest/v1/player_game_stats",
                                params={"steam_id": f"eq.{steam_id}"}))
        _sb_raise(client.delete("/rest/v1/players",
                                params={"steam_id": f"eq.{steam_id}"}))
    return jsonify({"ok": True})


# ── API: processed_replays ────────────────────────────────────────────────────

@app.get("/api/processed")
def api_processed():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        resp = client.get("/rest/v1/processed_replays", params={
            "select": "filename,processed_at,status",
            "order":  "processed_at.desc",
            "limit":  "2000",
        })
        _sb_raise(resp)
    return jsonify(resp.json())


@app.delete("/api/processed/errors")
def api_delete_errors():
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        _sb_raise(client.delete("/rest/v1/processed_replays",
                                params={"status": "eq.error"}))
    return jsonify({"ok": True})


@app.delete("/api/processed/<path:filename>")
def api_delete_processed(filename):
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        _sb_raise(client.delete("/rest/v1/processed_replays",
                                params={"filename": f"eq.{filename}"}))
    return jsonify({"ok": True})


@app.post("/api/processed")
def api_mark_processed():
    data     = request.get_json() or {}
    filename = data.get("filename", "").strip()
    status   = data.get("status", "ok")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    url, headers = _sb_client()
    with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
        _sb_raise(client.post(
            "/rest/v1/processed_replays",
            json={"filename": filename,
                  "processed_at": datetime.now(timezone.utc).isoformat(),
                  "status": status},
            headers={**headers, "Prefer": "resolution=merge-duplicates"},
        ))
    return jsonify({"ok": True})


# ── SSE helper ────────────────────────────────────────────────────────────────

def _stream(cmd: list[str], extra_env: dict | None = None) -> Response:
    env = {**os.environ, **(extra_env or {})}

    def generate():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
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
    days_back   = request.args.get("days_back",   "7")
    max_per_run = request.args.get("max_per_run", "20")
    servers     = request.args.get("servers",     "").strip()
    types       = request.args.get("types",       "").strip()
    min_players = request.args.get("min_players", "0").strip()

    date_from   = request.args.get("date_from",   "").strip()
    date_to     = request.args.get("date_to",     "").strip()

    extra: dict[str, str] = {"DAYS_BACK": days_back, "MAX_PER_RUN": max_per_run}
    if servers:                            extra["FILTER_SERVERS"]     = servers
    if types:                              extra["FILTER_TYPES"]       = types
    if min_players and min_players != "0": extra["FILTER_MIN_PLAYERS"] = min_players
    if date_from:                          extra["FILTER_DATE_FROM"]   = date_from
    if date_to:                            extra["FILTER_DATE_TO"]     = date_to

    return _stream([sys.executable, "pipeline.py"], extra)


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


def _fetch_process_gen(archive: str):
    base = os.path.dirname(__file__)
    yield f"data: {json.dumps('── Шаг 1: скачиваем архив ──')}\n\n"
    p1 = subprocess.Popen(
        [sys.executable, "pipeline.py", "--fetch", archive, "--out", "fetched"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=base,
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

    yield f"data: {json.dumps('── Шаг 2: обрабатываем ──')}\n\n"
    p2 = subprocess.Popen(
        [sys.executable, "pipeline.py", "--local", log_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=base,
    )
    for line in iter(p2.stdout.readline, ""):
        yield f"data: {json.dumps(line.rstrip())}\n\n"
    p2.wait()
    yield f"data: {json.dumps({'__done__': True, 'code': p2.returncode})}\n\n"


def _sse(gen):
    return Response(stream_with_context(gen),
                    content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.get("/api/run/fetch-process")
def api_run_fetch_process():
    archive = request.args.get("archive", "")
    if not archive:
        return jsonify({"error": "archive required"}), 400
    return _sse(_fetch_process_gen(archive))


@app.get("/api/run/reprocess")
def api_run_reprocess():
    archive = request.args.get("archive", "")
    if not archive:
        return jsonify({"error": "archive required"}), 400

    def generate():
        yield f"data: {json.dumps('── Сброс статуса ──')}\n\n"
        try:
            url, headers = _sb_client()
            with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
                _sb_raise(client.delete("/rest/v1/processed_replays",
                                        params={"filename": f"eq.{archive}"}))
            yield f"data: {json.dumps('Запись удалена из processed_replays')}\n\n"
        except Exception as e:
            yield f"data: {json.dumps(f'ОШИБКА сброса: {e}')}\n\n"
            yield f"data: {json.dumps({'__done__': True, 'code': 1})}\n\n"
            return
        yield from _fetch_process_gen(archive)

    return _sse(generate())


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return HTML


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>TSGstats Admin</title>
<style>
:root{
  --bg:#0f1117;--surface:#1a1d2e;--surface2:#222536;--surface3:#2a2d40;
  --border:#2a2d3e;--text:#e2e8f0;--muted:#64748b;
  --green:#22c55e;--red:#ef4444;--yellow:#eab308;
  --blue:#3b82f6;--purple:#8b5cf6;--cyan:#06b6d4;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* topbar */
.topbar{background:var(--surface);border-bottom:1px solid var(--border);
        padding:0 16px;height:46px;display:flex;align-items:center;gap:10px;flex-shrink:0}
.topbar h1{font-size:14px;font-weight:700;letter-spacing:-.3px}
.topbar .sp{flex:1}

/* layout */
.layout{display:flex;flex:1;overflow:hidden}
.sidebar{width:160px;background:var(--surface);border-right:1px solid var(--border);
         padding:6px 0;flex-shrink:0;display:flex;flex-direction:column;gap:1px}
.nav-item{display:flex;align-items:center;gap:7px;padding:9px 12px;
          color:var(--muted);font-size:13px;cursor:pointer;
          border-left:2px solid transparent;user-select:none}
.nav-item:hover{background:rgba(255,255,255,.04);color:var(--text)}
.nav-item.active{color:var(--blue);background:rgba(59,130,246,.08);border-left-color:var(--blue)}
.nav-sep{height:1px;background:var(--border);margin:5px 8px}
.content{flex:1;overflow-y:auto;padding:16px}

/* terminal */
.terminal{height:200px;background:#080b10;border-top:1px solid var(--border);
          display:flex;flex-direction:column;flex-shrink:0}
.term-hdr{padding:5px 12px;border-bottom:1px solid var(--border);
          display:flex;align-items:center;gap:8px;font-size:11px;
          color:var(--muted);flex-shrink:0}
.term-dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.term-body{flex:1;overflow-y:auto;padding:6px 12px;font-family:monospace;font-size:12px;line-height:1.65}
.tl{white-space:pre-wrap;word-break:break-all;color:#94a3b8}
.tl.e{color:#fc8181}.tl.ok{color:#68d391}.tl.h{color:#7dd3fc;font-weight:600}

/* stats grid */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px}
.stat-val{font-size:24px;font-weight:700;line-height:1}
.stat-lbl{font-size:10px;color:var(--muted);margin-top:5px;text-transform:uppercase;letter-spacing:.5px}

/* toolbar */
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.search{background:var(--surface2);border:1px solid var(--border);color:var(--text);
        padding:6px 10px;border-radius:6px;font-size:13px;outline:none;min-width:200px}
.search:focus{border-color:var(--blue)}
select.flt{background:var(--surface2);border:1px solid var(--border);color:var(--text);
           padding:6px 10px;border-radius:6px;font-size:13px;outline:none;cursor:pointer}
.sp{flex:1}

/* table */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;padding:7px 9px;color:var(--muted);font-weight:500;
        border-bottom:1px solid var(--border);font-size:11px;
        text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
.tbl th.sortable{cursor:pointer;user-select:none}
.tbl th.sortable:hover{color:var(--text)}
.tbl td{padding:8px 9px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}
.tbl tr:hover td{background:rgba(255,255,255,.02)}
.sort-ic{margin-left:4px;opacity:.35;font-size:10px}
.sort-ic.on{opacity:1;color:var(--blue)}
.mono{font-family:monospace;font-size:11px}

/* badges */
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
.b-ok{background:rgba(34,197,94,.15);color:#22c55e}
.b-err{background:rgba(239,68,68,.15);color:#ef4444}
.b-pend{background:rgba(100,116,139,.15);color:#94a3b8}
.b-off{background:rgba(234,179,8,.1);color:#ca8a04}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:4px;padding:5px 11px;border-radius:6px;
     border:1px solid var(--border);background:transparent;color:var(--text);
     font-size:12px;cursor:pointer;transition:background .1s,border-color .1s;white-space:nowrap}
.btn:hover{background:rgba(255,255,255,.07)}
.btn-primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn-primary:hover{background:#2563eb}
.btn-sm{padding:3px 7px;font-size:11px}
.btn-danger{border-color:rgba(239,68,68,.3);color:#ef4444}
.btn-danger:hover{background:rgba(239,68,68,.1)}
.btn-ghost{border-color:transparent;color:var(--muted)}
.btn-ghost:hover{color:var(--text);background:rgba(255,255,255,.06)}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* forms */
.form-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.form-row label{font-size:13px;color:var(--muted);min-width:130px}
.inp{background:var(--surface2);border:1px solid var(--border);color:var(--text);
     padding:6px 10px;border-radius:6px;font-size:13px;outline:none}
.inp:focus{border-color:var(--blue)}
.inp-sm{width:80px}
.hint{font-size:11px;color:var(--muted)}

/* card */
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;
      padding:16px;margin-bottom:14px}
.card-title{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;
            letter-spacing:.6px;margin-bottom:12px}

/* detail panel */
.detail{position:fixed;top:0;right:0;width:440px;height:100vh;
        background:var(--surface);border-left:1px solid var(--border);
        padding:20px;overflow-y:auto;z-index:200;
        transform:translateX(100%);transition:transform .2s ease}
.detail.open{transform:translateX(0)}
.detail-close{float:right;cursor:pointer;color:var(--muted);font-size:20px;
              background:none;border:none;padding:0}

/* bulk toolbar */
.bulk-bar{display:flex;align-items:center;gap:8px;padding:7px 12px;
          background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.25);
          border-radius:6px;margin-bottom:12px}
.bulk-count{font-size:13px;font-weight:600;color:var(--blue)}

/* inline edit */
.edit-inp{background:var(--surface2);border:1px solid var(--blue);color:var(--text);
          padding:3px 7px;border-radius:4px;font-size:13px;outline:none;width:190px}

/* misc */
.loading{color:var(--muted);font-size:13px;padding:30px 0;text-align:center}
.empty{color:var(--muted);font-size:13px;padding:40px 0;text-align:center}
.player-row{display:flex;align-items:center;gap:7px;padding:7px 0;
            border-bottom:1px solid rgba(255,255,255,.05)}
.kd-b{background:var(--surface2);border-radius:4px;padding:2px 6px;
      font-size:11px;font-family:monospace;color:var(--muted)}
.kd-b b{color:var(--text)}
.db-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.db-s{background:var(--surface);border:1px solid var(--border);border-radius:6px;
      padding:9px 14px;font-size:12px;color:var(--muted);white-space:nowrap}
.db-s b{color:var(--text);font-size:18px;display:block;margin-bottom:1px}
input[type=checkbox]{accent-color:var(--blue);width:14px;height:14px;cursor:pointer}
.off-site{opacity:.55}
</style>
</head>
<body>

<div class="topbar">
  <h1>⚙ TSGstats Admin</h1>
  <div class="sp"></div>
  <button class="btn btn-primary" onclick="quickPipeline()">▶ Pipeline</button>
</div>

<div class="layout">
  <nav class="sidebar">
    <div class="nav-item active" data-tab="archives" onclick="showTab('archives')">📦 Архивы</div>
    <div class="nav-item" data-tab="games" onclick="showTab('games')">🎮 Игры</div>
    <div class="nav-item" data-tab="players" onclick="showTab('players')">👥 Игроки</div>
    <div class="nav-item" data-tab="processed" onclick="showTab('processed')">✅ Обработанные</div>
    <div class="nav-sep"></div>
    <div class="nav-item" data-tab="pipeline" onclick="showTab('pipeline')">⚡ Pipeline</div>
  </nav>
  <main class="content" id="content">
    <div class="loading">Загрузка…</div>
  </main>
</div>

<div class="terminal">
  <div class="term-hdr">
    <div class="term-dot" id="tdot"></div>
    <span id="ttitle">Terminal</span>
    <div style="flex:1"></div>
    <button class="btn btn-ghost btn-sm" onclick="clearTerm()">Очистить</button>
  </div>
  <div class="term-body" id="term"></div>
</div>

<div class="detail" id="detail-panel">
  <button class="detail-close" onclick="closeDetail()">×</button>
  <div id="detail-content"></div>
</div>

<script>
// ════════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════════
let currentTab = '';
let currentSSE = null;
let allArchives = [], allGames = [], allPlayers = [];
const selGames = new Set();

// Sort state per tab: { col, dir }  dir: 1=asc -1=desc
const SS = {
  archives: { col: 'date',         dir: -1 },
  games:    { col: 'played_at',    dir: -1 },
  players:  { col: 'display_name', dir:  1 },
  processed:{ col: 'processed_at', dir: -1 },
};

// ════════════════════════════════════════════════════════
// TERMINAL
// ════════════════════════════════════════════════════════
function tLog(txt, cls='') {
  const el = document.getElementById('term');
  const d  = document.createElement('div');
  d.className = 'tl ' + cls;
  d.textContent = txt;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}
function clearTerm() { document.getElementById('term').innerHTML = ''; }
function termBusy(t) {
  document.getElementById('tdot').style.background = 'var(--yellow)';
  document.getElementById('ttitle').textContent = t || 'Running…';
}
function termDone(ok) {
  document.getElementById('tdot').style.background = ok ? 'var(--green)' : 'var(--red)';
  document.getElementById('ttitle').textContent = ok ? 'Done ✓' : 'Failed ✗';
}

// ════════════════════════════════════════════════════════
// SSE
// ════════════════════════════════════════════════════════
function streamSSE(url, title, onDone) {
  if (currentSSE) currentSSE.close();
  clearTerm(); termBusy(title);
  tLog('▶ ' + title, 'h');
  const es = new EventSource(url);
  currentSSE = es;
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (typeof d === 'string') {
      const c = (d.includes('ОШИБКА')||d.includes('Error')||d.includes('✗')) ? 'e'
              : (d.includes('Готово')||d.includes('успешно')||d.includes('✓'))  ? 'ok'
              : (d.startsWith('──')||d.startsWith('==='))                       ? 'h' : '';
      tLog(d, c);
    } else if (d.__done__) {
      const ok = d.code === 0;
      tLog(ok ? '✓ Завершено успешно' : '✗ Ошибка (код '+d.code+')', ok?'ok':'e');
      termDone(ok); es.close();
      if (onDone) onDone(ok);
    }
  };
  es.onerror = () => { tLog('✗ Соединение прервано','e'); termDone(false); es.close(); };
}

// ════════════════════════════════════════════════════════
// SORT UTILITIES
// ════════════════════════════════════════════════════════
function sortBy(tab, col) {
  const s = SS[tab];
  if (s.col === col) s.dir = -s.dir;
  else { s.col = col; s.dir = 1; }
}
function sorted(arr, tab) {
  const { col, dir } = SS[tab] || {};
  if (!col) return arr;
  return [...arr].sort((a, b) => {
    let va = a[col] ?? '', vb = b[col] ?? '';
    if (typeof va === 'number') return (va - vb) * dir;
    if (va < vb) return -dir; if (va > vb) return dir; return 0;
  });
}
function si(tab, col) {  // sort icon HTML
  const s = SS[tab];
  const on = s && s.col === col;
  return `<span class="sort-ic ${on?'on':''}">${on ? (s.dir===1?'↑':'↓') : '⇅'}</span>`;
}
function th(tab, col, label, renderFn, extra='') {
  return `<th class="sortable" onclick="${renderFn}('${col}')" ${extra}>${label}${si(tab,col)}</th>`;
}

// ════════════════════════════════════════════════════════
// TABS
// ════════════════════════════════════════════════════════
function showTab(tab) {
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.remove('active'));
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
  currentTab = tab; closeDetail();
  if      (tab==='archives') loadArchives();
  else if (tab==='games')    loadGames();
  else if (tab==='players')  loadPlayers();
  else if (tab==='processed')loadProcessed();
  else if (tab==='pipeline') renderPipeline();
}

// ════════════════════════════════════════════════════════
// ARCHIVES TAB
// ════════════════════════════════════════════════════════
async function loadArchives() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем архивы…</div>';
  const r = await fetch('/api/archives');
  const d = await r.json();
  if (!r.ok) { showErr(d); return; }
  allArchives = d;
  renderArchives();
  loadDbStats();
}

function sortArchives(col) { sortBy('archives', col); renderArchives(); }

function renderArchives() {
  const search = (document.getElementById('as')?.value || '').toLowerCase();
  const sf     = document.getElementById('af')?.value  || 'all';

  let list = allArchives.filter(a =>
    (sf==='all' || a.status===sf) &&
    (!search || a.filename.toLowerCase().includes(search) ||
     (a.server||'').toLowerCase().includes(search) ||
     (a.mission_type||'').toLowerCase().includes(search))
  );
  list = sorted(list, 'archives');

  const cnt  = s => allArchives.filter(a=>a.status===s).length;
  const pend = allArchives.filter(a=>a.status==='pending').length;
  const offSite = allArchives.filter(a=>!a.on_site).length;

  document.getElementById('content').innerHTML = `
    <div id="db-bar" class="db-bar"></div>
    <div class="stats-grid">
      <div class="stat"><div class="stat-val">${allArchives.length}</div><div class="stat-lbl">Всего архивов</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--green)">${cnt('ok')}</div><div class="stat-lbl">Обработано</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--yellow)">${pend}</div><div class="stat-lbl">Ожидает</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--red)">${cnt('error')}</div><div class="stat-lbl">Ошибки</div></div>
    </div>
    <div class="toolbar">
      <input class="search" id="as" placeholder="Поиск по имени, серверу, типу…" oninput="renderArchives()" value="${esc(search)}">
      <select class="flt" id="af" onchange="renderArchives()">
        <option value="all"${sf==='all'?' selected':''}>Все статусы</option>
        <option value="pending"${sf==='pending'?' selected':''}>Ожидает</option>
        <option value="ok"${sf==='ok'?' selected':''}>Обработано</option>
        <option value="error"${sf==='error'?' selected':''}>Ошибка</option>
      </select>
      <span class="hint">${list.length} из ${allArchives.length}${offSite?' · '+offSite+' только в БД':''}</span>
      <div class="sp"></div>
      <button class="btn" onclick="loadArchives()">↺ Обновить</button>
    </div>
    ${list.length===0 ? '<div class="empty">Нет архивов по выбранным фильтрам</div>' : `
    <table class="tbl">
      <thead><tr>
        ${th('archives','date','Дата','sortArchives')}
        ${th('archives','mission_name','Миссия','sortArchives')}
        ${th('archives','server','Сервер','sortArchives')}
        ${th('archives','mission_type','Тип','sortArchives')}
        ${th('archives','player_count','Слоты','sortArchives','style="text-align:center"')}
        ${th('archives','map','Карта','sortArchives')}
        ${th('archives','status','Статус','sortArchives')}
        <th style="width:180px"></th>
      </tr></thead>
      <tbody>
        ${list.map(a => {
          const date  = a.date ? new Date(a.date).toLocaleString('ru',{dateStyle:'short',timeStyle:'short'}) : '—';
          const badge = {ok:'b-ok',error:'b-err',pending:'b-pend'}[a.status]||'b-pend';
          const fn    = esc(a.filename);
          const gone  = !a.on_site;
          const mname = esc(a.mission_name || a.filename);
          const actions = [];
          if (!gone) {
            if (a.status==='pending'||a.status==='error')
              actions.push(`<button class="btn btn-sm btn-primary" onclick='fetchAndProcess("${fn}")'>▶ Обработать</button>`);
            if (a.status==='ok')
              actions.push(`<button class="btn btn-sm" onclick='reprocess("${fn}")'>↺ Reprocess</button>`);
            actions.push(`<button class="btn btn-sm btn-ghost" onclick='fetchOnly("${fn}")' title="Только скачать">⬇</button>`);
          }
          if (a.status==='pending')
            actions.push(`<button class="btn btn-sm" onclick='markOk("${fn}")' title="Отметить OK вручную">✓ OK</button>`);
          if (a.status!=='pending')
            actions.push(`<button class="btn btn-sm btn-danger" onclick='resetStatus("${fn}")' title="Сбросить статус">✕</button>`);
          return `<tr class="${gone?'off-site':''}" title="${fn}">
            <td style="white-space:nowrap">${date}</td>
            <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${mname}</td>
            <td style="white-space:nowrap">${a.server||'—'}</td>
            <td style="color:var(--muted);font-size:11px;white-space:nowrap">${a.mission_type||'—'}</td>
            <td style="text-align:center;color:var(--muted)">${a.player_count||'—'}</td>
            <td style="color:var(--muted);font-size:11px">${a.map||'—'}</td>
            <td>
              <span class="badge ${badge}">${a.status}</span>
              ${gone ? '<span class="badge b-off" style="margin-left:3px" title="Архив удалён с сайта">off-site</span>' : ''}
            </td>
            <td style="white-space:nowrap;display:flex;gap:3px;flex-wrap:wrap">${actions.join('')}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`}
  `;
  loadDbStats();
}

async function loadDbStats() {
  const r = await fetch('/api/stats');
  if (!r.ok) return;
  const s = await r.json();
  const el = document.getElementById('db-bar');
  if (!el) return;
  el.innerHTML = `
    <div class="db-s"><b>${s.games}</b>Игр в БД</div>
    <div class="db-s"><b>${s.players}</b>Игроков</div>
    <div class="db-s" style="color:var(--green)"><b>${s.processed_ok}</b>OK</div>
    ${s.processed_err>0?`<div class="db-s" style="color:var(--red)"><b>${s.processed_err}</b>Ошибок</div>`:''}
  `;
}

function fetchOnly(a)     { streamSSE('/api/run/fetch?archive='+enc(a), 'Fetch: '+shortName(a)); }
function fetchAndProcess(a) {
  streamSSE('/api/run/fetch-process?archive='+enc(a), 'Process: '+shortName(a),
    ok => { if(ok) loadArchives(); });
}
function reprocess(a) {
  if (!confirm('Переобработать? Данные игры будут перезаписаны.')) return;
  streamSSE('/api/run/reprocess?archive='+enc(a), 'Reprocess: '+shortName(a),
    ok => { if(ok) loadArchives(); });
}
async function resetStatus(fn) {
  await fetch('/api/processed/'+enc(fn), {method:'DELETE'});
  loadArchives();
}
async function markOk(fn) {
  await fetch('/api/processed', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({filename:fn, status:'ok'}),
  });
  loadArchives();
}

// ════════════════════════════════════════════════════════
// GAMES TAB
// ════════════════════════════════════════════════════════
async function loadGames() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем игры…</div>';
  const r = await fetch('/api/games');
  const d = await r.json();
  if (!r.ok) { showErr(d); return; }
  allGames = d;
  selGames.clear();
  renderGames();
}

function sortGames(col) { sortBy('games', col); renderGames(); }

function renderGames() {
  const search = (document.getElementById('gs')?.value || '').toLowerCase();
  let list = allGames.filter(g =>
    !search || g.mission.toLowerCase().includes(search) ||
    g.map.toLowerCase().includes(search) ||
    (g.server||'').toLowerCase().includes(search)
  );
  list = sorted(list, 'games');

  const selCount = selGames.size;
  const visIds   = list.map(g => g.id);

  document.getElementById('content').innerHTML = `
    <div class="toolbar">
      <input class="search" id="gs" placeholder="Поиск по миссии, карте, серверу…"
             oninput="renderGames()" value="${esc(search)}">
      <div class="sp"></div>
      <span class="hint">${list.length} из ${allGames.length} игр</span>
      <button class="btn btn-danger" onclick="deleteAllGames()">✕ Удалить все игры</button>
      <button class="btn" onclick="loadGames()">↺</button>
    </div>
    ${selCount > 0 ? `
    <div class="bulk-bar">
      <span class="bulk-count">${selCount} выбрано</span>
      <button class="btn btn-sm btn-danger" onclick="deleteSelected()">✕ Удалить выбранные</button>
      <button class="btn btn-sm btn-ghost" onclick="clearSel()">Снять выделение</button>
    </div>` : ''}
    ${list.length===0 ? '<div class="empty">Игр нет</div>' : `
    <table class="tbl">
      <thead><tr>
        <th style="width:30px;padding:7px 4px 7px 9px">
          <input type="checkbox" id="chk-all"
                 onchange="toggleAllGames(this.checked,${JSON.stringify(visIds).replace(/"/g,'&quot;')})"
                 ${selCount>0 && list.every(g=>selGames.has(g.id)) ? 'checked' : ''}>
        </th>
        ${th('games','played_at','Дата','sortGames')}
        ${th('games','server','Сервер','sortGames')}
        ${th('games','mission','Миссия','sortGames')}
        ${th('games','map','Карта','sortGames')}
        ${th('games','player_count','👥','sortGames','style="text-align:center"')}
        ${th('games','duration_sec','Длит.','sortGames')}
        <th style="width:60px"></th>
      </tr></thead>
      <tbody>
        ${list.map(g => {
          const date = new Date(g.played_at).toLocaleString('ru',{dateStyle:'short',timeStyle:'short'});
          const dur  = Math.round(g.duration_sec/60)+'м';
          const gid  = esc(g.id);
          const sel  = selGames.has(g.id);
          return `<tr style="${sel?'background:rgba(59,130,246,.07)':''}">
            <td style="padding:8px 4px 8px 9px">
              <input type="checkbox" ${sel?'checked':''} onchange="toggleGame('${gid}',this.checked)">
            </td>
            <td style="white-space:nowrap;cursor:pointer" onclick="openGame('${gid}')">${date}</td>
            <td style="cursor:pointer" onclick="openGame('${gid}')">${g.server||'—'}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer"
                onclick="openGame('${gid}')" title="${esc(g.mission)}">${esc(g.mission)}</td>
            <td style="cursor:pointer" onclick="openGame('${gid}')">${g.map}</td>
            <td style="text-align:center;cursor:pointer" onclick="openGame('${gid}')">${g.player_count}</td>
            <td style="color:var(--muted);cursor:pointer" onclick="openGame('${gid}')">${dur}</td>
            <td>
              <button class="btn btn-sm btn-danger" onclick='deleteGame("${gid}")' title="Удалить">✕</button>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`}
  `;
}

function toggleGame(id, checked) {
  if (checked) selGames.add(id); else selGames.delete(id);
  renderGames();
}
function toggleAllGames(checked, ids) {
  if (checked) ids.forEach(id => selGames.add(id));
  else         ids.forEach(id => selGames.delete(id));
  renderGames();
}
function clearSel() { selGames.clear(); renderGames(); }

async function deleteSelected() {
  const ids = [...selGames];
  if (!ids.length) return;
  if (!confirm(`Удалить ${ids.length} игр и всю их статистику?`)) return;
  const r = await fetch('/api/games/bulk-action', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'delete', ids}),
  });
  const d = await r.json();
  if (!r.ok) { tLog('Ошибка: '+(d.error||r.status),'e'); return; }
  tLog(`✓ Удалено ${d.deleted} игр`,'ok');
  selGames.clear();
  loadGames();
}

async function deleteAllGames() {
  const total = allGames.length;
  if (!confirm(`Удалить ВСЕ ${total} игр и всю статистику? Это необратимо!`)) return;
  const r = await fetch('/api/games/bulk-action', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'delete', all:true}),
  });
  const d = await r.json();
  if (!r.ok) { tLog('Ошибка: '+(d.error||r.status),'e'); return; }
  tLog(`✓ Удалено ${d.deleted} игр`,'ok');
  selGames.clear();
  loadGames();
}

async function openGame(gameId) {
  const panel = document.getElementById('detail-panel');
  const cont  = document.getElementById('detail-content');
  panel.classList.add('open');
  cont.innerHTML = '<div class="loading">Загружаем…</div>';

  const r = await fetch('/api/games/'+enc(gameId));
  const d = await r.json();
  if (!d.game) { cont.innerHTML = '<div class="empty">Не найдено</div>'; return; }

  const g    = d.game;
  const date = new Date(g.played_at).toLocaleString('ru');
  const dur  = Math.round(g.duration_sec/60)+' мин';
  const gid  = esc(g.id);

  cont.innerHTML = `
    <h2 style="font-size:15px;font-weight:700;margin-bottom:5px">${esc(g.mission)}</h2>
    <p style="color:var(--muted);font-size:12px;margin-bottom:3px">${g.map}</p>
    <p style="color:var(--muted);font-size:12px;margin-bottom:14px">${g.server||'—'} · ${date} · ${dur} · ${g.player_count} игроков</p>
    <div style="display:flex;gap:6px;margin-bottom:18px">
      <button class="btn btn-sm btn-danger" onclick='deleteGame("${gid}")'>✕ Удалить игру</button>
    </div>
    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:8px">
      Статистика (${d.stats.length})
    </div>
    ${d.stats.map(s => {
      const nm = s.players?.display_name || s.steam_id;
      return `<div class="player-row">
        <div style="flex:1;font-size:13px">${esc(nm)}</div>
        <span class="kd-b">K<b style="color:var(--green)">${s.kills}</b></span>
        <span class="kd-b">D<b style="color:var(--red)">${s.deaths}</b></span>
        ${s.teamkills>0?`<span class="kd-b">TK<b style="color:var(--yellow)">${s.teamkills}</b></span>`:''}
      </div>`;
    }).join('')}
  `;
}

async function deleteGame(gameId) {
  if (!confirm('Удалить игру и всю статистику?')) return;
  const r = await fetch('/api/games/'+enc(gameId), {method:'DELETE'});
  if (!r.ok) { const d=await r.json(); alert('Ошибка: '+(d.error||r.status)); return; }
  closeDetail();
  selGames.delete(gameId);
  loadGames();
}

// ════════════════════════════════════════════════════════
// PLAYERS TAB
// ════════════════════════════════════════════════════════
async function loadPlayers() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем игроков…</div>';
  const r = await fetch('/api/players');
  const d = await r.json();
  if (!r.ok) { showErr(d); return; }
  allPlayers = d;
  renderPlayers();
}

function sortPlayers(col) { sortBy('players', col); renderPlayers(); }

function renderPlayers() {
  const search = (document.getElementById('ps')?.value || '').toLowerCase();
  let list = allPlayers.filter(p =>
    !search || p.display_name.toLowerCase().includes(search) ||
    p.steam_id.toLowerCase().includes(search)
  );
  list = sorted(list, 'players');

  document.getElementById('content').innerHTML = `
    <div class="toolbar">
      <input class="search" id="ps" placeholder="Поиск по имени или Steam ID…"
             oninput="renderPlayers()" value="${esc(search)}">
      <div class="sp"></div>
      <span class="hint">${list.length} из ${allPlayers.length}</span>
      <button class="btn" onclick="loadPlayers()">↺</button>
    </div>
    ${list.length===0 ? '<div class="empty">Нет игроков</div>' : `
    <table class="tbl">
      <thead><tr>
        ${th('players','display_name','Имя','sortPlayers')}
        ${th('players','steam_id','Steam ID','sortPlayers')}
        ${th('players','games_count','Игр','sortPlayers','style="text-align:center"')}
        <th style="width:90px"></th>
      </tr></thead>
      <tbody>
        ${list.map(p => {
          const sid = enc(p.steam_id);
          const nm  = esc(p.display_name);
          return `<tr id="pr-${sid}">
            <td id="pn-${sid}">${nm}</td>
            <td class="mono">${p.steam_id}</td>
            <td style="text-align:center;color:var(--muted)">${p.games_count??0}</td>
            <td style="white-space:nowrap;display:flex;gap:3px">
              <button class="btn btn-sm" onclick='editPlayer("${sid}","${nm}")'>✎</button>
              <button class="btn btn-sm btn-danger" onclick='deletePlayer("${sid}")'>✕</button>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`}
  `;
}

function editPlayer(sid, cur) {
  const cell = document.getElementById('pn-'+sid);
  if (!cell) return;
  cell.innerHTML = `
    <input class="edit-inp" id="ei-${sid}" value="${cur}"
           onkeydown="if(event.key==='Enter')savePlayer('${sid}');if(event.key==='Escape')loadPlayers()">
    <button class="btn btn-sm btn-primary" onclick="savePlayer('${sid}')">✓</button>
    <button class="btn btn-sm" onclick="loadPlayers()">✕</button>
  `;
  document.getElementById('ei-'+sid)?.focus();
}

async function savePlayer(sid) {
  const inp  = document.getElementById('ei-'+sid);
  if (!inp) return;
  const name = inp.value.trim();
  if (!name) return;
  const r = await fetch('/api/players/'+enc(sid), {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({display_name: name}),
  });
  const d = await r.json();
  if (!r.ok) { tLog('Ошибка: '+(d.error||r.status),'e'); return; }
  loadPlayers();
}

async function deletePlayer(sid) {
  if (!confirm('Удалить игрока и ВСЮ его статистику?')) return;
  const r = await fetch('/api/players/'+enc(sid), {method:'DELETE'});
  const d = await r.json();
  if (!r.ok) { alert('Ошибка: '+(d.error||r.status)); return; }
  loadPlayers();
}

// ════════════════════════════════════════════════════════
// PROCESSED TAB
// ════════════════════════════════════════════════════════
async function loadProcessed() {
  document.getElementById('content').innerHTML = '<div class="loading">Загружаем…</div>';
  const r = await fetch('/api/processed');
  const d = await r.json();
  if (!r.ok) { showErr(d); return; }
  window._processed = d;
  renderProcessed();
}

function sortProcessed(col) { sortBy('processed', col); renderProcessed(); }

function renderProcessed() {
  const data   = window._processed || [];
  const search = (document.getElementById('prs')?.value || '').toLowerCase();
  const sf     = document.getElementById('prf')?.value || 'all';
  let list     = data.filter(p =>
    (sf==='all' || p.status===sf) &&
    (!search || p.filename.toLowerCase().includes(search))
  );
  list = sorted(list, 'processed');

  const ok  = data.filter(p=>p.status==='ok').length;
  const err = data.filter(p=>p.status==='error').length;

  document.getElementById('content').innerHTML = `
    <div class="toolbar">
      <input class="search" id="prs" placeholder="Поиск по имени архива…"
             oninput="renderProcessed()" value="${esc(search)}">
      <select class="flt" id="prf" onchange="renderProcessed()">
        <option value="all"${sf==='all'?' selected':''}>Все</option>
        <option value="ok"${sf==='ok'?' selected':''}>OK</option>
        <option value="error"${sf==='error'?' selected':''}>Ошибка</option>
      </select>
      <span class="hint">${list.length} из ${data.length} · ${ok} ok · ${err} ошибок</span>
      <div class="sp"></div>
      <button class="btn btn-sm btn-danger" onclick="clearErrors()">✕ Удалить все ошибки</button>
      <button class="btn" onclick="loadProcessed()">↺</button>
    </div>
    ${list.length===0 ? '<div class="empty">Нет записей</div>' : `
    <table class="tbl">
      <thead><tr>
        ${th('processed','filename','Архив','sortProcessed')}
        ${th('processed','processed_at','Обработан','sortProcessed')}
        ${th('processed','status','Статус','sortProcessed')}
        <th style="width:50px"></th>
      </tr></thead>
      <tbody>
        ${list.map(p => {
          const date  = new Date(p.processed_at).toLocaleString('ru',{dateStyle:'short',timeStyle:'short'});
          const badge = {ok:'b-ok',error:'b-err'}[p.status]||'b-pend';
          const fn    = esc(p.filename);
          return `<tr>
            <td class="mono" style="max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                title="${fn}">${p.filename}</td>
            <td style="white-space:nowrap">${date}</td>
            <td><span class="badge ${badge}">${p.status}</span></td>
            <td>
              <button class="btn btn-sm btn-danger" onclick='resetStatus("${fn}")' title="Удалить запись">✕</button>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`}
  `;
}

async function clearErrors() {
  if (!confirm('Удалить все записи со статусом error?')) return;
  await fetch('/api/processed/errors', {method:'DELETE'});
  loadProcessed();
}

// ════════════════════════════════════════════════════════
// PIPELINE TAB
// ════════════════════════════════════════════════════════
function renderPipeline() {
  document.getElementById('content').innerHTML = `
    <div class="card">
      <div class="card-title">Запуск Pipeline — скачать + обработать новые</div>

      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:8px">Диапазон дат</div>
      <div class="form-row"><label>Дней назад</label>
        <input class="inp inp-sm" type="number" id="p-days" value="7" min="1" max="365"
               oninput="syncDateMode(0)" title="Игнорируется если задана дата «от»">
        <span class="hint" id="p-days-hint">заменяется датой «от» если задана</span></div>
      <div class="form-row"><label>Дата от</label>
        <input class="inp" type="date" id="p-dfrom" oninput="syncDateMode(1)">
        <span class="hint">включительно · заменяет «дней назад»</span></div>
      <div class="form-row"><label>Дата до</label>
        <input class="inp" type="date" id="p-dto" oninput="syncDateMode(1)">
        <span class="hint">включительно</span></div>

      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin:12px 0 8px">Фильтры</div>
      <div class="form-row"><label>Макс. архивов</label>
        <input class="inp inp-sm" type="number" id="p-max" value="20" min="1" max="500"></div>
      <div class="form-row"><label>Серверы</label>
        <input class="inp" type="text" id="p-srv" placeholder="T1,T2 — пусто = все" style="width:210px">
        <span class="hint">через запятую</span></div>
      <div class="form-row"><label>Типы миссий</label>
        <input class="inp" type="text" id="p-typ" placeholder="mTSG,TSG — пусто = все" style="width:210px">
        <span class="hint">через запятую · регистр не важен</span></div>
      <div class="form-row"><label>Мин. игроков</label>
        <input class="inp inp-sm" type="number" id="p-minp" value="0" min="0">
        <span class="hint">реальные (из лога) · 0 = без ограничения</span></div>

      <button class="btn btn-primary" onclick="startPipeline()">▶ Запустить</button>
    </div>

    <div class="card">
      <div class="card-title">Обработать локальный log.txt</div>
      <div class="form-row"><label>Путь к файлу</label>
        <input class="inp" type="text" id="p-lpath" placeholder="D:/Downloads/.../log.txt" style="flex:1"></div>
      <div class="form-row"><label>Сервер</label>
        <select class="inp" id="p-lsrv" style="width:100px">
          <option value="">Авто</option>
          <option>T1</option><option>T2</option><option>T3</option><option>T4</option>
        </select></div>
      <button class="btn btn-primary" onclick="processLocal()">▶ Обработать</button>
    </div>

    <div class="card">
      <div class="card-title">Скачать архив без обработки</div>
      <div class="form-row"><label>Имя архива</label>
        <input class="inp" type="text" id="p-fname"
               placeholder="T1.2026-05-20-20-27-54.mTSG%4016_....pbo.7z" style="flex:1"></div>
      <button class="btn" onclick="fetchByName()">⬇ Скачать</button>
    </div>
  `;
}

function syncDateMode(fromDate) {
  // Dim "days back" when explicit date_from is set
  const dfrom = document.getElementById('p-dfrom')?.value;
  const inp   = document.getElementById('p-days');
  const hint  = document.getElementById('p-days-hint');
  if (!inp) return;
  const locked = !!dfrom;
  inp.disabled      = locked;
  inp.style.opacity = locked ? '0.4' : '1';
  if (hint) hint.textContent = locked
    ? 'отключено — используется дата «от»'
    : 'заменяется датой «от» если задана';
}

function startPipeline() {
  const days  = document.getElementById('p-days').value;
  const max   = document.getElementById('p-max').value;
  const srv   = document.getElementById('p-srv').value.trim();
  const typ   = document.getElementById('p-typ').value.trim();
  const minp  = document.getElementById('p-minp').value;
  const dfrom = document.getElementById('p-dfrom').value;
  const dto   = document.getElementById('p-dto').value;
  let url = `/api/run/pipeline?days_back=${days}&max_per_run=${max}`;
  if (srv)                url += '&servers='+enc(srv);
  if (typ)                url += '&types='+enc(typ);
  if (minp && minp!=='0') url += '&min_players='+minp;
  if (dfrom)              url += '&date_from='+dfrom;
  if (dto)                url += '&date_to='+dto;
  streamSSE(url, 'Pipeline', ok => { if(ok && currentTab==='archives') loadArchives(); });
}

function processLocal() {
  const path = document.getElementById('p-lpath').value.trim();
  const srv  = document.getElementById('p-lsrv').value;
  if (!path) { alert('Укажите путь к файлу'); return; }
  let url = '/api/run/local?path='+enc(path);
  if (srv) url += '&server='+srv;
  streamSSE(url, 'Local: '+path.split(/[\\\\/]/).pop());
}

function fetchByName() {
  const name = document.getElementById('p-fname').value.trim();
  if (!name) { alert('Укажите имя архива'); return; }
  fetchOnly(name);
}

// ════════════════════════════════════════════════════════
// GLOBAL
// ════════════════════════════════════════════════════════
function quickPipeline() {
  streamSSE('/api/run/pipeline?days_back=7&max_per_run=20',
    'Pipeline', ok => { if(ok && currentTab==='archives') loadArchives(); });
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
}

function showErr(d) {
  document.getElementById('content').innerHTML =
    `<div class="empty" style="color:var(--red)">Ошибка: ${esc(d.error||JSON.stringify(d))}</div>`;
}

// ════════════════════════════════════════════════════════
// UTILS
// ════════════════════════════════════════════════════════
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
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
