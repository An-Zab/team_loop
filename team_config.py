# -*- coding: utf-8 -*-
"""
team_config.py — ШАГ 1: конфигурация и утилиты подсчёта токенов контекста.

Часть автономного цикла loop_team движка Strata
(Аналитик -> Исполнитель <-> Изолированный Ревьюер, механизм Handoff).

Возможности (см. PLAN.md, ШАГ 1):
  * Загрузка настроек из `.env` (`OPENAI_BASE_URL`, `OPENAI_API_KEY`,
    `MODEL_NAME`) с дефолтными значениями; лимиты переопределяются через `.env`.
  * Пороги токенов для механизма Handoff и максимум попыток одного шага.
  * extract_used_tokens()  — получение использованных токенов из ответа сервера
    (с приближённым подсчётом по сообщениям, если сервер не вернул `usage`).
  * is_context_critical()  — сигнал о том, что контекст приближается к Handoff.

Совместимость с Windows:
  * pathlib.Path, явный encoding="utf-8" при записи файлов;
  * UTF-8 принудительно включается и для stdout, и для stderr при импорте
    модуля (перенаправленный в лог поток наследует кодовую страницу локали);
  * .env читается устойчиво к кодировке: UTF-8 (с BOM и без) или UTF-16 с BOM
    (его даёт `> .env` в Windows PowerShell 5.1). Файл в другой кодировке не
    роняет импорт, а даёт предупреждение в stderr и дефолтные значения;
  * файлы состояния Handoff привязаны к каталогу модуля, а не к текущему
    каталогу процесса, поэтому `handoff.md` не «едет» вместе с cwd.
"""

from __future__ import annotations

import codecs
import io
import os
import sys
from pathlib import Path

__all__ = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "MODEL_NAME",
    "MAX_CONTEXT_TOKENS",
    "HANDOFF_FREE_RATIO",
    "HANDOFF_TOKEN_LIMIT",
    "MAX_STEP_ATTEMPTS",
    "TOKEN_ESTIMATE_DIVISOR",
    "HANDOFF_FILE",
    "HANDOFF_BACKUP",
    "extract_used_tokens",
    "is_context_critical",
]

# ---------------------------------------------------------------------------
# Потоки вывода и загрузка .env (без тяжёлых зависимостей).
#
# Единый источник истины: путь к .env ищем сами (cwd, затем каталог модуля),
# байты читаем и декодируем ОДНИМ и тем же кодом. Если установлен
# python-dotenv, он получает уже готовый ТЕКСТ через stream=, а не путь: иначе
# поиск и декодирование выполняет он по своим правилам, и поведение
# конфигурации молча зависело бы от наличия необязательной зависимости.
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent

