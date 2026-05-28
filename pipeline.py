"""
TSGstats — Pipeline Orchestrator

Режимы запуска:

  Стандартный (GitHub Actions / cron):
    python pipeline.py

  Скачать архив локально (без обработки):
    python pipeline.py --fetch "T1.2026-05-20-20-27-54.mTSG%4016_Plane_Dogfight_v4.chernarus.pbo.7z"
    python pipeline.py --fetch "T1.2026-..." --out D:/Downloads/replays

  Обработать локальный log.txt:
    python pipeline.py --local ./fetched/.../log.txt [--server T1]

  Переобработка конкретного архива:
    1. DELETE FROM processed_replays WHERE filename = 'T1.2026-...pbo.7z';
    2. python pipeline.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

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

    if not server:
        print(f"  ПРЕДУПРЕЖДЕНИЕ: не удалось определить сервер из '{archive_name}'. "
              f"Поле server будет пустым в базе данных.")

    try:
        parse(log_path, work_dir, server=server)
        resolve(work_dir, work_dir)
        attribute(work_dir, work_dir)
        calculate(work_dir, work_dir)

        # ── Фильтр по реальным игрокам (после calculate, данные в game_stats.json) ──
        # meta.json не содержит player_count — реальный состав известен только после
        # полной обработки. Используем len(players) — тот же источник, что supabase_writer.
        _min_pl_raw = os.environ.get("FILTER_MIN_PLAYERS", "").strip()
        if _min_pl_raw.isdigit():
            _min_pl = int(_min_pl_raw)
            if _min_pl > 0:
                try:
                    with open(os.path.join(work_dir, "game_stats.json"), encoding="utf-8") as _f:
                        _gs = json.load(_f)
                    _actual = len(_gs.get("players", []))
                    if _actual < _min_pl:
                        print(f"  Пропускаем: {_actual} реальных игроков < "
                              f"минимум {_min_pl}. Архив помечен как обработанный.")
                        return True   # не ошибка — просто пропуск
                except Exception as _e:
                    print(f"  WARNING: не удалось прочитать player_count из game_stats.json: {_e}")

        write(work_dir)
        return True
    except Exception as e:
        print(f"  ОШИБКА при обработке {archive_name}: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Режим 1: стандартный (скачивает новые реплеи) ─────────────────────────────

def run(
    servers: list[str] | None = None,
    mission_types: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> None:
    print(f"\n{'='*60}")
    print(f"TSGstats Pipeline  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    with tempfile.TemporaryDirectory(prefix="tsgstats_") as tmp:
        replays = get_new_replays(
            tmp,
            servers=servers,
            mission_types=mission_types,
            date_from=date_from,
            date_to=date_to,
        )

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


# ── Режим 2: скачать без обработки ───────────────────────────────────────────

def run_fetch(archive_name: str, out_dir: str = "fetched") -> None:
    """
    Скачивает один архив с сайта реплеев и извлекает log.txt — без обработки.
    Удобно для локальной отладки: получаешь файл, правишь правила, запускаешь --local.
    """
    from downloader import download_archive

    out_dir = os.path.abspath(out_dir)
    dest = os.path.join(out_dir, archive_name.replace(".pbo.7z", ""))
    os.makedirs(dest, exist_ok=True)

    print(f"\nСкачиваем: {archive_name}")
    log_path = download_archive(archive_name, dest)

    if log_path:
        print(f"\nlog.txt сохранён: {log_path}")
        print(f"\nЗапустить обработку:")
        print(f'  python pipeline.py --local "{log_path}"')
    else:
        print("ОШИБКА: не удалось извлечь log.txt")
        sys.exit(1)


# ── Режим 3: локальный файл ───────────────────────────────────────────────────

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
        "--fetch", metavar="ARCHIVE_NAME",
        help="Скачать конкретный архив с сайта и сохранить log.txt локально (без обработки)",
    )
    parser.add_argument(
        "--out", metavar="DIR", default="fetched",
        help="Папка для --fetch (по умолчанию: ./fetched)",
    )
    parser.add_argument(
        "--local", metavar="LOG_PATH",
        help="Путь к локальному log.txt (пропускает скачивание и processed_replays)",
    )
    parser.add_argument(
        "--server", metavar="NAME", default="",
        help="Имя сервера (T1/T2/T3/T4), используется с --local если не распознаётся из пути",
    )
    parser.add_argument(
        "--servers", metavar="T1,T2,...", default="",
        help="Фильтр по серверу через запятую (T1,T2,...). Пусто = все серверы.",
    )
    parser.add_argument(
        "--types", metavar="mTSG,TSG", default="",
        help="Фильтр по типу миссии через запятую (mTSG,TSG,...). Пусто = все типы.",
    )
    parser.add_argument(
        "--min-players", metavar="N", type=int, default=0,
        help="Минимальное кол-во реальных игроков (0 = без ограничения).",
    )
    parser.add_argument(
        "--date-from", metavar="YYYY-MM-DD", default="",
        help="Обрабатывать архивы не старше этой даты (заменяет --days-back).",
    )
    parser.add_argument(
        "--date-to", metavar="YYYY-MM-DD", default="",
        help="Обрабатывать архивы не новее этой даты.",
    )
    args = parser.parse_args()

    if args.fetch:
        run_fetch(args.fetch, out_dir=args.out)
    elif args.local:
        # В локальном режиме Supabase всё равно нужен для записи результатов.
        # Если переменные не заданы — write() упадёт, но parse/resolve/attribute/calculate
        # отработают и результаты останутся в папке рядом с log.txt.
        run_local(args.local, server=args.server)
    else:
        missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY") if not os.environ.get(v)]
        if missing:
            print(f"ОШИБКА: не заданы переменные окружения: {', '.join(missing)}")
            sys.exit(1)
        servers = [s.strip() for s in args.servers.split(",") if s.strip()] or None
        types   = [t.strip() for t in args.types.split(",")   if t.strip()] or None

        # min_players — через env (проверяется в process_one() после parse())
        if args.min_players > 0:
            os.environ["FILTER_MIN_PLAYERS"] = str(args.min_players)

        # Дата от/до
        def _parse_arg_date(s: str) -> datetime | None:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            except ValueError:
                print(f"ОШИБКА: неверный формат даты {s!r} (ожидается YYYY-MM-DD)")
                sys.exit(1)

        run(
            servers=servers,
            mission_types=types,
            date_from=_parse_arg_date(args.date_from),
            date_to=_parse_arg_date(args.date_to),
        )
