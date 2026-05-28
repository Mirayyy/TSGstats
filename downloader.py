"""
TSGstats — Downloader
Проверяет сайт с реплеями на новые .pbo.7z файлы,
скачивает и распаковывает log.txt из каждого нового архива.
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import py7zr

REPLAYS_URL = "https://replays.tsgames.ru/replays/"

# Значения по умолчанию (переопределяются через env или аргументы)
DEFAULT_DAYS_BACK   = 7    # не обрабатывать архивы старше N дней
DEFAULT_MAX_PER_RUN = 20   # максимум архивов за один запуск


# ── Список файлов на сайте ────────────────────────────────────────────────────

def list_remote_archives() -> list[str]:
    """Возвращает имена .pbo.7z файлов с сайта реплеев, отсортированные по дате."""
    with httpx.Client(timeout=30) as client:
        resp = client.get(REPLAYS_URL)
        resp.raise_for_status()

    names = re.findall(r'href="([^"/]+\.pbo\.7z)"', resp.text)
    return sorted(set(names))


def _parse_archive_date(filename: str) -> datetime | None:
    """
    Извлекает дату из имени архива.
    Формат: T1.2026-01-28-21-00-33.mTSG@...pbo.7z
    """
    m = re.search(r'\.(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.', filename)
    if not m:
        return None
    parts = m.group(1).split("-")
    try:
        return datetime(
            int(parts[0]), int(parts[1]), int(parts[2]),
            int(parts[3]), int(parts[4]), int(parts[5]),
            tzinfo=timezone.utc,
        )
    except (ValueError, IndexError):
        return None


# Канонические имена типов миссий (ключ — lower-case)
_TYPE_NORM: dict[str, str] = {
    "mtsg": "mTSG",
    "tsg":  "TSG",
}


def _normalize_mission_type(raw: str) -> str:
    """'MTSG' → 'mTSG', 'tsg' → 'TSG', неизвестный тип — без изменений."""
    return _TYPE_NORM.get(raw.strip().lower(), raw.strip())


def _parse_archive_info(filename: str) -> dict:
    """
    Разбирает имя архива и возвращает атрибуты.

    'T1.2026-05-20-20-27-54.mTSG%4016_Plane_Dogfight_v4.chernarus.pbo.7z'
    → {
        'server':       'T1',
        'date':         datetime(2026, 5, 20, ...),
        'mission_type': 'mTSG',
        'player_count': 16,
        'map':          'chernarus',
      }
    """
    info: dict = {
        "server": "",
        "date": None,
        "mission_type": "",
        "mission_name": "",
        "player_count": 0,
        "map": "",
    }

    # URL-decode (%40 → @), strip .pbo.7z, split on '.'
    name = urllib.parse.unquote(filename)
    if name.endswith(".pbo.7z"):
        name = name[:-7]
    parts = name.split(".")

    if len(parts) < 2:
        return info

    info["server"] = parts[0]                        # 'T1'
    info["date"]   = _parse_archive_date(filename)   # reuse regex on original

    if len(parts) < 3:
        return info

    mission_part = parts[2]   # 'mTSG@16_Plane_Dogfight_v4'
    if "@" in mission_part:
        at_idx   = mission_part.index("@")
        info["mission_type"] = _normalize_mission_type(mission_part[:at_idx])  # 'mTSG'
        after_at = mission_part[at_idx + 1:]                  # '16_Plane_Dogfight_v4'
        m = re.match(r"^(\d+)(?:_(.+))?$", after_at)
        if m:
            info["player_count"] = int(m.group(1))            # 16
            info["mission_name"] = m.group(2) or ""           # 'Plane_Dogfight_v4'
    else:
        info["mission_type"] = _normalize_mission_type(mission_part)
        info["mission_name"] = mission_part

    if len(parts) >= 4:
        info["map"] = parts[3]                                # 'chernarus'

    return info


_VERSIONS_CACHE: list | None = None


def _load_versions() -> list:
    """Загружает versions.json (с кешированием)."""
    global _VERSIONS_CACHE
    if _VERSIONS_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), "versions.json")
        try:
            with open(path, encoding="utf-8") as f:
                _VERSIONS_CACHE = json.load(f)
        except Exception as e:
            print(f"  WARNING: не удалось прочитать versions.json: {e}")
            _VERSIONS_CACHE = []
    return _VERSIONS_CACHE


def _version_for_date(dt: datetime) -> str | None:
    """
    Возвращает версию парсера для данной даты из versions.json.
    Возвращает None если ни одна запись не покрывает эту дату
    (формат лога для этого архива неизвестен).
    """
    dt_date = dt.date()
    for entry in _load_versions():
        try:
            from_date = datetime.fromisoformat(entry["from"]).date()
        except Exception:
            continue
        to_str = entry.get("to")
        to_date = datetime.fromisoformat(to_str).date() if to_str else None
        if dt_date >= from_date and (to_date is None or dt_date <= to_date):
            return entry.get("parser")
    return None


# ── Supabase: обработанные файлы ──────────────────────────────────────────────

def _sb_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


def get_processed_files() -> set[str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        return set()
    try:
        with httpx.Client(base_url=url, headers=_sb_headers(), timeout=10) as client:
            resp = client.get("/rest/v1/processed_replays", params={"select": "filename"})
            resp.raise_for_status()
            return {row["filename"] for row in resp.json()}
    except Exception as e:
        print(f"  WARNING: не удалось получить список обработанных: {e}")
        return set()


def mark_processed(filename: str, status: str = "ok") -> None:
    """Отмечает архив как обработанный. status = 'ok' | 'error'."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        return
    try:
        with httpx.Client(base_url=url, headers=_sb_headers(), timeout=10) as client:
            client.post(
                "/rest/v1/processed_replays",
                json={
                    "filename":     filename,
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "status":       status,
                },
                headers={"Prefer": "resolution=merge-duplicates"},
            )
    except Exception as e:
        print(f"  WARNING: не удалось отметить {filename} как обработанный: {e}")