# BOM UTF-16 — то, с чего начинает байты `echo "K=v" > .env` в PowerShell 5.1.
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def _ensure_utf8_streams() -> None:
    """Включает UTF-8 в stdout и stderr (правило 2 PLAN.md).

    Вызывается на уровне модуля и до первого вывода: предупреждения о битых
    значениях конфигурации печатаются ещё во время импорта, а перенаправленный
    поток (`python team_config.py > log.txt 2>&1`) наследует кодовую страницу
    локали (cp437/cp1252/cp1251), где кириллица роняет print с
    UnicodeEncodeError — сообщение об ошибке конфигурации было бы хуже самой
    ошибки. errors="backslashreplace" страхует от этого второй раз.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        encoding = str(getattr(stream, "encoding", "") or "").replace("-", "").lower()
        if encoding == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            # Поток подменён чужим объектом, закрыт или не переконфигурируется.
            pass


def _warn(message: str) -> None:
    """Пишет предупреждение в stderr и никогда не бросает исключение."""
    line = f"[team_config] {message}\n"
    stream = sys.stderr
    try:
        stream.write(line)
        stream.flush()
        return
    except (UnicodeEncodeError, ValueError, OSError):
        pass
    # UTF-8 включить не удалось: выводим ASCII-безопасный вариант с \u-экраниками.
    safe = line.encode("ascii", errors="backslashreplace").decode("ascii")
    try:
        stream.write(safe)
        stream.flush()
    except (UnicodeEncodeError, ValueError, OSError):
        pass


def _clean_value(raw: str) -> str:
    """Приводит правую часть строки `KEY=VALUE` к значению.

    Снимает кавычки и хвостовой комментарий — намеренно так же, как
    python-dotenv: иначе одна и та же строка `MAX_CONTEXT_TOKENS=8192  # ок`
    давала бы 8192 на машине с установленным dotenv и «не является целым
    числом» + дефолт на машине без него.
    """
    value = raw.strip()
    if value[:1] in {"'", '"'}:
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            # Всё после закрывающей кавычки, включая «# комментарий», не значение.
            return value[1:end]
        return value[1:]  # незакрытая кавычка — берём остальное до конца строки
    head, sep, _ = value.partition(" #")
    return head.rstrip() if sep else value


def _decode_env_text(data: bytes, source: Path) -> "str | None":
    """Декодирует содержимое .env или возвращает None с предупреждением.

    utf-8-sig читает и обычный UTF-8, и «Сохранить как… UTF-8» из Блокнота:
    без -sig BOM оставался бы в первой строке и превращал первую переменную в
    ключ «\\ufeffOPENAI_BASE_URL» — реальная настройка не подхватывалась бы, и
    приложение молча уходило на дефолты. UTF-16 пробуем только при наличии BOM.

    Прочие кодировки (CP1251 и т. п.) НЕ угадываем: строгий decode чужой
    кодировки почти всегда «удался» бы и дал бы испорченные ключи и значения,
    то есть ту же тихую поломку. Лучше громкое предупреждение и дефолты.
    """
    encodings = ("utf-16", "utf-8-sig") if data.startswith(_UTF16_BOMS) else ("utf-8-sig",)
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            # UTF-16/UTF-32 без BOM по байтам выглядит как валидный UTF-8 с NUL,
            # а os.environ такие имена и значения не принимает в принципе.
            _warn(
                f"{source} похож на UTF-16/UTF-32 без BOM. Читаю UTF-8 или UTF-16 "
                f"с BOM; файл пропущен, используются дефолтные значения."
            )
            return None
        # Декодер снимает ровно один BOM; «пересохранённые» файлы (UTF-8+BOM,
        # затем пересозданные в UTF-16) оставляют второй BOM уже обычным
        # символом в начале текста — он снова стал бы частью имени первой
        # переменной.
        return text.lstrip("\ufeff")
    _warn(
        f"{source} не читается как UTF-8 или UTF-16 с BOM: файл пропущен, "
        f"используются дефолтные значения. Сохраните .env в UTF-8 (в Windows "
        f"PowerShell 5.1 оператор > пишет UTF-16 — используйте "
        f"Out-File -Encoding utf8 или редактор с выбором кодировки)."
    )
    return None


def _parse_env_text(text: str) -> dict:
    """Минимальный парсер .env: строки вида KEY=VALUE, комментарии через #.

    Грамматика совпадает с python-dotenv в реальных случаях (кавычки,
    `export`, хвостовые комментарии, пустые значения), чтобы наличие или
    отсутствие необязательной зависимости не меняло результат.
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        key = key.strip()
        if key:
            result[key] = _clean_value(value)
    return result


def _find_env_file() -> "Path | None":
    """Ищет .env одинаково для обеих веток: сначала cwd, затем каталог модуля."""
    for directory in (Path.cwd(), _MODULE_DIR):
        candidate = directory / ".env"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # Невозможно статить каталог (сетевой диск, права) — идём дальше.
            continue
    return None


