"""
TSGstats — Stats Calculator
Читает: attributed_events.json, entity_map.json, meta.json
Пишет: game_stats.json
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from entity_resolver import load_entity_map
from models import GameStats, PlayerStats


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def calculate(input_dir: str, output_dir: str) -> None:
    print("\nStats Calculator...")

    meta       = _load(os.path.join(input_dir, "meta.json"))
    entity_map = load_entity_map(os.path.join(input_dir, "entity_map.json"))
    attributed = _load(os.path.join(input_dir, "attributed_events.json"))

    min_confidence: float = 0.5  # убийства ниже этого порога не считаются

    # ── Инициализируем stats для каждого известного игрока ────────────────────
    stats: dict[str, PlayerStats] = {
        steam_id: PlayerStats(steam_id=steam_id, display_name=name)
        for steam_id, name in entity_map.players.items()
    }

    # ── Считаем по убийствам ──────────────────────────────────────────────────
    for kill in attributed["kills"]:
        killer = kill["killer_steam_id"]
        victim = kill["victim_steam_id"]
        confidence = kill["attribution_confidence"]

        # Смерть засчитывается всегда (жертва всегда известна или нет)
        if victim and victim in stats:
            stats[victim].deaths += 1

        # Убийства — только выше порога confidence
        if killer and killer in stats and confidence >= min_confidence:
            if kill["is_suicide"]:
                stats[killer].suicides += 1
            elif kill["is_teamkill"]:
                stats[killer].teamkills += 1
            else:
                stats[killer].kills += 1

    # ── Формируем результат ───────────────────────────────────────────────────
    game_stats = GameStats(
        game_id     = meta["timestamp"],
        server      = meta["server"],
        map         = meta["map"],
        mission     = meta["mission"],
        duration_sec= meta["duration_sec"],
        players     = sorted(stats.values(), key=lambda p: p.kills, reverse=True),
    )

    # ── Запись ────────────────────────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "game_stats.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(asdict(game_stats), f, ensure_ascii=False, indent=2)
    print(f"  > {out_path}")

    # ── Итог ──────────────────────────────────────────────────────────────────
    total_kills = sum(p.kills for p in game_stats.players)
    top5 = game_stats.players[:5]

    print(f"\nИтого: {len(game_stats.players)} игроков, {total_kills} убийств")
    print(f"\nТоп 5:")
    for p in top5:
        print(f"  {p.display_name:<25} K:{p.kills}  D:{p.deaths}  TK:{p.teamkills}  S:{p.suicides}")


if __name__ == "__main__":
    input_dir  = sys.argv[1] if len(sys.argv) > 1 else "output"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    calculate(input_dir, output_dir)
