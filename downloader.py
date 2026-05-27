"""
TSGstats — Downloader
Проверяет сайт с реплеями на новые .pbo.7z файлы,
скачивает и распаковывает log.txt из каждого нового архива.
"""

from __future__ import annotations

import os
import re
import struct
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def mark_processed(filename: str) -> None:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        return
    try:
        with httpx.Client(base_url=url, headers=_sb_headers(), timeout=10) as client:
            client.post(
                "/rest/v1/processed_replays",
                json={"filename": filename, "processed_at": datetime.now(timezone.utc).isoformat()},
                headers={"Prefer": "resolution=merge-duplicates"},
            )
    except Exception as e:
        print(f"  WARNING: не удалось отметить {filename} как обработанный: {e}")


# ── ArmA LZSS декомпрессор ────────────────────────────────────────────────────

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
                # Literal
                result.append(data[pos])
                pos += 1
            else:
                # Back-reference
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

CPRS_MAGIC = 0x43707273  # packing = compressed
VERS_MAGIC = 0x56657273  # packing = version entry


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

        # Boundary entry
        if name == "" and data_size == 0:
            break

        if name:
            entries.append((name, packing, orig_size, data_size))

    # pos теперь указывает на начало данных файлов
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
      2. Извлекаем log.txt из .pbo
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
        print(f"    log.txt не найден внутри .pbo")

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

def get_new_replays(
    work_dir: str,
    days_back: int | None = None,
    max_per_run: int | None = None,
) -> list[tuple[str, str]]:
    """
    Находит новые архивы за последние days_back дней и скачивает их.
    Возвращает список (archive_filename, log_txt_path).

    days_back   — читается из env DAYS_BACK  (default: 7)
    max_per_run — читается из env MAX_PER_RUN (default: 20)
    """
    if days_back is None:
        days_back = int(os.environ.get("DAYS_BACK", DEFAULT_DAYS_BACK))
    if max_per_run is None:
        max_per_run = int(os.environ.get("MAX_PER_RUN", DEFAULT_MAX_PER_RUN))

    print("\nDownloader...")
    print(f"  Ограничения: последние {days_back} дней, макс. {max_per_run} за запуск")

    cutoff       = datetime.now(timezone.utc) - timedelta(days=days_back)
    all_archives = list_remote_archives()
    processed    = get_processed_files()

    # Фильтр 1: не обработанные
    candidates = [f for f in all_archives if f not in processed]

    # Фильтр 2: только архивы в пределах days_back (самые свежие — в конце sorted списка)
    dated = []
    skipped_old = 0
    for filename in candidates:
        dt = _parse_archive_date(filename)
        if dt is None or dt >= cutoff:
            dated.append((dt or datetime.min.replace(tzinfo=timezone.utc), filename))
        else:
            skipped_old += 1

    # Сортируем по дате (старые → новые), берём не более max_per_run
    dated.sort(key=lambda x: x[0])
    selected = [filename for _, filename in dated[:max_per_run]]

    print(f"  Всего: {len(all_archives)} | уже обработано: {len(processed)} | "
          f"слишком старых: {skipped_old} | к обработке: {len(selected)}")

    results: list[tuple[str, str]] = []
    for filename in selected:
        archive_dir = os.path.join(work_dir, filename.replace(".pbo.7z", ""))
        os.makedirs(archive_dir, exist_ok=True)

        log_path = download_archive(filename, archive_dir)
        if log_path:
            results.append((filename, log_path))
        else:
            print(f"    Пропускаем {filename} — log.txt не найден")

    return results
