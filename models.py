from __future__ import annotations
from dataclasses import dataclass, field


# ─── Meta ────────────────────────────────────────────────────────────────────

@dataclass
class GameTime:
    year: int
    month: int
    day: int
    hour: int
    minute: int

@dataclass
class Meta:
    schema_version: str        # версия нашей модели данных
    parser_version: str        # "v1", "v2", ...
    source_file: str           # имя исходного файла
    server: str                # "T1", "T2", ...
    map: str
    mission: str
    timestamp: str             # ISO: "2026-05-24T21:08:09"
    game_time: GameTime
    duration_sec: float
    frame_count: int
    initial_markers: list


# ─── Entities ────────────────────────────────────────────────────────────────

@dataclass
class UnitEntity:
    """Event 1 — регистрация юнита."""
    entity_id: int
    initial_name: str          # мусорное имя до идентификации
    classname: str
    side: int
    group: str
    slot: str
    owner: int

@dataclass
class VehicleEntity:
    """Event 2 — регистрация техники."""
    entity_id: int
    name: str | None           # None в старом формате
    classname: str
    display_type: str
    owner: int

@dataclass
class Identity:
    """Event 3 — идентификация игрока (steamId + реальный ник)."""
    entity_id: int
    old_name: str
    new_name: str
    steam_id: str | None       # None если не передан
    time_sec: float

@dataclass
class Message:
    """Event 0 — текстовое сообщение."""
    time_sec: float
    message: str

@dataclass
class VehicleOccupancyEvent:
    """Выведено из states — вход/выход игрока из техники."""
    time_sec: float
    entity_id: int
    vehicle_entity_id: int | None  # None = вышел из техники
    action: str                    # "entered" | "exited"

@dataclass
class ConsciousnessEvent:
    """Выведено из states — изменение состояния сознания."""
    time_sec: float
    entity_id: int
    is_unconscious: bool

@dataclass
class Entities:
    units: list[UnitEntity]             = field(default_factory=list)
    vehicles: list[VehicleEntity]       = field(default_factory=list)
    identities: list[Identity]          = field(default_factory=list)
    messages: list[Message]             = field(default_factory=list)
    vehicle_occupancy: list[VehicleOccupancyEvent] = field(default_factory=list)
    consciousness: list[ConsciousnessEvent]        = field(default_factory=list)


# ─── Events ──────────────────────────────────────────────────────────────────

@dataclass
class KillEvent:
    """Event 4 — убийство."""
    time_sec: float
    frame_index: int
    killer_id: int
    victim_id: int
    weapon: str
    weapon_classname: str | None   # None в старом формате
    ammo_classname: str | None     # None в старом формате
    distance: float

@dataclass
class DamageEvent:
    """Event 5 — урон/попадание."""
    time_sec: float
    frame_index: int
    source_id: int
    target_id: int
    weapon: str
    weapon_classname: str | None   # None в старом формате
    distance: float
    damage_value: float
    is_unconscious: bool
    damage_source: str
    resolved_source_id: int | None # реальный стрелок если источник — техника

@dataclass
class MedicalEvent:
    """Event 7 — медицинское действие."""
    time_sec: float
    frame_index: int
    medic_id: int
    patient_id: int
    action: str
    value: float
    is_unconscious: bool

@dataclass
class Events:
    kills: list[KillEvent]       = field(default_factory=list)
    damage: list[DamageEvent]    = field(default_factory=list)
    medical: list[MedicalEvent]  = field(default_factory=list)
    markers: list               = field(default_factory=list)  # Event 6, сырые


# ─── Stats ───────────────────────────────────────────────────────────────────

@dataclass
class PlayerStats:
    steam_id: str
    display_name: str
    kills: int = 0
    deaths: int = 0
    teamkills: int = 0
    suicides: int = 0
    extra: dict = field(default_factory=dict)  # расширяемые метрики

@dataclass
class GameStats:
    game_id: str        # timestamp из meta
    server: str
    map: str
    mission: str
    duration_sec: float
    players: list[PlayerStats] = field(default_factory=list)


# ─── Attribution ─────────────────────────────────────────────────────────────

@dataclass
class AttributedKill:
    # Raw (из лога, не меняется)
    time_sec: float
    frame_index: int
    killer_entity_id: int
    victim_entity_id: int
    weapon: str
    weapon_classname: str | None
    ammo_classname: str | None
    distance: float

    # Attribution (результат engine)
    killer_steam_id: str | None
    victim_steam_id: str | None
    attribution_method: str        # имя правила
    attribution_confidence: float  # 0.0 — 1.0

    # Flags
    is_teamkill: bool
    is_suicide: bool
    is_vehicle_kill: bool
    needs_review: bool             # confidence == 0.0

@dataclass
class AttributedEvents:
    kills: list[AttributedKill] = field(default_factory=list)


# ─── Entity Map ──────────────────────────────────────────────────────────────

@dataclass
class PlayerSession:
    """Период когда конкретный игрок занимал конкретный слот."""
    steam_id: str
    entity_id: int
    display_name: str              # последний известный ник в этой сессии
    side: int | None
    group: str | None
    slot: str | None
    start_time_sec: float
    end_time_sec: float | None     # None = до конца игры
    end_reason: str | None         # "death" | "slot_change" | "game_end"

@dataclass
class EntityMap:
    """Результат Entity Resolver — используется Attribution Engine и Stats."""
    sessions: list[PlayerSession]
    players: dict                  # steam_id → latest display_name
    no_steam_id: list[int]         # entity_id с Event 3 но без steam_id
    no_session: list[int]          # entity_id без Event 3 (чистые боты)


# ─── States ──────────────────────────────────────────────────────────────────

@dataclass
class EntityState:
    entity_id: int
    x: float
    y: float
    z: float
    dir: float
    state_value: float | None      # 0=alive, 1=dead, 2=unconscious
    linked_entity_id: int | None   # техника в которой находится

@dataclass
class Frame:
    frame_index: int
    time_sec: float
    states: list[EntityState]      = field(default_factory=list)

@dataclass
class StatesChunk:
    frame_range: dict              # {"start": 0, "end": 199}
    frames: list[Frame]            = field(default_factory=list)
