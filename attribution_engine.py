"""
TSGstats — Attribution Engine
Читает: entity_map.json, entities.json, events.json
Пишет: attributed_events.json

Для каждого убийства определяет реального игрока-убийцу и жертву.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from entity_resolver import get_player_at, load_entity_map
from models import AttributedEvents, AttributedKill, EntityMap


# ─── Контекст для правил ──────────────────────────────────────────────────────

@dataclass
class RuleContext:
    entity_map: EntityMap
    vehicle_ids: set[int]                   # entity_ids которые являются техникой
    damage_by_source: dict[int, list]       # source_id → damage events (sorted by time_sec)
    occupancy_by_vehicle: dict[int, list]   # vehicle_id → occupancy events (sorted by time_sec)
    config: dict


# ─── Правила ─────────────────────────────────────────────────────────────────
# Каждое правило: (entity_id, time_sec, ctx) → (steam_id, confidence) | None

RuleResult = tuple[str, float] | None


def rule_direct(entity_id: int, time_sec: float, ctx: RuleContext) -> RuleResult:
    """Прямая атрибуция: entity_id имеет активную сессию в момент убийства."""
    session = get_player_at(ctx.entity_map, entity_id, time_sec)
    if session:
        return session.steam_id, 1.0
    return None


def rule_vehicle_damage_resolved(entity_id: int, time_sec: float, ctx: RuleContext) -> RuleResult:
    """
    Vehicle kill: ищем resolved_source_id в damage events от этой техники
    в пределах N секунд до убийства.
    """
    if entity_id not in ctx.vehicle_ids:
        return None

    window: float = ctx.config.get("vehicle_damage_window_sec", 5)

    for dmg in reversed(ctx.damage_by_source.get(entity_id, [])):
        if dmg["time_sec"] > time_sec:
            continue
        if dmg["time_sec"] < time_sec - window:
            break
        resolved_id = dmg.get("resolved_source_id")
        if not resolved_id:
            continue
        session = get_player_at(ctx.entity_map, resolved_id, time_sec)
        if session:
            return session.steam_id, 0.95

    return None


def rule_vehicle_occupancy(entity_id: int, time_sec: float, ctx: RuleContext) -> RuleResult:
    """
    Vehicle kill: определяем кто был в технике в момент убийства
    через vehicle_occupancy events.
    """
    if entity_id not in ctx.vehicle_ids:
        return None

    # Проигрываем все occupancy events до time_sec — строим картину "кто внутри"
    occupants: dict[int, float] = {}  # entity_id → время входа

    for ev in ctx.occupancy_by_vehicle.get(entity_id, []):
        if ev["time_sec"] > time_sec:
            break
        if ev["action"] == "entered":
            occupants[ev["entity_id"]] = ev["time_sec"]
        else:
            occupants.pop(ev["entity_id"], None)

    if not occupants:
        return None

    # Берём того кто вошёл последним
    latest_id = max(occupants, key=lambda eid: occupants[eid])
    session = get_player_at(ctx.entity_map, latest_id, time_sec)
    if session:
        return session.steam_id, 0.7

    return None


# ── Список правил — здесь добавляешь новые ────────────────────────────────────
KILLER_RULES = [
    rule_direct,
    rule_vehicle_damage_resolved,
    rule_vehicle_occupancy,
]


# ─── Ядро — не меняется ───────────────────────────────────────────────────────

def apply_rules(rules: list, entity_id: int, time_sec: float, ctx: RuleContext) -> tuple[str | None, str, float]:
    """Применяет правила по порядку. Возвращает первый результат."""
    for rule in rules:
        result = rule(entity_id, time_sec, ctx)
        if result:
            steam_id, confidence = result
            return steam_id, rule.__name__, confidence
    return None, "unresolved", 0.0


# ─── Атрибуция убийств ────────────────────────────────────────────────────────

def _get_session_side(entity_map: EntityMap, steam_id: str, time_sec: float) -> int | None:
    """Возвращает сторону игрока в момент времени."""
    for s in entity_map.sessions:
        if s.steam_id != steam_id:
            continue
        if s.start_time_sec > time_sec:
            continue
        if s.end_time_sec is not None and time_sec > s.end_time_sec:
            continue
        return s.side
    return None


def attribute_kills(kills: list[dict], ctx: RuleContext) -> list[AttributedKill]:
    attributed = []

    for kill in kills:
        time_sec   = kill["time_sec"]
        killer_id  = kill["killer_id"]
        victim_id  = kill["victim_id"]

        # ── Атрибуция убийцы ──────────────────────────────────────────────────
        killer_steam, method, confidence = apply_rules(
            KILLER_RULES, killer_id, time_sec, ctx
        )

        # ── Атрибуция жертвы (всегда прямая) ─────────────────────────────────
        victim_session = get_player_at(ctx.entity_map, victim_id, time_sec)
        victim_steam = victim_session.steam_id if victim_session else None

        # ── Флаги ─────────────────────────────────────────────────────────────
        is_suicide     = killer_id == victim_id
        is_vehicle_kill= killer_id in ctx.vehicle_ids

        is_teamkill = False
        if killer_steam and victim_steam and not is_suicide:
            killer_side = _get_session_side(ctx.entity_map, killer_steam, time_sec)
            victim_side = _get_session_side(ctx.entity_map, victim_steam, time_sec)
            if killer_side is not None and killer_side == victim_side:
                is_teamkill = True

        attributed.append(AttributedKill(
            time_sec             = time_sec,
            frame_index          = kill["frame_index"],
            killer_entity_id     = killer_id,
            victim_entity_id     = victim_id,
            weapon               = kill["weapon"],
            weapon_classname     = kill.get("weapon_classname"),
            ammo_classname       = kill.get("ammo_classname"),
            distance             = kill["distance"],
            killer_steam_id      = killer_steam,
            victim_steam_id      = victim_steam,
            attribution_method   = method,
            attribution_confidence=confidence,
            is_teamkill          = is_teamkill,
            is_suicide           = is_suicide,
            is_vehicle_kill      = is_vehicle_kill,
            needs_review         = confidence == 0.0,
        ))

    return attributed


# ─── Построение контекста ─────────────────────────────────────────────────────

def build_context(entity_map: EntityMap, entities: dict, events: dict, config: dict) -> RuleContext:
    vehicle_ids = {v["entity_id"] for v in entities.get("vehicles", [])}

    # damage events сгруппированные по source_id, отсортированные по времени
    damage_by_source: dict[int, list] = defaultdict(list)
    for dmg in events.get("damage", []):
        damage_by_source[dmg["source_id"]].append(dmg)
    for lst in damage_by_source.values():
        lst.sort(key=lambda x: x["time_sec"])

    # vehicle occupancy сгруппированные по vehicle_entity_id
    occupancy_by_vehicle: dict[int, list] = defaultdict(list)
    for ev in entities.get("vehicle_occupancy", []):
        if ev["vehicle_entity_id"] is not None:
            occupancy_by_vehicle[ev["vehicle_entity_id"]].append(ev)
    for lst in occupancy_by_vehicle.values():
        lst.sort(key=lambda x: x["time_sec"])

    return RuleContext(
        entity_map          = entity_map,
        vehicle_ids         = vehicle_ids,
        damage_by_source    = dict(damage_by_source),
        occupancy_by_vehicle= dict(occupancy_by_vehicle),
        config              = config,
    )


# ─── Главная функция ──────────────────────────────────────────────────────────

def attribute(input_dir: str, output_dir: str) -> None:
    print("\nAttribution Engine...")

    entity_map = load_entity_map(os.path.join(input_dir, "entity_map.json"))
    entities   = _load(os.path.join(input_dir, "entities.json"))
    events     = _load(os.path.join(input_dir, "events.json"))
    config     = _load("attribution_rules.json")

    ctx = build_context(entity_map, entities, events, config)

    attributed_kills = attribute_kills(events.get("kills", []), ctx)
    result = AttributedEvents(kills=attributed_kills)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "attributed_events.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(asdict(result), f, ensure_ascii=False, indent=2)
    print(f"  > {out_path}")

    # ── Итог ──────────────────────────────────────────────────────────────────
    direct   = sum(1 for k in attributed_kills if k.attribution_method == "rule_direct")
    veh_dmg  = sum(1 for k in attributed_kills if k.attribution_method == "rule_vehicle_damage_resolved")
    veh_occ  = sum(1 for k in attributed_kills if k.attribution_method == "rule_vehicle_occupancy")
    unres    = sum(1 for k in attributed_kills if k.needs_review)
    teamkill = sum(1 for k in attributed_kills if k.is_teamkill)
    suicide  = sum(1 for k in attributed_kills if k.is_suicide)

    print(f"\nГотово: {len(attributed_kills)} убийств")
    print(f"  direct:                   {direct}")
    print(f"  vehicle_damage_resolved:  {veh_dmg}")
    print(f"  vehicle_occupancy:        {veh_occ}")
    print(f"  needs_review:             {unres}")
    print(f"  teamkills:                {teamkill}")
    print(f"  suicides:                 {suicide}")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    input_dir  = sys.argv[1] if len(sys.argv) > 1 else "output"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    attribute(input_dir, output_dir)
