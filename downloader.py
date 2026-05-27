"""
TSGstats — Downloader
Проверяет сайт с реплеями на новые .pbo.7z файлы,
скачивает и распаковывает log.txt из каждого нового архива.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import py7zr

REPLAYS_URL = "https://replays.tsgames.ru/replays/"


# ── Список файлов на сайте ────────────────────────────────────────────────────

def list_remote_archives() -> list[str]:
    """Возвращает имена .pbo.7z файлов с сайта реплеев."""
    with httpx.Client(timeout=30) as client:
        resp = client.get(REPLAYS_URL)
        resp.raise_for_status()

    # nginx directory listing: href="filename.pbo.7z"
    names = re.findall(r'href="([^"/]+\.pbo\.7z)"', resp.text)
    return sorted(set(names))


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


# ── Скачивание и распаковка ───────────────────────────────────────────────────

def _extract_log(archive_path: str, dest_dir: str) -> str | None:
    """Извлекает log.txt из 7z-архива. Возвращает путь или None."""
    with py7zr.SevenZipFile(archive_path, mode="r") as z:
        names = z.getnames()
        # Ищем файл с "log" в имени (log.txt, arma.log и т.п.)
        log_name = next(
            (n for n in names if Path(n).name.lower() == "log.txt"),
            next((n for n in names if "log" in Path(n).name.lower() and n.endswith(".txt")), None),
        )
        if not log_name:
            print(f"    log.txt не найден в архиве. Содержимое: {names}")
            return None
        z.extract(targets=[log_name], path=dest_dir)

    extracted = os.path.join(dest_dir, log_name)
    return extracted if os.path.exists(extracted) else None


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

def get_new_replays(work_dir: str) -> list[tuple[str, str]]:
    """
    Находит новые архивы и скачивает их.
    Возвращает список (archive_filename, log_txt_path).
    """
    print("\nDownloader...")

    all_archives = list_remote_archives()
    processed    = get_processed_files()
    new_archives = [f for f in all_archives if f not in processed]

    print(f"  Всего архивов: {len(all_archives)}, новых: {len(new_archives)}")

    results: list[tuple[str, str]] = []
    for filename in new_archives:
        # Каждый архив распаковываем в отдельную папку
        archive_dir = os.path.join(work_dir, filename.replace(".pbo.7z", ""))
        os.makedirs(archive_dir, exist_ok=True)

        log_path = download_archive(filename, archive_dir)
        if log_path:
            results.append((filename, log_path))
        else:
            print(f"    Пропускаем {filename} — log.txt не найден")

    return results
