"""
TSGstats — Supabase Writer
Читает: game_stats.json
Пишет: games, players, player_game_stats

Использует Supabase REST API (PostgREST) напрямую через httpx.
Зависимости: только httpx (стандартная).

Переменные окружения:
    SUPABASE_URL         -- https://<project>.supabase.co
    SUPABASE_SERVICE_KEY -- service_role key (не anon!)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import httpx


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _get_config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise EnvironmentError(
            "Нужны переменные окружения: SUPABASE_URL, SUPABASE_SERVICE_KEY"
        )
    return url, key


def _upsert(client: httpx.Client, table: str, rows: list[dict]) -> None:
    """POST /rest/v1/<table> с Prefer: resolution=merge-duplicates."""
    resp = client.post(
        f"/rest/v1/{table}",
        json=rows,
        headers={"Prefer": "resolution=merge-duplicates"},
    )
    resp.raise_for_status()


def write(input_dir: str) -> None:
    print("\nSupabase Writer...")

    game_stats = _load(os.path.join(input_dir, "game_stats.json"))
    url, key = _get_config()

    game_id = game_stats["game_id"]
    players = game_stats["players"]

    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }

    with httpx.Client(base_url=url, headers=headers) as client:

        # -- 1. Upsert game ---------------------------------------------------
        _upsert(client, "games", [{
            "id":           game_id,
            "server":       game_stats["server"],
            "map":          game_stats["map"],
            "mission":      game_stats["mission"],
            "duration_sec": game_stats["duration_sec"],
            "played_at":    game_id,        # ISO строка, Postgres парсит сам
            "player_count": len(players),
        }])
        print(f"  game:              {game_id}")

        # -- 2. Upsert players ------------------------------------------------
        now = datetime.now(timezone.utc).isoformat()
        player_rows = [
            {
                "steam_id":     p["steam_id"],
                "display_name": p["display_name"],
                "updated_at":   now,
            }
            for p in players
        ]
        _upsert(client, "players", player_rows)
        print(f"  players:           {len(player_rows)}")

        # -- 3. Upsert player_game_stats --------------------------------------
        stats_rows = [
            {
                "game_id":   game_id,
                "steam_id":  p["steam_id"],
                "kills":     p["kills"],
                "deaths":    p["deaths"],
                "teamkills": p["teamkills"],
                "suicides":  p["suicides"],
                "extra":     p.get("extra", {}),
            }
            for p in players
        ]
        _upsert(client, "player_game_stats", stats_rows)
        print(f"  player_game_stats: {len(stats_rows)}")

    # -- Итог -----------------------------------------------------------------
    total_kills = sum(p["kills"] for p in players)
    print(f"\nГотово: {len(players)} игроков, {total_kills} убийств -> Supabase")


if __name__ == "__main__":
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "output"
    write(input_dir)
