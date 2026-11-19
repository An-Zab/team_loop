# Путь: F:\Projects\team_loop\run_strata_team.py
# Назначение: ШАГ 6 — точка входа в автономный цикл команды для консоли Windows:
#             обычный запуск с текстом задачи, продолжение работы из handoff.md
#             (--resume) и стресс-тест механизма Handoff (--test-handoff) с
#             намеренно заниженным порогом в 800 токенов.
# Кодировка исходника: UTF-8 (в Python 3 она включена по умолчанию).
"""
run_strata_team.py — ШАГ 6: точка входа и режим стресс-теста.

Удобная кнопка «Запустить» для автономного цикла движка Strata
(Аналитик -> Исполнитель <-> Изолированный Ревьюер, механизм Handoff).

Три режима запуска (см. PLAN.md, ШАГ 6, п.1):
  * `python run_strata_team.py "Текст задачи"` — обычный запуск цикла;
  * `python run_strata_team.py --resume` — продолжение работы напрямую из
    `handoff.md` (или резервного `handoff.bak`), текст задачи вводить не нужно;
  * `python run_strata_team.py "Текст задачи" --test-handoff` — стресс-тест:
    порог `HANDOFF_TOKEN_LIMIT` занижается до 800 токенов, чтобы Handoff
    гарантированно сработал сразу после первого же шага и можно было за минуту
    проверить, что `handoff.md` создаётся, а `--resume` его подхватывает.

Сам цикл живёт в `loop_team.py` (ШАГ 5): он печатает номер шага, номер попытки,
строки `[Used: X / Limit: Y]` и вердикт Ревьюера. Эта точка входа добавляет то,
чего у цикла нет: разбор аргументов командной строки, проверку запуска ДО начала
работы (из чего продолжать, что будет перезаписано), печать действующих лимитов
и человеческое резюме итога с кодом возврата.

Почему понижение порога сделано присваиванием переменной. Шаг 1 (`team_config.py`)
читает `HANDOFF_TOKEN_LIMIT` как глобальную переменную модуля в момент вызова
`is_context_critical()`, поэтому `team_config.HANDOFF_TOKEN_LIMIT = 800` действует
сразу для всего цикла и отменяется самим завершением процесса: файл `.env` не
меняется, руками ничего возвращать не нужно.

Совместимость с Windows и защита от падения (правило 2 PLAN.md):
  * pathlib.Path для путей; содержимое файлов эта точка входа не читает и не
    пишет — только проверяет существование и размер handoff.md и handoff.bak;
  * UTF-8 включается в stdout/stderr до импорта модулей проекта, чтобы русское
    сообщение о сломанном импорте было читаемым;
  * ошибки argparse печатаются по-русски с готовыми примерами запуска;
  * запуск без задачи, `--resume` без `handoff.md` и `--resume` вместе с текстом
    задачи останавливаются сразу — локальная модель не тратится впустую;
  * Ctrl+C и любая непредвиденная ошибка завершаются понятным русским текстом и
    кодом возврата, без голого traceback.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# ---------------------------------------------------------------------------
# Каталог проекта, кодировка консоли и импорт модулей Шагов 1-5
# ---------------------------------------------------------------------------

# Скрипт можно запустить из любой папки (python F:\\...\\run_strata_team.py),
# поэтому каталог проекта явно добавляется в sys.path: без этого импорты Шагов 1-5
# работали бы только при запуске из папки проекта.
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))


def _ensure_console_utf8() -> None:
    """Переводит stdout и stderr в UTF-8 (правило 2 PLAN.md).

    Функция своя и намеренно не зависит от `team_config`: вызывается ДО импорта
    модулей проекта, чтобы русское сообщение о неудачном импорте не превратилось
    в крах кодировки консоли (cp866/cp1251) вместо внятной подсказки.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            # Поток подменён чужим объектом или закрыт: печатаем как умеет консоль.
            pass


# Включаем UTF-8 до импорта проекта — сообщение об ошибке импорта должно читаться.
_ensure_console_utf8()

try:
    import team_config
    import loop_team
except ImportError as _import_error:
    # Причина обычно одна: файл лежит не в папке проекта.
    print("[ОШИБКА] не удалось загрузить модули проекта (Шаги 1-5).")
    print(f"Причина: {_import_error}")
    print("Проверь, что рядом с run_strata_team.py лежат team_config.py,")
    print("isolated_reviewer.py, handoff_manager.py, analyst_planner.py и")
    print(f"loop_team.py. Каталог этого файла: {_MODULE_DIR}")
    sys.exit(1)

