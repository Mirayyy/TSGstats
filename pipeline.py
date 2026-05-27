"""
TSGstats — Pipeline Orchestrator
Запускает полный pipeline для каждого нового реплея:
  downloader → parser → entity_resolver → attribution_engine
             → stats_calculator → supabase_writer
"""

from __future__ import annotations

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


def process_one(archive_name: str, log_path: str) -> bool:
    """Прогоняет один log.txt через все слои pipeline."""
    work_dir = os.path.dirname(log_path)   # файлы пишем рядом с log.txt

    try:
        parse(log_path, work_dir)
        resolve(work_dir, work_dir)
        attribute(work_dir, work_dir)
        calculate(work_dir, work_dir)
        write(work_dir)
        return True
    except Exception as e:
        print(f"  ОШИБКА при обработке {archive_name}: {e}")
        import traceback; traceback.print_exc()
        return False


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


if __name__ == "__main__":
    # Проверяем наличие переменных окружения
    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
    if missing:
        print(f"ОШИБКА: не заданы переменные окружения: {', '.join(missing)}")
        sys.exit(1)

    run()