def _apply_with_dotenv(source: Path, text: str) -> bool:
    """Прогоняет уже прочитанный НАМИ текст через python-dotenv, если он стоит.

    Путь dotenv не передаём: find_dotenv искал бы файл заново по своей
    стратегии (каталог модуля веткой не гарантирован) и декодировал бы байты
    по своей кодировке. Здесь же обе ветки получают один и тот же текст.
    override=False сохраняет приоритет окружения над файлом,
    interpolate=False совпадает с поведением встроенного парсера.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return False

    # Разные версии dotenv имеют разный набор параметров.
    for kwargs in ({"override": False, "interpolate": False}, {"override": False}, {}):
        try:
            load_dotenv(stream=io.StringIO(text), **kwargs)
        except TypeError:
            continue
        except (OSError, ValueError) as exc:
            _warn(f"python-dotenv не смог разобрать {source}: {exc}; "
                  f"использую встроенный парсер.")
            return False
        return True
    return False


def _load_dotenv() -> None:
    """Загружает первый найденный .env в os.environ, не роняя импорт модуля.

    Уже установленные переменные окружения НЕ перезаписываются
    (стандартная семантика dotenv override=False: окружение важнее файла).
    Битая кодировка или недоступный файл — предупреждение в stderr и дефолты:
    критерий готовности Шага 1 требует, чтобы модуль импортировался без ошибок.
    """
    source = _find_env_file()
    if source is None:
        return
    try:
        data = source.read_bytes()
    except OSError as exc:
        _warn(f"Не удалось прочитать {source}: {exc.strerror or exc}; "
              f"использую дефолтные значения.")
        return

    text = _decode_env_text(data, source)
    if text is None:
        return
    if _apply_with_dotenv(source, text):
        return
    for key, value in _parse_env_text(text).items():
        os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Чтение типизированных значений из окружения с дефолтами
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _warn(f"Переменная {name}={raw!r} не является целым числом; "
              f"использую дефолт {default}.")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        _warn(f"Переменная {name}={raw!r} не является числом; "
              f"использую дефолт {default}.")
        return default


def _env_path(name: str, default: str) -> Path:
    """Путь из окружения; относительные значения привязываем к каталогу модуля.

    Иначе файл состояния Handoff следует за текущим каталогом процесса:
    `cd F:\\ && python F:\\Projects\\team_loop\\run_strata_team.py --resume`
    искал бы handoff.md не там, где его оставил предыдущий запуск, и --resume
    молча начал бы новую задачу (выглядело бы как потеря состояния).
    Абсолютный путь из .env остаётся абсолютным.
    """
    value = Path(_env_str(name, default))
    if not value.is_absolute():
        value = _MODULE_DIR / value
    return value.resolve()


# ---------------------------------------------------------------------------
# Константы конфигурации (переопределяются через .env / переменные окружения)
# ---------------------------------------------------------------------------

# UTF-8 в stdout/stderr включается до первого вывода: предупреждения ниже
# печатаются в stderr ещё на этапе импорта модуля.
_ensure_utf8_streams()
_load_dotenv()

# Подключение к локальному серверу модели (по умолчанию — стиль LM Studio).
OPENAI_BASE_URL: str = _env_str("OPENAI_BASE_URL", "http://localhost:1234/v1")
OPENAI_API_KEY: str = _env_str("OPENAI_API_KEY", "EMPTY")
MODEL_NAME: str = _env_str("MODEL_NAME", "qwen-local")

# Дефолты, к которым возвращаемся при некорректных значениях в .env.
_DEFAULT_MAX_CONTEXT_TOKENS = 32768
_DEFAULT_MAX_STEP_ATTEMPTS = 3

# Полный размер контекстного окна локальной модели.
MAX_CONTEXT_TOKENS: int = _env_int("MAX_CONTEXT_TOKENS", _DEFAULT_MAX_CONTEXT_TOKENS)
if MAX_CONTEXT_TOKENS <= 0:
    _warn(f"MAX_CONTEXT_TOKENS={MAX_CONTEXT_TOKENS} должно быть больше 0; "
          f"использую дефолт {_DEFAULT_MAX_CONTEXT_TOKENS}.")
    MAX_CONTEXT_TOKENS = _DEFAULT_MAX_CONTEXT_TOKENS

# Доля контекста, которую нужно оставить свободной (порог срабатывания Handoff).
HANDOFF_FREE_RATIO: float = _env_float("HANDOFF_FREE_RATIO", 0.25)
if not 0.0 < HANDOFF_FREE_RATIO < 1.0:
    _warn(f"HANDOFF_FREE_RATIO={HANDOFF_FREE_RATIO} вне диапазона (0; 1); "
          f"использую дефолт 0.25.")
    HANDOFF_FREE_RATIO = 0.25


def _handoff_limit_from_ratio() -> int:
    """Порог Handoff по формуле ТЗ: int(MAX * (1 - HANDOFF_FREE_RATIO))."""
    return min(max(int(MAX_CONTEXT_TOKENS * (1.0 - HANDOFF_FREE_RATIO)), 1),
               MAX_CONTEXT_TOKENS)


# Расчётный порог токенов: при его достижении пора делать Handoff.
# Может быть явно переопределён в .env (например, для стресс-теста Шага 6).
HANDOFF_TOKEN_LIMIT: int = _env_int(
    "HANDOFF_TOKEN_LIMIT",
    _handoff_limit_from_ratio(),
)
if not 1 <= HANDOFF_TOKEN_LIMIT <= MAX_CONTEXT_TOKENS:
    _fixed_limit = _handoff_limit_from_ratio()
    # Порог 0 означал бы «контекст критичен всегда»: цикл Шага 5 уходил бы в
    # Handoff до первого вызова Исполнителя, а --resume зацикливал бы
    # handoff.md -> план -> снова критично. Порог больше окна наоборот означал
    # бы, что Handoff не сработает никогда и контекст переполнится ошибкой
    # сервера.
    _warn(f"HANDOFF_TOKEN_LIMIT={HANDOFF_TOKEN_LIMIT} вне осмысленного диапазона "
          f"1..{MAX_CONTEXT_TOKENS} (MAX_CONTEXT_TOKENS); пересчитываю по формуле: "
          f"{_fixed_limit}.")
    HANDOFF_TOKEN_LIMIT = _fixed_limit

# Максимум попыток исправления одного шага плана.
MAX_STEP_ATTEMPTS: int = _env_int("MAX_STEP_ATTEMPTS", _DEFAULT_MAX_STEP_ATTEMPTS)
if MAX_STEP_ATTEMPTS < 1:
    _warn(f"MAX_STEP_ATTEMPTS={MAX_STEP_ATTEMPTS} должно быть не меньше 1; "
          f"использую дефолт {_DEFAULT_MAX_STEP_ATTEMPTS}.")
    MAX_STEP_ATTEMPTS = _DEFAULT_MAX_STEP_ATTEMPTS

# Делитель для запасной оценки числа токенов (len(text) // N), когда сервер не
# вернул usage. По умолчанию 3 — как требует ТЗ (PLAN.md, ШАГ 1, п.3).
# Для преимущественно кириллического контекста тройка систематически занижает
# оценку (кириллица в современных BPE-токенизаторах дороже латиницы), из-за
# чего Handoff вызывается позже реального заполнения окна; для русских промптов
# реалистичнее 2.
_DEFAULT_TOKEN_ESTIMATE_DIVISOR = 3
TOKEN_ESTIMATE_DIVISOR: int = _env_int(
    "TOKEN_ESTIMATE_DIVISOR", _DEFAULT_TOKEN_ESTIMATE_DIVISOR
)
if not 1 <= TOKEN_ESTIMATE_DIVISOR <= 16:
    _warn(f"TOKEN_ESTIMATE_DIVISOR={TOKEN_ESTIMATE_DIVISOR} вне диапазона 1..16; "
          f"использую дефолт {_DEFAULT_TOKEN_ESTIMATE_DIVISOR}.")
    TOKEN_ESTIMATE_DIVISOR = _DEFAULT_TOKEN_ESTIMATE_DIVISOR

# Файлы состояния механизма Handoff (абсолютные; см. _env_path).
HANDOFF_FILE: Path = _env_path("HANDOFF_FILE", "handoff.md")
HANDOFF_BACKUP: Path = _env_path("HANDOFF_BACKUP", "handoff.bak")


# ---------------------------------------------------------------------------
# Подсчёт использованных токенов
# ---------------------------------------------------------------------------

def _extract_total_tokens(api_response) -> "int | None":
    """Пытается достать usage.total_tokens из ответа API-сервера.

    Поддерживает dict-ответ (OpenAI-совместимый JSON), объекты OpenAI SDK
    (response.usage) и AIMessage-style объекты (usage_metadata).
    Возвращает None, если usage отсутствует или некорректен.
    """
    if api_response is None:
        return None

    usage = None
    if isinstance(api_response, dict):
        usage = api_response.get("usage") or api_response.get("usage_metadata")
    else:
        usage = getattr(api_response, "usage", None)
        if usage is None:
            usage = getattr(api_response, "usage_metadata", None)

    if usage is None:
        return None

    if isinstance(usage, dict):
        total = usage.get("total_tokens")
    else:
        total = getattr(usage, "total_tokens", None)

    try:
        total = int(total)
    except (TypeError, ValueError):
        return None
    return total if total > 0 else None


def _content_to_text(content) -> str:
    """Приводит поле content сообщения к одной строке текста."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        # OpenAI-стиль: [{"type": "text", "text": "..."}, ...]
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return str(content)


