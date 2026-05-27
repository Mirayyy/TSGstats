"""
TSGstats — Entity Resolver
Читает: meta.json, entities.json, events.json
Пишет: entity_map.json

Строит сессии игроков: кто, на каком слоте, с какого по какое время.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict

from models import EntityMap, Identity, PlayerSession


# ─── Загрузка ─────────────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── Построение сессий ────────────────────────────────────────────────────────

def build_sessions(
    identities: list[dict],
    units: list[dict],
    kills: list[dict],
    duration_sec: float,
) -> EntityMap:

    # Справочник юнитов: entity_id → {side, group, slot}
    unit_info: dict[int, dict] = {
        u["entity_id"]: {"side": u["side"], "group": u["group"], "slot": u["slot"]}
        for u in units
    }

    # Сортируем идентификации по времени
    sorted_idents = sorted(identities, key=lambda x: x["time_sec"])

    sessions: list[PlayerSession] = []
    no_steam_id: list[int] = []

    # Активные сессии: entity_id → открытая сессия
    active_by_entity: dict[int, PlayerSession] = {}
    # Активные сессии: steam_id → открытая сессия
    active_by_steam: dict[str, PlayerSession] = {}

    for ident in sorted_idents:
        entity_id: int = ident["entity_id"]
        steam_id: str | None = ident["steam_id"]
        new_name: str = ident["new_name"]
        time_sec: float = ident["time_sec"]

        # ── Нет steam_id — пропускаем, фиксируем для ручной правки ───────────
        if not steam_id:
            if entity_id not in no_steam_id:
                no_steam_id.append(entity_id)
            continue

        existing_entity = active_by_entity.get(entity_id)
        existing_steam  = active_by_steam.get(steam_id)

        # ── Тот же игрок на том же слоте — реконнект или смена ника ──────────
        if existing_entity and existing_entity.steam_id == steam_id:
            existing_entity.display_name = new_name   # берём последний ник
            continue

        # ── Другой игрок занял слот — закрываем старую сессию ────────────────
        if existing_entity:
            existing_entity.end_time_sec = time_sec
            existing_entity.end_reason   = "slot_change"
            old_steam = existing_entity.steam_id
            if active_by_steam.get(old_steam) is existing_entity:
                del active_by_steam[old_steam]
            del active_by_entity[entity_id]

        # ── Тот же игрок переходит на другой слот — закрываем старую сессию ──
        if existing_steam and existing_steam is not existing_entity:
            existing_steam.end_time_sec = time_sec
            existing_steam.end_reason   = "slot_change"
            old_entity = existing_steam.entity_id
            if active_by_entity.get(old_entity) is existing_steam:
                del active_by_entity[old_entity]
            del active_by_steam[steam_id]

        # ── Создаём новую сессию ──────────────────────────────────────────────
        info = unit_info.get(entity_id, {})
        session = PlayerSession(
            steam_id      = steam_id,
            entity_id     = entity_id,
            display_name  = new_name,
            side          = info.get("side"),
            group         = info.get("group"),
            slot          = info.get("slot"),
            start_time_sec= time_sec,
            end_time_sec  = None,
            end_reason    = None,
        )
        sessions.append(session)
        active_by_entity[entity_id] = session
        active_by_steam[steam_id]   = session

    # ── Проход 2: определяем смерти ───────────────────────────────────────────
    # Убийства сгруппированные по жертве, отсортированные по времени
    kills_by_victim: dict[int, list[float]] = defaultdict(list)
    for kill in kills:
        kills_by_victim[kill["victim_id"]].append(kill["time_sec"])
    for times in kills_by_victim.values():
        times.sort()

    for session in sessions:
        if session.end_time_sec is not None:
            continue  # уже закрыта через slot_change

        # Ищем первую смерть после начала сессии
        for kill_time in kills_by_victim.get(session.entity_id, []):
            if kill_time >= session.start_time_sec:
                session.end_time_sec = kill_time
                session.end_reason   = "death"
                break

        # Если смерти нет — игра закончилась
        if session.end_time_sec is None:
            session.end_reason = "game_end"

    # ── Игроки: steam_id → последний известный ник ────────────────────────────
    players: dict[str, str] = {}
    for session in sorted(sessions, key=lambda s: s.start_time_sec):
        players[session.steam_id] = session.display_name

    # ── Слоты без единой сессии (чистые боты) ────────────────────────────────
    session_entities = {s.entity_id for s in sessions}
    no_session = [
        eid for eid in unit_info
        if eid not in session_entities and eid not in no_steam_id
    ]

    return EntityMap(
        sessions   = sessions,
        players    = players,
        no_steam_id= no_steam_id,
        no_session = no_session,
    )


# ─── Вспомогательная функция для Attribution Engine ──────────────────────────

def get_player_at(
    entity_map: EntityMap,
    entity_id: int,
    time_sec: float,
) -> PlayerSession | None:
    """Возвращает сессию игрока который контролировал entity_id в момент time_sec."""
    for session in entity_map.sessions:
        if session.entity_id != entity_id:
            continue
        if session.start_time_sec > time_sec:
            continue
        if session.end_time_sec is not None and time_sec > session.end_time_sec:
            continue
        return session
    return None


def load_entity_map(path: str) -> EntityMap:
    """Загружает entity_map.json в объект EntityMap."""
    data = _load(path)
    sessions = [PlayerSession(**s) for s in data["sessions"]]
    return EntityMap(
        sessions   = sessions,
        players    = data["players"],
        no_steam_id= data["no_steam_id"],
        no_session = data["no_session"],
    )


# ─── Главная функция ──────────────────────────────────────────────────────────

def resolve(input_dir: str, output_dir: str) -> None:
    print("\nEntity Resolver...")

    meta     = _load(os.path.join(input_dir, "meta.json"))
    entities = _load(os.path.join(input_dir, "entities.json"))
    events   = _load(os.path.join(input_dir, "events.json"))

    entity_map = build_sessions(
        identities  = entities["identities"],
        units       = entities["units"],
        kills       = events["kills"],
        duration_sec= meta["duration_sec"],
    )

    # Запись
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "entity_map.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(asdict(entity_map), f, ensure_ascii=False, indent=2)
    print(f"  > {out_path}")

    # Итог
    deaths     = sum(1 for s in entity_map.sessions if s.end_reason == "death")
    slot_ch    = sum(1 for s in entity_map.sessions if s.end_reason == "slot_change")
    game_end   = sum(1 for s in entity_map.sessions if s.end_reason == "game_end")

    print(f"\nГотово:")
    print(f"  Сессий:          {len(entity_map.sessions)}")
    print(f"  Уникальных игроков: {len(entity_map.players)}")
    print(f"  Закрыто смертью: {deaths}")
    print(f"  Смена слота:     {slot_ch}")
    print(f"  До конца игры:   {game_end}")
    if entity_map.no_steam_id:
        print(f"  Без steam_id:    {entity_map.no_steam_id}  <- ручная правка")
    if entity_map.no_session:
        print(f"  Чистые боты:     {len(entity_map.no_session)}")


if __name__ == "__main__":
    input_dir  = sys.argv[1] if len(sys.argv) > 1 else "output"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    resolve(input_dir, output_dir)
