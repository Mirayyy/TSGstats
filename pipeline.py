"""
TSGstats — Pipeline Orchestrator

Режимы запуска:

  Стандартный (GitHub Actions / cron):
    python pipeline.py

  Локальный файл (отладка, ручной фикс):
    python pipeline.py --local /path/to/log.txt [--server T1]

  Переобработка конкретного архива:
    1. DELETE FROM processed_replays WHERE filename = 'T1.2026-...pbo.7z';
    2. python pipeline.py
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime

from downloader import get_new_replays, mark_processed
from parser import parse
from entity_resolver import resolve
from attribution_engine import attribute
from stats_calculator import calculate
from supabase_writer import write


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _server_from_name(archive_name: str) -> str:
    """
    Извлекает имя сервера из имени архива.
    'T1.2026-05-20-...' → 'T1'
    'T2.2026-...'       → 'T2'
    Возвращает '' если не распознано.
    """
    prefix = archive_name.split(".")[0]
    return prefix if prefix and prefix[0] == "T" and prefix[1:].isdigit() else ""


# ── Основной шаг pipeline ─────────────────────────────────────────────────────

def process_one(archive_name: str, log_path: str, server: str = "") -> bool:
    """Прогоняет один log.txt через все слои pipeline."""
    work_dir = os.path.dirname(log_path)

    if not server:
        server = _server_from_name(archive_name)

    try:
        parse(log_path, work_dir, server=server)
        resolve(work_dir, work_dir)
        attribute(work_dir, work_dir)
        calculate(work_dir, work_dir)
        write(work_dir)
        return True
    except Exception as e:
        print(f"  ОШИБКА при обработке {archive_name}: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Режим 1: стандартный (скачивает новые реплеи) ─────────────────────────────

def run() -> None:
    print(f"\n{'='*60}")
    print(f"TSGstats Pipeline  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    with tempfile.TemporaryDirectory(prefix="tsgstats_") as tmp:
        replays = get_new_replays(tmp)

        if not replays:
            print("\nНовых реплеев нет. Выход.")
            return

        ok = fail = 0
        for archive_name, log_path in replays:
            print(f"\n--- {archive_name} ---")
            if process_one(archive_name, log_path):
                mark_processed(archive_name, status="ok")
                ok += 1
            else:
                # Помечаем как обработанный даже при ошибке —
                # сломанный архив будет сломан всегда, не стоит ретраить.
                mark_processed(archive_name, status="error")
                fail += 1

    print(f"\n{'='*60}")
    print(f"Итог: {ok} успешно обработано, {fail} с ошибками")


# ── Режим 2: локальный файл ───────────────────────────────────────────────────

def run_local(log_path: str, server: str = "") -> None:
    """
    Обрабатывает один локальный log.txt.
    НЕ обращается к processed_replays — не помечает архив как обработанный.
    Полезно для отладки и ручного исправления конкретных игр.
    """
    log_path = os.path.abspath(log_path)
    if not os.path.exists(log_path):
        print(f"ОШИБКА: файл не найден: {log_path}")
        sys.exit(1)

    # Имя архива берём из папки, в которой лежит log.txt
    archive_name = os.path.basename(os.path.dirname(log_path))

    print(f"\n{'='*60}")
    print(f"TSGstats Pipeline (локальный файл)")
    print(f"{'='*60}")
    print(f"\n  Файл:   {log_path}")
    print(f"  Архив:  {archive_name}")
    print(f"  Сервер: {server or _server_from_name(archive_name) or '(не определён)'}")

    success = process_one(archive_name, log_path, server=server)
    sys.exit(0 if success else 1)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TSGstats Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
примеры:
  # Стандартный запуск (новые реплеи с сайта):
  python pipeline.py

  # Обработать локальный log.txt:
  python pipeline.py --local /path/to/log.txt
  python pipeline.py --local /path/to/log.txt --server T1

  # Переобработать конкретный архив:
  #   1. В Supabase SQL: DELETE FROM processed_replays WHERE filename = 'T1.2026-...pbo.7z';
  #   2. python pipeline.py

  # Переобработать всё (после обновления правил):
  #   1. В Supabase SQL: TRUNCATE TABLE processed_replays;
  #   2. python pipeline.py  (или с DAYS_BACK=30 для истории)
        """,
    )
    parser.add_argument(
        "--local", metavar="LOG_PATH",
        help="Путь к локальному log.txt (пропускает скачивание и processed_replays)",
    )
    parser.add_argument(
        "--server", metavar="NAME", default="",
        help="Имя сервера (T1/T2/T3), используется с --local если не распознаётся из пути",
    )
    args = parser.parse_args()

    # Supabase-переменные нужны только для стандартного режима
    if not args.local:
        missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
        if missing:
            print(f"ОШИБКА: не заданы переменные окружения: {', '.join(missing)}")
            sys.exit(1)
        run()
    else:
        # В локальном режиме Supabase всё равно нужен для записи результатов.
        # Если переменные не заданы — write() упадёт, но parse/resolve/attribute/calculate
        # отработают и результаты останутся в папке рядом с log.txt.
        run_local(args.local, server=args.server)