def _message_content_text(message) -> str:
    """Извлекает текстовое содержимое одного сообщения чата.

    Понимает str, dict {"role": ..., "content": ...},
    кортежи ("role", "content") и объекты с атрибутом .content.
    """
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return _content_to_text(message.get("content", ""))
    if isinstance(message, (tuple, list)):
        # Формат ("role", "content"): содержимое — последний непустой элемент.
        for part in reversed(message):
            text = _content_to_text(part)
            if text:
                return text
        return ""
    return _content_to_text(getattr(message, "content", message))


def _estimate_divisor() -> int:
    """Делитель запасной оценки в момент вызова.

    TOKEN_ESTIMATE_DIVISOR — глобальная переменная модуля, её можно
    переопределить на лету (как HANDOFF_TOKEN_LIMIT), поэтому проверка на
    месте: ноль или мусор не должны уронить подсчёт делением на ноль.
    """
    divisor = TOKEN_ESTIMATE_DIVISOR
    if not isinstance(divisor, int) or divisor < 1:
        return _DEFAULT_TOKEN_ESTIMATE_DIVISOR
    return divisor


def extract_used_tokens(api_response, messages_fallback: list) -> int:
    """Возвращает число токенов, занятых текущим контекстом.

    1) Пытается достать `usage.total_tokens` из ответа локального сервера.
    2) Если сервер не вернул `usage` — считает приближённо по массиву
       messages_fallback: суммарная длина всех строк контента, делённая на
       TOKEN_ESTIMATE_DIVISOR (по умолчанию 3 — как в ТЗ; для русскоязычного
       контекста ближе 2, иначе оценка стабильно занижена).
    """
    total = _extract_total_tokens(api_response)
    if total is not None:
        return total

    total_chars = 0
    for message in messages_fallback or []:
        total_chars += len(_message_content_text(message))
    return total_chars // _estimate_divisor()


