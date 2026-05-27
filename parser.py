"""
TSGstats — Raw Parser
Читает log.txt и выдаёт: meta.json, entities.json, events.json, states/frames_*.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime

from models import (
    ConsciousnessEvent, Entities, EntityState, Events, Frame, GameTime,
    Identity, KillEvent, DamageEvent, MedicalEvent, Message, Meta,
    StatesChunk, UnitEntity, VehicleEntity, VehicleOccupancyEvent,
)

CHUNK_SIZE = 200  # фреймов в одном файле states


# ─── Версионирование ─────────────────────────────────────────────────────────

def get_parser_version(timestamp_str: str) -> str:
    """Выбирает версию парсера по дате реплея."""
    with open("versions.json", encoding="utf-8") as f:
        versions = json.load(f)

    # Парсим timestamp из лога: "2026-05-24-21-08-09"
    for fmt in ("%Y-%m-%d-%H-%M-%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            date = datetime.strptime(timestamp_str, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Неизвестный формат timestamp: {timestamp_str!r}")

    for v in sorted(versions, key=lambda x: x["from"], reverse=True):
        from_date = datetime.strptime(v["from"], "%Y-%m-%d")
        to_date = datetime.strptime(v["to"], "%Y-%m-%d") if v["to"] else None
        if date >= from_date and (to_date is None or date <= to_date):
            return v["parser"]

    raise ValueError(f"Нет парсера для даты: {date.date()}")


# ─── Загрузка файла ───────────────────────────────────────────────────────────

# SQF-токены, невалидные в JSON: any, nil, objNull, grpNull и т.д.
# Lookbehind/lookahead (?<!["\w]) / (?!["\w]) исключает совпадения
# внутри строк ("any") и составных имён ("any_weapon", "company").
_SQF_NULL_RE = re.compile(
    r'(?<!["\w])(any|nil|objNull|grpNull|teamMemberNull|configNull|scriptNull)(?!["\w])'
)


def preprocess(text: str) -> str:
    """Исправляет известные дефекты JSON до парсинга."""
    text = text.replace("-1.#IND", "null")
    text = text.replace("-1.#INF", "null")
    text = text.replace("1.#INF", "null")
    text = _SQF_NULL_RE.sub("null", text)
    # Обрезаем мусор после последней закрывающей скобки
    last = text.rfind("]")
    if last != -1:
        text = text[: last + 1]
    return text


def load_log(path: str) -> list:
    """Загружает log.txt с перебором кодировок."""
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            with open(path, encoding=enc, errors="replace") as f:
                text = f.read()
            return json.loads(preprocess(text))
        except json.JSONDecodeError:
            continue

    # Диагностика: показываем начало файла чтобы понять причину сбоя
    try:
        with open(path, "rb") as f:
            head = f.read(400)
        is_binary = sum(1 for b in head[:100] if b < 0x09 or 0x0E <= b <= 0x1F) > 3
        if is_binary:
            print(f"  ! Файл выглядит как бинарный (armake2 не распаковал?): {head[:80]!r}")
        else:
            snippet = head.decode("latin-1", errors="replace")
            print(f"  ! Начало файла (первые 300 символов):\n    {snippet[:300]!r}")
    except Exception:
        pass

    raise ValueError(f"Не удалось распарсить JSON: {path}")


# ─── Parser v1 ───────────────────────────────────────────────────────────────

class ParserV1:
    """
    Текущий формат (с ~2020):
      Event 2: [2, id, name, class, icon, owner]
      Event 4: [4, time, killerId, id, weapon, weaponClass, ammoClass, distance]
      Event 5: [5, time, sourceId, id, weapon, weaponClass, distance, damage,
                   isUnconscious, ammo, vehicleId]
    """

    def parse_header(self, raw: list) -> Meta:
        gt = raw[3]
        ts = raw[2]
        for fmt in ("%Y-%m-%d-%H-%M-%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                timestamp = datetime.strptime(ts, fmt).strftime("%Y-%m-%dT%H:%M:%S")
                break
            except ValueError:
                continue
        else:
            timestamp = ts

        return Meta(
            schema_version="1.0",
            parser_version="v1",
            source_file="",       # заполняется в parse()
            server="",            # заполняется в parse()
            map=raw[0],
            mission=raw[1],
            timestamp=timestamp,
            game_time=GameTime(
                year=gt[0], month=gt[1], day=gt[2], hour=gt[3], minute=gt[4]
            ),
            duration_sec=0.0,     # заполняется после обхода фреймов
            frame_count=0,        # заполняется после обхода фреймов
            initial_markers=raw[5] if len(raw) > 5 else [],
        )

    def parse_event_0(self, raw: list, time_sec: float) -> Message:
        return Message(time_sec=time_sec, message=raw[1])

    def parse_event_1(self, raw: list) -> UnitEntity:
        return UnitEntity(
            entity_id=raw[1],
            initial_name=raw[2],
            classname=raw[3],
            side=raw[4],
            # raw[5] = displayType (иконка реплея) — не нужен для статистики
            group=raw[6],
            slot=raw[7],
            owner=raw[8],
        )

    def parse_event_2(self, raw: list) -> VehicleEntity:
        # [2, id, name, class, icon, owner]
        return VehicleEntity(
            entity_id=raw[1],
            name=raw[2],
            classname=raw[3],
            display_type=raw[4],
            owner=raw[5],
        )

    def parse_event_3(self, raw: list, time_sec: float) -> Identity:
        steam_id = raw[4] if len(raw) > 4 and raw[4] else None
        return Identity(
            entity_id=raw[1],
            old_name=raw[2],
            new_name=raw[3],
            steam_id=steam_id,
            time_sec=time_sec,
        )

    def parse_event_4(self, raw: list, time_sec: float, frame_index: int) -> KillEvent:
        # [4, time, killerId, id, weapon, weaponClass, ammoClass, distance]
        return KillEvent(
            time_sec=raw[1],
            frame_index=frame_index,
            killer_id=raw[2],
            victim_id=raw[3],
            weapon=raw[4],
            weapon_classname=raw[5] if raw[5] else None,
            ammo_classname=raw[6] if raw[6] else None,
            distance=raw[7],
        )

    def parse_event_5(self, raw: list, time_sec: float, frame_index: int) -> DamageEvent:
        # [5, time, sourceId, id, weapon, weaponClass, distance,
        #    damage, isUnconscious, ammo, vehicleId]
        resolved = raw[10] if len(raw) > 10 and raw[10] != raw[2] else None
        return DamageEvent(
            time_sec=raw[1],
            frame_index=frame_index,
            source_id=raw[2],
            target_id=raw[3],
            weapon=raw[4],
            weapon_classname=raw[5] if raw[5] else None,
            distance=raw[6],
            damage_value=raw[7],
            is_unconscious=bool(raw[8]),
            damage_source=raw[9],
            resolved_source_id=resolved,
        )

    def parse_event_7(self, raw: list, time_sec: float, frame_index: int) -> MedicalEvent:
        return MedicalEvent(
            time_sec=raw[1],
            frame_index=frame_index,
            medic_id=raw[2],
            patient_id=raw[3],
            action=raw[4],
            value=raw[5],
            is_unconscious=bool(raw[6]),
        )

    def parse_state(self, raw: list) -> EntityState:
        return EntityState(
            entity_id=raw[0],
            x=raw[1],
            y=raw[2],
            z=raw[3],
            dir=raw[4],
            state_value=raw[5] if len(raw) > 5 else None,
            linked_entity_id=raw[6] if len(raw) > 6 else None,
        )


PARSERS = {"v1": ParserV1}


# ─── Derived events из states ─────────────────────────────────────────────────

def derive_vehicle_occupancy(frames: list[Frame]) -> list[VehicleOccupancyEvent]:
    """Отслеживает вход/выход игроков из техники по изменению linked_entity_id."""
    events = []
    prev: dict[int, int | None] = {}  # entity_id → vehicle_entity_id

    for frame in frames:
        for state in frame.states:
            eid = state.entity_id
            curr = state.linked_entity_id
            prev_vid = prev.get(eid)

            if eid not in prev:
                # Первое появление: если уже в технике — фиксируем
                if curr is not None:
                    events.append(VehicleOccupancyEvent(
                        time_sec=frame.time_sec,
                        entity_id=eid,
                        vehicle_entity_id=curr,
                        action="entered",
                    ))
            elif prev_vid != curr:
                if curr is not None:
                    events.append(VehicleOccupancyEvent(
                        time_sec=frame.time_sec,
                        entity_id=eid,
                        vehicle_entity_id=curr,
                        action="entered",
                    ))
                else:
                    events.append(VehicleOccupancyEvent(
                        time_sec=frame.time_sec,
                        entity_id=eid,
                        vehicle_entity_id=None,
                        action="exited",
                    ))

            prev[eid] = curr

    return events


def derive_consciousness(frames: list[Frame]) -> list[ConsciousnessEvent]:
    """Отслеживает изменения состояния сознания (state_value == 2)."""
    events = []
    prev: dict[int, bool] = {}  # entity_id → is_unconscious

    for frame in frames:
        for state in frame.states:
            if state.state_value is None:
                continue

            eid = state.entity_id
            curr = state.state_value == 2  # 2 = isUnconscious

            if eid not in prev:
                if curr:
                    events.append(ConsciousnessEvent(
                        time_sec=frame.time_sec,
                        entity_id=eid,
                        is_unconscious=True,
                    ))
            elif prev[eid] != curr:
                events.append(ConsciousnessEvent(
                    time_sec=frame.time_sec,
                    entity_id=eid,
                    is_unconscious=curr,
                ))

            prev[eid] = curr

    return events


# ─── Запись файлов ────────────────────────────────────────────────────────────

def write_json(path: str, data: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(data), f, ensure_ascii=False, indent=2)
    print(f"  > {path}")


# ─── Главная функция ──────────────────────────────────────────────────────────

def parse(log_path: str, output_dir: str, server: str = "") -> None:
    print(f"\nПарсим: {log_path}")

    raw_log = load_log(log_path)
    raw_header = raw_log[0]

    version = get_parser_version(raw_header[2])
    print(f"Версия парсера: {version}")

    if version not in PARSERS:
        raise ValueError(f"Парсер {version!r} не реализован")

    p = PARSERS[version]()
    meta = p.parse_header(raw_header)
    meta.source_file = os.path.basename(log_path)
    meta.server = server

    # ── Обход фреймов ──────────────────────────────────────────────────────
    units, vehicles, identities, messages = [], [], [], []
    kills, damage_events, medical_events, markers = [], [], [], []
    frames: list[Frame] = []
    warnings = 0

    for frame_index, raw_frame in enumerate(raw_log[1:]):
        time_sec: float = raw_frame[0]
        events_raw: list = raw_frame[1]
        states_raw: list = raw_frame[2:]

        # States
        states = []
        for s in states_raw:
            try:
                states.append(p.parse_state(s))
            except (IndexError, TypeError):
                warnings += 1
        frames.append(Frame(frame_index=frame_index, time_sec=time_sec, states=states))

        # Events
        for event in events_raw:
            if not isinstance(event, list) or not event:
                continue
            eid = event[0]
            try:
                if eid == 0:
                    messages.append(p.parse_event_0(event, time_sec))
                elif eid == 1:
                    units.append(p.parse_event_1(event))
                elif eid == 2:
                    vehicles.append(p.parse_event_2(event))
                elif eid == 3:
                    identities.append(p.parse_event_3(event, time_sec))
                elif eid == 4:
                    kills.append(p.parse_event_4(event, time_sec, frame_index))
                elif eid == 5:
                    damage_events.append(p.parse_event_5(event, time_sec, frame_index))
                elif eid == 6:
                    markers.append(event)
                elif eid == 7:
                    medical_events.append(p.parse_event_7(event, time_sec, frame_index))
            except (IndexError, TypeError, KeyError) as e:
                print(f"  ! Event {eid} frame {frame_index}: {e}")
                warnings += 1

    meta.duration_sec = frames[-1].time_sec if frames else 0.0
    meta.frame_count = len(frames)

    # ── Derived events ─────────────────────────────────────────────────────
    vehicle_occupancy = derive_vehicle_occupancy(frames)
    consciousness = derive_consciousness(frames)

    entities = Entities(
        units=units,
        vehicles=vehicles,
        identities=identities,
        messages=messages,
        vehicle_occupancy=vehicle_occupancy,
        consciousness=consciousness,
    )
    events_out = Events(
        kills=kills,
        damage=damage_events,
        medical=medical_events,
        markers=markers,
    )

    # ── Запись файлов ──────────────────────────────────────────────────────
    states_dir = os.path.join(output_dir, "states")
    os.makedirs(states_dir, exist_ok=True)

    write_json(os.path.join(output_dir, "meta.json"), meta)
    write_json(os.path.join(output_dir, "entities.json"), entities)
    write_json(os.path.join(output_dir, "events.json"), events_out)

    for i in range(0, len(frames), CHUNK_SIZE):
        chunk = frames[i : i + CHUNK_SIZE]
        name = f"frames_{i:04d}-{i + len(chunk) - 1:04d}.json"
        write_json(
            os.path.join(states_dir, name),
            StatesChunk(
                frame_range={"start": i, "end": i + len(chunk) - 1},
                frames=chunk,
            ),
        )

    # ── Итог ───────────────────────────────────────────────────────────────
    print(f"\nГотово:")
    print(f"  Фреймов:       {len(frames)}  ({meta.duration_sec:.0f} сек)")
    print(f"  Юнитов:        {len(units)}")
    print(f"  Техники:       {len(vehicles)}")
    print(f"  Идентификаций: {len(identities)}")
    print(f"  Убийств:       {len(kills)}")
    print(f"  Урона:         {len(damage_events)}")
    print(f"  Медицины:      {len(medical_events)}")
    print(f"  Occupancy:     {len(vehicle_occupancy)}")
    print(f"  Consciousness: {len(consciousness)}")
    if warnings:
        print(f"  Предупреждений: {warnings}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python parser.py <log.txt> [output_dir] [server]")
        sys.exit(1)

    log_path   = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    server     = sys.argv[3] if len(sys.argv) > 3 else ""

    parse(log_path, output_dir, server)