# ── ArmA LZSS декомпрессор ────────────────────────────────────────────────────

CPRS_MAGIC = 0x43707273  # packing = LZSS-compressed
VERS_MAGIC = 0x56657273  # packing = version entry


def _lzss_decompress(data: bytes, orig_size: int) -> bytes:
    """
    Декомпрессия ArmA LZSS (packing=0x43707273).
    Управляющий байт: каждый бит (LSB→MSB) задаёт тип следующего блока:
      1 = literal byte
      0 = back-reference: 2 байта → (length = b2 & 0xF + 3, offset = b1 | (b2>>4)<<8)
    """
    result = bytearray()
    pos = 0

    while len(result) < orig_size and pos < len(data):
        ctrl = data[pos]
        pos += 1

        for bit in range(8):
            if len(result) >= orig_size or pos >= len(data):
                break

            if ctrl & (1 << bit):
                result.append(data[pos])
                pos += 1
            else:
                if pos + 1 >= len(data):
                    break
                b1, b2 = data[pos], data[pos + 1]
                pos += 2

                length = (b2 & 0x0F) + 3
                offset = b1 | ((b2 & 0xF0) << 4)

                if offset == 0:
                    break

                start = len(result) - offset
                for j in range(length):
                    idx = start + j
                    result.append(result[idx] if idx >= 0 else 0)
                    if len(result) >= orig_size:
                        break

    return bytes(result)


# ── PBO парсер ────────────────────────────────────────────────────────────────