def is_context_critical(current_tokens: int) -> bool:
    """True, если использовано уже не меньше HANDOFF_TOKEN_LIMIT токенов.

    Константа читается как глобальная переменная модуля в момент вызова,
    поэтому её можно переопределить (например, для стресс-теста Handoff).
    """
    return current_tokens >= HANDOFF_TOKEN_LIMIT


# ---------------------------------------------------------------------------
# Точка входа: вывод текущих лимитов (критерий готовности Шага 1)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # UTF-8 для stdout и stderr уже включён в _ensure_utf8_streams() при импорте.

    print("=" * 62)
    print("team_config.py — ШАГ 1: конфигурация цикла loop_team")
    print("=" * 62)
    print(f"MODEL_NAME            : {MODEL_NAME}")
    print(f"OPENAI_BASE_URL       : {OPENAI_BASE_URL}")
    print(f"OPENAI_API_KEY        : {'(задан)' if OPENAI_API_KEY else '(пуст)'}")
    print("-" * 62)
    print(f"MAX_CONTEXT_TOKENS    : {MAX_CONTEXT_TOKENS}")
    print(f"HANDOFF_FREE_RATIO    : {HANDOFF_FREE_RATIO}")
    print(f"HANDOFF_TOKEN_LIMIT   : {HANDOFF_TOKEN_LIMIT} "
          f"(Handoff при ~{HANDOFF_TOKEN_LIMIT / MAX_CONTEXT_TOKENS:.0%} заполнении контекста)")
    print(f"MAX_STEP_ATTEMPTS     : {MAX_STEP_ATTEMPTS}")
    print(f"TOKEN_ESTIMATE_DIVISOR: {TOKEN_ESTIMATE_DIVISOR} "
          f"(запасная оценка: len(text) // {TOKEN_ESTIMATE_DIVISOR})")
    print(f"HANDOFF_FILE          : {HANDOFF_FILE}")
    print(f"HANDOFF_BACKUP        : {HANDOFF_BACKUP}")
    print("                        (абсолютные, привязаны к каталогу модуля)")
    print("-" * 62)
    print("Самопроверка утилит:")

    fake_response = {"usage": {"total_tokens": 12345}}
    got = extract_used_tokens(fake_response, [])
    print(f"  extract_used_tokens(ответ с usage)      -> {got} "
          f"[{'OK' if got == 12345 else 'ОШИБКА'}]")

    fallback_msgs = [{"role": "user", "content": "x" * 300},
                     ("assistant", "y" * 300)]
    expected_estimate = 600 // _estimate_divisor()
    got = extract_used_tokens(None, fallback_msgs)
    print(f"  extract_used_tokens(без usage, ~оценка) -> {got} "
          f"(600 симв. // {TOKEN_ESTIMATE_DIVISOR}) "
          f"[{'OK' if got == expected_estimate else 'ОШИБКА'}]")

    below = is_context_critical(HANDOFF_TOKEN_LIMIT - 1)
    at = is_context_critical(HANDOFF_TOKEN_LIMIT)
    print(f"  is_context_critical(limit-1)={below}, (limit)={at} "
          f"[{'OK' if (not below and at) else 'ОШИБКА'}]")

    in_range = 1 <= HANDOFF_TOKEN_LIMIT <= MAX_CONTEXT_TOKENS
    print(f"  HANDOFF_TOKEN_LIMIT в диапазоне 1..{MAX_CONTEXT_TOKENS} "
          f"[{'OK' if in_range else 'ОШИБКА'}]")
    print(f"  HANDOFF_FILE абсолютный (не зависит от cwd) "
          f"[{'OK' if HANDOFF_FILE.is_absolute() else 'ОШИБКА'}]")