__all__ = ["main"]

# ---------------------------------------------------------------------------
# Константы точки входа
# ---------------------------------------------------------------------------

# Порог Handoff для режима стресс-теста (--test-handoff) — ровно 800 токенов,
# как требует ТЗ (PLAN.md, ШАГ 6, п.1). Столько не набирает даже короткий
# системный промпт Исполнителя плюс один ответ модели, поэтому Handoff
# срабатывает гарантированно и быстро.
TEST_HANDOFF_TOKEN_LIMIT = 800

# Коды возврата процесса: 0 — успех, 1 — работа не завершена,
# 2 — ошибка в параметрах запуска, 130 — остановлено клавишей Ctrl+C.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

# Ограничение длины текста задачи в приветственном баннере: длинную задачу
# показываем свёрнутой, чтобы баннёр осталось видно целиком.
PRINT_TASK_CHARS = 300


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _as_text(value) -> str:
    """Безопасно приводит любое значение к строке (None -> пустая строка)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value, default: int = 0) -> int:
    """Целое из любого значения или default, если привести не удалось."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truncate(text: str, limit: int) -> str:
    """Сворачивает длинный текст в одну строку для аккуратного вывода в консоль."""
    collapsed = " ".join(_as_text(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + f"... (ещё {len(collapsed) - limit} символов)"


def _config_int(name: str, default: int) -> int:
    """Целочисленная настройка Шага 1 в момент вызова (её можно переопределить)."""
    return _as_int(getattr(team_config, name, default), default)


def _current_handoff_limit() -> int:
    """Действующий порог Handoff с тем же зажимом, что и в Шаге 1.

    Шаг 1 зажимает порог в диапазон 1..MAX_CONTEXT_TOKENS. Повторяем зажим здесь,
    чтобы в баннере и в итоговой строке `[Used: X / Limit: Y]` человек видел ровно
    то число, по которому цикл реально принимает решение, а не желаемое.
    """
    limit = _config_int("HANDOFF_TOKEN_LIMIT", 0)
    maximum = _config_int("MAX_CONTEXT_TOKENS", 0)
    if limit < 1:
        return 0
    if 1 <= maximum < limit:
        return maximum
    return limit


def _handoff_paths() -> tuple[Path, Path]:
    """Пути `handoff.md` и `handoff.bak` так, как их видят Шаги 3-5.

    Относительный путь отсчитывается от каталога модуля (корня проекта), а не от
    текущего каталога процесса — так же, как это делают `handoff_manager.py` и
    `loop_team.py`. Иначе `--resume` искал бы файл не там, где его оставил
    предыдущий запуск.
    """
    paths: list[Path] = []
    config_names = ("HANDOFF_FILE", "HANDOFF_BACKUP")
    fallback_names = ("handoff.md", "handoff.bak")
    for index, name in enumerate(config_names):
        raw = getattr(team_config, name, None)
        text = _as_text(raw).strip()
        if not text:
            paths.append(_MODULE_DIR / fallback_names[index])
            continue
        try:
            path = Path(text)
        except (TypeError, ValueError):
            paths.append(_MODULE_DIR / fallback_names[index])
            continue
        if not path.is_absolute():
            path = _MODULE_DIR / path
        paths.append(path)
    return (paths[0], paths[1])


def _is_file(path: Path) -> bool:
    """Существует ли файл; ошибка доступа не должна ронять проверку запуска."""
    try:
        return path.is_file()
    except OSError:
        # Недопустимый путь или недоступный сетевой диск: считаем, что файла нет.
        return False


def _read_size(path: Path) -> str:
    """Размер файла для вывода в консоль, без падений на недоступном файле."""
    try:
        return f"{path.stat().st_size} байт"
    except OSError:
        return "размер неизвестен"


# ---------------------------------------------------------------------------
# Разбор аргументов командной строки (ШАГ 6, п.1)
# ---------------------------------------------------------------------------

def _print_usage_examples() -> None:
    """Печатает готовые к копированию примеры запуска — их можно вставить как есть."""
    print("Как запускать (текст задачи — в кавычках):")
    print('  python run_strata_team.py "Создай модуль calc.py со сложением и вычитанием"')
    print("  python run_strata_team.py --resume")
    print('  python run_strata_team.py "Собери модуль calc.py" --test-handoff')
    print("  python run_strata_team.py --help")


class _RussianArgumentParser(argparse.ArgumentParser):
    """argparse с русским сообщением об ошибке вместо английской строки.

    Стандартное поведение — напечатать «usage: ... error: unrecognized arguments»
    и выйти с кодом 2. Человеку без навыков отладки такая строка не помогает,
    поэтому к ней добавляются примеры правильного запуска.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        print("[ОШИБКА] не получилось понять аргументы запуска.")
        print(f"Сообщение парсера: {message}")
        print()
        _print_usage_examples()
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    """Собирает параметры запуска: текст задачи, --resume и --test-handoff."""
    parser = _RussianArgumentParser(
        prog="run_strata_team.py",
        description=(
            "Запуск автономного цикла команды Strata: Аналитик -> Исполнитель "
            "<-> Изолированный Ревьюер, с механизмом Handoff."
        ),
        epilog=(
            "Примеры:\n"
            '  python run_strata_team.py "Создай модуль calc.py со сложением и вычитанием"\n'
            "  python run_strata_team.py --resume\n"
            '  python run_strata_team.py "Собери модуль calc.py" --test-handoff\n\n'
            "Нужен запущенный локальный сервер модели (адрес и имя — в файле .env)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "task",
        nargs="*",
        metavar="ТЕКСТ_ЗАДАЧИ",
        help=(
            "Задача для цикла. Лучше писать в кавычках одной строкой; без кавычек "
            "слова всё равно склеятся в одну задачу."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Продолжить работу из handoff.md (или резервного handoff.bak) без текста "
            "задачи."
        ),
    )
    parser.add_argument(
        "--test-handoff",
        dest="test_handoff",
        action="store_true",
        help=(
            f"Стресс-тест Handoff: порог токенов занижается до "
            f"{TEST_HANDOFF_TOKEN_LIMIT}, чтобы handoff.md создался сразу после "
            "первого шага. Файл .env при этом не меняется."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Проверки ДО запуска и стресс-режим
# ---------------------------------------------------------------------------

def _apply_test_handoff_limit() -> None:
    """Занижает `HANDOFF_TOKEN_LIMIT` до 800 токенов на время этого запуска.

    Работает потому, что Шаг 1 читает `HANDOFF_TOKEN_LIMIT` как глобальную
    переменную модуля в момент вызова `is_context_critical()`. После завершения
    процесса значение исчезает само: `.env` и обычные запуски не меняются.
    """
    previous = _current_handoff_limit()
    team_config.HANDOFF_TOKEN_LIMIT = TEST_HANDOFF_TOKEN_LIMIT
    effective = _current_handoff_limit()
    print("\n" + "=" * 62)
    print("РЕЖИМ СТРЕСС-ТЕСТА HANDOFF (--test-handoff)")
    print("=" * 62)
    print(f"Порог Handoff          : {previous} -> {TEST_HANDOFF_TOKEN_LIMIT} токенов")
    print(f"Действующий порог      : {effective} "
          f"(MAX_CONTEXT_TOKENS = {_config_int('MAX_CONTEXT_TOKENS', 0)})")
    print("Это только текущий запуск: файл .env не изменён, после выхода всё вернётся.")
    print("Ожидаемое поведение: цикл выполнит первый шаг, упрётся в порог,")
    print("создаст handoff.md и штатно остановится.")
    print("Примечание: при таком пороге Исполнителю остаётся мало места, поэтому")
    print("он может не успеть записать большой файл — для проверки Handoff это")
    print("нормально, важнее сам факт создания handoff.md и остановки.")
    if effective < TEST_HANDOFF_TOKEN_LIMIT:
        print("[ВНИМАНИЕ] MAX_CONTEXT_TOKENS меньше 800, поэтому порог зажат до")
        print(f"{effective} — Handoff сработает ещё раньше.")


def _check_arguments(task: str, resume: bool) -> str:
    """Проверяет сочетания параметров. Возвращает текст ошибки или пустую строку."""
    if resume and task:
        return (
            "[ОШИБКА] флаг --resume нельзя совмещать с текстом новой задачи.\n"
            "Продолжение берёт исходную задачу из handoff.md. Выбери одно:\n"
            "  продолжить работу : python run_strata_team.py --resume\n"
            '  новая задача        : python run_strata_team.py "Текст задачи"\n'
            "(перед новой задачей уберите старый handoff.md с дороги — иначе цикл\n"
            "продолжит работу из него, а не начнёт новую задачу)"
        )
    if not resume and not task:
        return (
            "[ОШИБКА] не передан текст задачи и не указан флаг --resume.\n"
            "Циклу нечего выполнять, а запуск локальной модели без задачи — это\n"
            "минуты впустую.\n"
        )
    return ""


def _check_state(resume: bool, handoff_path: Path, backup_path: Path) -> str:
    """Проверяет файлы состояния ДО обращения к модели.

    Возвращает текст ошибки, из-за которой запуск бессмысленен, или пустую строку.
    Предупреждения печатает сразу и не мешают работе.
    """
    handoff_exists = _is_file(handoff_path)
    backup_exists = _is_file(backup_path)

    if resume and not handoff_exists and not backup_exists:
        return (
            "[ОШИБКА] продолжать работу не из чего: handoff.md и handoff.bak "
            "не найдены.\n"
            "Проверенные пути:\n"
            f"  {handoff_path}\n"
            f"  {backup_path}\n"
            "Если файл был в другом месте, поправь HANDOFF_FILE в .env. Иначе\n"
            "поставь задачу с нуля:\n"
            '  python run_strata_team.py "Текст задачи"'
        )

    if not resume and handoff_exists:
        print("[ВНИМАНИЕ] в папке проекта уже есть handoff.md "
              f"({_read_size(handoff_path)}).")
        print("Аналитик (ШАГ 4) продолжит работу из него, а текст новой задачи")
        print("будет проигнорирован — так устроен механизм Handoff.")
        print("Начать задачу с нуля можно, убрав файл с дороги, например так:")
        print(f'  Move-Item "{handoff_path}" "{handoff_path}.old"')
        print()

    return ""


# ---------------------------------------------------------------------------
# Логирование запуска и итога (ШАГ 6, п.2)
# ---------------------------------------------------------------------------

def _print_startup_info(task: str, resume: bool, test_handoff: bool,
                        handoff_path: Path, backup_path: Path) -> None:
    """Печатает режим запуска и действующие лимиты до обращения к модели."""
    if resume:
        mode = "продолжение работы из handoff"
    elif test_handoff:
        mode = "обычная задача + стресс-тест Handoff"
    else:
        mode = "обычная задача"

    print("=" * 62)
    print("run_strata_team.py — ШАГ 6: запуск цикла команды")
    print("=" * 62)
    print(f"Режим запуска         : {mode}")
    print(f"Задача                : {_truncate(task or '(из handoff.md)', PRINT_TASK_CHARS)}")
    print(f"MODEL_NAME            : {getattr(team_config, 'MODEL_NAME', '')}")
    print(f"OPENAI_BASE_URL       : {getattr(team_config, 'OPENAI_BASE_URL', '')}")
    print(f"MAX_CONTEXT_TOKENS    : {_config_int('MAX_CONTEXT_TOKENS', 0)}")
    print(f"HANDOFF_TOKEN_LIMIT   : {_current_handoff_limit()}")
    print(f"MAX_STEP_ATTEMPTS     : {_config_int('MAX_STEP_ATTEMPTS', 3)}")
    print(f"handoff.md            : {handoff_path} "
          f"[{'есть, ' + _read_size(handoff_path) if _is_file(handoff_path) else 'нет'}]")
    print(f"handoff.bak           : {backup_path} "
          f"[{'есть, ' + _read_size(backup_path) if _is_file(backup_path) else 'нет'}]")
    print(f"Папка проекта         : {_MODULE_DIR}")
    print("-" * 62)
    print("Исполнитель СОЗДАЁТ И МЕНЯЕТ файлы в папке проекта — это его работа.")
    print("Дальше печатает цикл (ШАГ 5): номер шага, номер попытки,")
    print("строки [Used: X / Limit: Y] и вердикт ревьюера ПРИНЯТО / ДОРАБОТАТЬ.")
    print("Остановка: Ctrl+C.")
    print("-" * 62)


def _print_summary(result: dict) -> int:
    """Печатает итог запуска по-русски и возвращает код возврата процесса."""
    if not isinstance(result, dict):
        print("\n[ОШИБКА] цикл вернул неожиданный результат вместо словаря итога.")
        return EXIT_FAILED

    status = _as_text(result.get("status")) or "unknown"
    completed = _as_int(result.get("completed_steps"), 0)
    total = _as_int(result.get("total_steps"), 0)
    stopped_at = result.get("stopped_at_step")
    saved = bool(result.get("handoff_saved"))
    handoff_path = _as_text(result.get("handoff_path")).strip() or str(_handoff_paths()[0])
    tokens = _as_int(result.get("tokens"), 0)
    limit = _current_handoff_limit()

    print("\n" + "=" * 62)
    print("ИТОГ ЗАПУСКА")
    print("=" * 62)
    print(f"Шагов принято         : {completed} из {total}")
    if stopped_at is not None:
        print(f"Остановка на шаге      : {stopped_at}")
    print(f"Токены основного чата : [Used: {tokens} / Limit: {limit}]")
    if status == "completed":
        print("Результат             : задача выполнена, все шаги приняты ревьюером")
        print(f"Файл состояния        : handoff.md не создавался ({handoff_path})")
    else:
        print(f"Файл состояния        : {handoff_path} "
              f"[{'сохранён' if saved else 'НЕ сохранён'}]")

    if status == "completed":
        print("\nЧто делать дальше: проверь созданные файлы и запусти их, если нужно.")
        return EXIT_OK

    if status == "context_handoff":
        print("\nРезультат             : контекст модели заполнен, работа перенесена")
        if saved:
            print("Это штатная остановка. Продолжи работу той же командой:")
            print("  python run_strata_team.py --resume")
            print("Открой handoff.md — там задача, статус плана и что делать первым.")
            return EXIT_OK
        print("handoff.md НЕ сохранён: причина напечатана выше строками [loop_team].")
        print("Частые причины: файл открыт в Блокноте или неверный HANDOFF_FILE в .env.")
        return EXIT_FAILED

    if status == "attempts_exhausted":
        print("\nРезультат             : шаг не принят ревьюером, попытки исчерпаны")
        print("Открой handoff.md, прочитай раздел «# Текущий контекст и блокер»")
        print("и поставь задачу конкретнее (или меньше по объёму), затем:")
        print('  python run_strata_team.py "Уточнённый текст задачи"')
        return EXIT_FAILED

    print(f"\nРезультат             : работа остановлена из-за ошибки (status={status})")
    print("Подробности — в строках [loop_team] и [analyst_planner] выше.")
    if saved:
        print("Состояние сохранено в handoff.md — можно продолжить: --resume")
    return EXIT_FAILED


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> int:
    """Разбирает аргументы, проверяет запуск и передаёт работу циклу Шага 5."""
    parser = _build_parser()
    args = parser.parse_args()

    # task собран из всех позиционных аргументов: если новичок забыл кавычки,
    # слова «Создай модуль calc.py» всё равно станут одной задачей.
    task = " ".join(_as_text(part) for part in (args.task or [])).strip()
    resume = bool(args.resume)
    test_handoff = bool(args.test_handoff)

    error = _check_arguments(task, resume)
    if error:
        print(error)
        _print_usage_examples()
        return EXIT_USAGE

    handoff_path, backup_path = _handoff_paths()
    error = _check_state(resume, handoff_path, backup_path)
    if error:
        print(error)
        return EXIT_USAGE

    if test_handoff and _is_file(handoff_path):
        print("[ВНИМАНИЕ] режим --test-handoff перезапишет существующий handoff.md.")
        print("Ценное состояние лучше убрать: "
              f'Move-Item "{handoff_path}" "{handoff_path}.old"')
        print()

    _print_startup_info(task, resume, test_handoff, handoff_path, backup_path)

    if test_handoff:
        _apply_test_handoff_limit()

    try:
        result = loop_team.run_team_loop(task, resume_backup=resume)
    except KeyboardInterrupt:
        print("\n[ПРЕРВАНО] работа остановлена клавишей Ctrl+C.")
        print("Новый handoff.md не создан. Ранее сохранённый handoff.md (или")
        print("handoff.bak) остался на диске — его подхватит запуск с --resume.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # последняя страховка: без голого traceback
        print("\n[ОШИБКА] запуск завершился непредвиденной ошибкой: "
              f"{type(exc).__name__}: {_truncate(_as_text(exc), 800)}")
        print(f"Проверь, не остался ли сохранённый handoff.md: {handoff_path}")
        return EXIT_FAILED

    code = _print_summary(result)

    if test_handoff:
        print("\n" + "-" * 62)
        print("СТРЕСС-ТЕСТ ЗАВЕРШЁН.")
        print(f"1. Открой handoff.md — он должен существовать: {handoff_path}")
        print("2. Проверь продолжение работы командой:")
        print("     python run_strata_team.py --resume")
        print("3. Обычные лимиты вернутся сами: просто запусти без --test-handoff.")
        print("-" * 62)

    return code


if __name__ == "__main__":
    # UTF-8 включён выше, до импорта модулей проекта (см. _ensure_console_utf8).
    sys.exit(main())