def _extract_log_from_pbo(pbo_path: str, output_dir: str) -> str | None:
    """
    Извлекает log.txt из .pbo файла (формат архива ArmA/Bohemia).

    Структура PBO:
      [version entry (опц.)] → [file entries...] → [boundary entry] → [raw file data]
    Каждый entry: null-term name + 5×uint32 (packing, orig_size, reserved, timestamp, data_size)
    """
    with open(pbo_path, "rb") as f:
        raw = f.read()

    pos = 0
    entries: list[tuple[str, int, int, int]] = []  # (name, packing, orig_size, data_size)

    while pos < len(raw):
        null = raw.find(b"\x00", pos)
        if null < 0 or null + 21 > len(raw):
            break
        name = raw[pos:null].decode("latin-1", errors="replace")
        pos = null + 1

        packing, orig_size, _res, _ts, data_size = struct.unpack_from("<5I", raw, pos)
        pos += 20

        # Version entry: пустое имя + magic → пропускаем extended props
        if name == "" and packing == VERS_MAGIC:
            while pos < len(raw):
                end = raw.find(b"\x00", pos)
                if end < 0:
                    return None
                key = raw[pos:end].decode("latin-1", errors="replace")
                pos = end + 1
                if key == "":
                    break
                end = raw.find(b"\x00", pos)
                if end < 0:
                    return None
                pos = end + 1
            continue

        # Boundary entry — конец заголовка
        if name == "" and data_size == 0:
            break

        if name:
            entries.append((name, packing, orig_size, data_size))

    # pos теперь указывает на начало блока данных файлов
    offset = pos
    for name, packing, orig_size, data_size in entries:
        basename = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if basename == "log.txt":
            chunk = raw[offset: offset + data_size]
            if packing == CPRS_MAGIC:
                chunk = _lzss_decompress(chunk, orig_size)
            out_path = os.path.join(output_dir, "log.txt")
            with open(out_path, "wb") as f:
                f.write(chunk)
            return out_path
        offset += data_size

    return None


# ── Скачивание и распаковка ───────────────────────────────────────────────────

def _extract_log(archive_path: str, dest_dir: str) -> str | None:
    """
    Извлекает log.txt из .pbo.7z:
      1. Распаковываем .pbo из .7z
      2. Разбираем заголовок .pbo и извлекаем log.txt (с LZSS-декомпрессией)
    """
    with py7zr.SevenZipFile(archive_path, mode="r") as z:
        names = z.getnames()
        pbo_name = next((n for n in names if n.lower().endswith(".pbo")), None)

        if not pbo_name:
            print(f"    .pbo не найден в архиве. Содержимое: {names}")
            return None

        z.extract(targets=[pbo_name], path=dest_dir)

    pbo_path = os.path.join(dest_dir, pbo_name)
    if not os.path.exists(pbo_path):
        return None

    log_path = _extract_log_from_pbo(pbo_path, dest_dir)

    # Удаляем .pbo — больше не нужен
    os.unlink(pbo_path)

    if not log_path:
        print("    log.txt не найден внутри .pbo")

    return log_path


def download_archive(filename: str, dest_dir: str) -> str | None:
    """Скачивает архив и возвращает путь к извлечённому log.txt."""
    url = REPLAYS_URL + filename

    with tempfile.NamedTemporaryFile(suffix=".7z", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        print(f"    Скачиваем {filename}...")
        with httpx.Client(timeout=180) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)

        size_mb = os.path.getsize(tmp_path) / 1024 / 1024
        print(f"    Скачано {size_mb:.1f} MB, распаковываем...")
        return _extract_log(tmp_path, dest_dir)

    except Exception as e:
        print(f"    ОШИБКА скачивания {filename}: {e}")
        return None
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Главная функция ───────────────────────────────────────────────────────────

def _parse_date_env(key: str) -> datetime | None:
    """Парсит дату из env переменной (ISO формат: 2026-05-01)."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"  WARNING: неверный формат {key}={raw!r} (ожидается YYYY-MM-DD)")
        return None


def get_new_replays(
    work_dir: str,
    days_back: int | None = None,
    max_per_run: int | None = None,
    servers: list[str] | None = None,
    mission_types: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[tuple[str, str]]:
    """
    Находит новые архивы, применяет фильтры и скачивает их новейшие → старые.
    Останавливается на первом архиве без версии парсера.
    Возвращает список (archive_filename, log_txt_path).

    Параметры (читаются из env если не переданы явно):
      days_back     — DAYS_BACK         (default: 7)
      max_per_run   — MAX_PER_RUN       (default: 20)
      servers       — FILTER_SERVERS    ('T1,T2,...' или пусто = все)
      mission_types — FILTER_TYPES      ('mTSG,TSG,...', case-insensitive)
      date_from     — FILTER_DATE_FROM  (YYYY-MM-DD; заменяет days_back как нижнюю границу)
      date_to       — FILTER_DATE_TO    (YYYY-MM-DD; верхняя граница, включительно)

    Фильтр FILTER_MIN_PLAYERS (реальные игроки) применяется в process_one()
    после parse() — число игроков известно только из log.txt.
    """
    if days_back is None:
        days_back = int(os.environ.get("DAYS_BACK", DEFAULT_DAYS_BACK))
    if max_per_run is None:
        max_per_run = int(os.environ.get("MAX_PER_RUN", DEFAULT_MAX_PER_RUN))

    # Читаем фильтры из env если не переданы явно
    if servers is None:
        raw = os.environ.get("FILTER_SERVERS", "")
        servers = [s.strip() for s in raw.split(",") if s.strip()] or None
    if mission_types is None:
        raw = os.environ.get("FILTER_TYPES", "")
        mission_types = [t.strip() for t in raw.split(",") if t.strip()] or None
    if date_from is None:
        date_from = _parse_date_env("FILTER_DATE_FROM")
    if date_to is None:
        date_to = _parse_date_env("FILTER_DATE_TO")

    # Нормализуем типы миссий для case-insensitive сравнения
    mission_types_norm: list[str] | None = None
    if mission_types:
        mission_types_norm = [_normalize_mission_type(t) for t in mission_types]

    # Нижняя граница дат: явная date_from имеет приоритет над days_back
    if date_from is not None:
        cutoff = date_from
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Верхняя граница: конец дня date_to (включительно)
    ceiling: datetime | None = None
    if date_to is not None:
        ceiling = date_to.replace(hour=23, minute=59, second=59)

    print("\nDownloader...")
    fp = []
    if date_from:  fp.append(f"от {date_from.date()}")
    if date_to:    fp.append(f"до {date_to.date()}")
    if not fp:     fp.append(f"последние {days_back} дн.")
    if servers:              fp.append(f"серверы={','.join(servers)}")
    if mission_types_norm:   fp.append(f"типы={','.join(mission_types_norm)}")
    print(f"  Параметры: макс. {max_per_run}, {', '.join(fp)}")

    all_archives = list_remote_archives()
    processed    = get_processed_files()

    # Фильтр 1: не обработанные
    candidates = [f for f in all_archives if f not in processed]

    # Фильтр 2: дата + атрибуты
    _MIN_DT = datetime.min.replace(tzinfo=timezone.utc)
    dated: list[tuple[datetime, str]] = []
    skipped_old = skipped_filter = 0

    for filename in candidates:
        info = _parse_archive_info(filename)
        dt   = info["date"]

        # Нижняя граница
        if dt is not None and dt < cutoff:
            skipped_old += 1
            continue

        # Верхняя граница (date_to)
        if ceiling is not None and dt is not None and dt > ceiling:
            skipped_filter += 1
            continue

        # Фильтр по серверу
        if servers and info["server"] not in servers:
            skipped_filter += 1
            continue

        # Фильтр по типу миссии (нормализованный → case-insensitive)
        if mission_types_norm and info["mission_type"] not in mission_types_norm:
            skipped_filter += 1
            continue

        dated.append((dt or _MIN_DT, filename))

    # Сортируем НОВЫЕ → СТАРЫЕ
    dated.sort(key=lambda x: x[0], reverse=True)

    print(f"  Всего: {len(all_archives)} | уже обработано: {len(processed)} | "
          f"слишком старых: {skipped_old} | по фильтрам: {skipped_filter} | "
          f"кандидатов: {len(dated)}")

    # Проверяем версию парсера перед скачиванием (обходим от новейшего к старому)
    to_download: list[tuple[datetime, str]] = []
    for dt, filename in dated:
        if len(to_download) >= max_per_run:
            break
        if dt != _MIN_DT:
            version = _version_for_date(dt)
            if version is None:
                print(f"  СТОП: нет версии парсера для '{filename}' "
                      f"(дата {dt.date()}). Архивы с этой даты и старше пропускаем.")
                break
        to_download.append((dt, filename))

    print(f"  К скачиванию: {len(to_download)}")

    results: list[tuple[str, str]] = []
    for _, filename in to_download:
        archive_dir = os.path.join(work_dir, filename.replace(".pbo.7z", ""))
        os.makedirs(archive_dir, exist_ok=True)

        log_path = download_archive(filename, archive_dir)
        if log_path:
            results.append((filename, log_path))
        else:
            print(f"    Пропускаем {filename} — log.txt не найден")

    return results
