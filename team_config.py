# -*- coding: utf-8 -*-
"""
team_config.py — ШАГ 1: конфигурация и утилиты подсчёта токенов контекста.

Часть автономного цикла loop_team движка Strata
(Аналитик -> Исполнитель <-> Изолированный Ревьюер, механизм Handoff).

Возможности (см. PLAN.md, ШАГ 1):
  * Загрузка настроек из `.env` (`OPENAI_BASE_URL`, `OPENAI_API_KEY`,
    `MODEL_NAME`, `MAX_CONTEXT_TOKENS`) с дефолтными значениями.
  * ОДНО правило Handoff: контекст заполнен на 60 % и больше
    (`HANDOFF_TRIGGER_RATIO * MAX_CONTEXT_TOKENS`) — завершённая роль обязана
    обновить handoff.md и продолжить работу в чистом чате. Порог больше не
    настраивается отдельно: он всегда вычислен из `MAX_CONTEXT_TOKENS`, а его
    подставляет `start_team.py` из ответа сервера. Переключать под вторую модель
    руками в `.env` больше нечего.
  * extract_used_tokens()  — получение использованных токенов из ответа сервера
    (с приближённым подсчётом по сообщениям, если сервер не вернул `usage`).
  * is_context_critical()  — сигнал о том, что пора делать Handoff и уходить в
    чистый чат; context_fill_percent() — те же проценты для строк лога.

Совместимость с Windows:
  * pathlib.Path, явный encoding="utf-8" при записи файлов;
  * UTF-8 включается для stdout/stderr в точках входа (правило 2 PLAN.md);
    при импорте модуля глобальные потоки процесса намеренно не модифицируются,
    а функция вывода предупреждений `_warn()` устойчива к любой кодировке
    потока (включая cp1251/cp437) и подмене stderr;
  * .env читается устойчиво к кодировке: UTF-8 (с BOM и без) или UTF-16 с BOM
    (его даёт `> .env` в Windows PowerShell 5.1). Файл в другой кодировке не
    роняет импорт, а даёт предупреждение в stderr и дефолтные значения;
  * встроенный парсер .env полностью совместим с грамматикой python-dotenv
    (поддерживает комментарии со сдвигом через пробел/таб, пустые значения
    перед комментарием, `export`, кавычки с escape-последовательностями),
    благодаря чему результат конфигурации не зависит от наличия или отсутствия
    необязательного пакета python-dotenv;
  * файлы состояния Handoff привязаны к каталогу модуля, а не к текущему
    каталогу процесса, поэтому `handoff.md` не «едет» вместе с cwd.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import re
from pathlib import Path
import sys

__all__ = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "MODEL_NAME",
    "MAX_CONTEXT_TOKENS",
    "HANDOFF_TRIGGER_RATIO",
    "HANDOFF_TOKEN_LIMIT",
    "MAX_STEP_ATTEMPTS",
    "TOKEN_ESTIMATE_DIVISOR",
    "HANDOFF_FILE",
    "HANDOFF_BACKUP",
    "extract_used_tokens",
    "is_context_critical",
    "context_fill_percent",
]

# ---------------------------------------------------------------------------
# Потоки вывода и загрузка .env (без тяжёлых зависимостей).
#
# Единый источник истины: путь к .env ищем сами (cwd, затем каталог модуля),
# байты читаем и декодируем ОДНИМ и тем же кодом.
# Встроенный парсер воспроизводит грамматику python-dotenv (кавычки, export,
# комментарии через пробел/таб, экранирование). Если python-dotenv установлен,
# он вызывается с override=False и interpolate=False, давая строго идентичный
# результат.
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent

# BOM UTF-16 — то, с чего начинает байты `echo "K=v" > .env` в PowerShell 5.1.
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)

# Регулярные выражения для dotenv-совместимого парсера
_RE_MULTILINE_WS = re.compile(r"\s*", re.MULTILINE)
_RE_WS = re.compile(r"[^\S\r\n]*")
_RE_EXPORT = re.compile(r"(?:export[^\S\r\n]+)?")
_RE_SINGLE_KEY = re.compile(r"'([^']+)'")
_RE_UNQUOTED_KEY = re.compile(r"([^=\#\s]+)")
_RE_EQUAL = re.compile(r"(=[^\S\r\n]*)")
_RE_SINGLE_VAL = re.compile(r"'((?:\\.|[^'\\])*)'", re.DOTALL)
_RE_DOUBLE_VAL = re.compile(r'"((?:\\.|[^"\\])*)"', re.DOTALL)
_RE_UNQUOTED_VAL = re.compile(r"([^\r\n]*)")
_RE_COMMENT = re.compile(r"(?:[^\S\r\n]*#[^\r\n]*)?")
_RE_EOL = re.compile(r"[^\S\r\n]*(?:\r\n|\n|\r|$)")
_RE_REST_OF_LINE = re.compile(r"[^\r\n]*(?:\r|\n|\r\n)?")
_RE_DQUOTE_ESCAPES = re.compile(r"\\[\\'\"abfnrtv]")
_RE_SQUOTE_ESCAPES = re.compile(r"\\[\\']")


def _ensure_utf8_streams() -> None:
    """Включает UTF-8 в stdout и stderr (правило 2 PLAN.md).

    Вызывается в точках входа (например, в if __name__ == '__main__': или в
    точках входа скриптов запуска), чтобы не менять глобальное состояние
    процесса хоста при обычном импорте библиотеки.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            encoding = str(getattr(stream, "encoding", "") or "").replace("-", "").lower()
            if encoding == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError, TypeError):
            # Поток подменён чужим объектом, закрыт или reconfigure иной сигнатуры.
            pass
        except Exception:
            pass


def _warn(message: str) -> None:
    """Пишет предупреждение в stderr и никогда не бросает исключение."""
    stream = getattr(sys, "stderr", None)
    if stream is None or not hasattr(stream, "write"):
        return
    line = f"[team_config] {message}\n"
    try:
        stream.write(line)
        if hasattr(stream, "flush"):
            stream.flush()
        return
    except UnicodeEncodeError:
        pass
    except (AttributeError, ValueError, OSError):
        return
    except Exception:
        return

    # UTF-8 включить не удалось: выводим ASCII-безопасный вариант с \u-экраниками.
    safe = line.encode("ascii", errors="backslashreplace").decode("ascii")
    try:
        stream.write(safe)
        if hasattr(stream, "flush"):
            stream.flush()
    except Exception:
        pass


def _decode_escapes(regex: re.Pattern, text: str) -> str:
    """Декодирует escape-последовательности внутри кавычек, как в dotenv."""
    def _match(m: re.Match) -> str:
        return codecs.decode(m.group(0), "unicode-escape")

    return regex.sub(_match, text)


def _clean_value(raw: str) -> str:
    """Приводит правую часть строки `KEY=VALUE` к значению по грамматике dotenv.

    Снимает кавычки (с декодированием escape-последовательностей) и хвостовой
    комментарий (отделенный пробельными символами). Незакрытые кавычки отбрасываются.
    """
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("#"):
        # Пустое значение, за которым сразу идет комментарий (# ...)
        return ""
    if value[:1] in {"'", '"'}:
        quote = value[0]
        escapes = _RE_DQUOTE_ESCAPES if quote == '"' else _RE_SQUOTE_ESCAPES
        re_val = _RE_DOUBLE_VAL if quote == '"' else _RE_SINGLE_VAL
        m = re_val.match(value)
        if m:
            return _decode_escapes(escapes, m.group(1))
        # Незакрытая кавычка — синтаксическая ошибка dotenv, значение отбрасывается
        return ""
    return re.sub(r"\s+#.*", "", value).rstrip()


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
            # Нулевые байты свидетельствуют о повреждении файла либо неподдерживаемой
            # кодировке (например, UTF-16/UTF-32 без BOM, воспринятой как UTF-8).
            _warn(
                f"{source} содержит нулевые байты (NUL) или некорректную кодировку "
                f"(например, UTF-16/UTF-32 без BOM). Читаю UTF-8 или UTF-16 "
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


def _parse_env_text(text: str) -> dict[str, str]:
    """Парсер .env с полным паритетом грамматики python-dotenv.

    Поддерживает:
      * префикс export (с пробелами или табами);
      * ключи в одинарных кавычках и без кавычек;
      * значения в одинарных кавычках (экранирование \\', \\\\);
      * значения в двойных кавычках (экранирование \\n, \\t, \\", \\\\ и т. д.);
      * многострочные значения внутри кавычек;
      * незакрытые кавычки (отбрасываются с пропуском строки, не ломая остальные);
      * комментарии через пробел/таб: `KEY=8192\t# коммент` -> `8192`;
      * пустые значения перед комментарием: `KEY= # коммент` -> `""`;
      * символы `#` без предшествующих пробелов: `URL=http://host/#frag` сохраняются;
      * пустые строки и комментарии на отдельных строках.
    """
    result: dict[str, str] = {}
    pos = 0
    length = len(text)
    if text.startswith("\ufeff"):
        pos = 1

    while pos < length:
        m = _RE_MULTILINE_WS.match(text, pos)
        if m:
            pos = m.end()
        if pos >= length:
            break

        # Комментарий в начале строки
        if text[pos] == "#":
            m_eol = _RE_REST_OF_LINE.match(text, pos)
            pos = m_eol.end() if m_eol else length
            continue

        error = False
        key: str | None = None
        val: str | None = None

        m_exp = _RE_EXPORT.match(text, pos)
        if m_exp:
            pos = m_exp.end()

        # Разбор ключа
        if pos < length and text[pos] == "'":
            m_k = _RE_SINGLE_KEY.match(text, pos)
            if m_k:
                key = m_k.group(1)
                pos = m_k.end()
            else:
                error = True
        else:
            m_k = _RE_UNQUOTED_KEY.match(text, pos)
            if m_k:
                key = m_k.group(1)
                pos = m_k.end()
            else:
                error = True

        if not error:
            m_ws = _RE_WS.match(text, pos)
            if m_ws:
                pos = m_ws.end()

            if pos < length and text[pos] == "=":
                m_eq = _RE_EQUAL.match(text, pos)
                eq_str = m_eq.group(1) if m_eq else "="
                pos = m_eq.end() if m_eq else pos + 1

                # Если после '=' есть пробел и значение начинается с '#',
                # значение пустое, а остаток — комментарий (KEY= # коммент).
                if len(eq_str) > 1 and pos < length and text[pos] == "#":
                    val = ""
                elif pos >= length or text[pos] in ("\r", "\n"):
                    val = ""
                elif text[pos] == "'":
                    m_v = _RE_SINGLE_VAL.match(text, pos)
                    if m_v:
                        val = _decode_escapes(_RE_SQUOTE_ESCAPES, m_v.group(1))
                        pos = m_v.end()
                    else:
                        error = True
                elif text[pos] == '"':
                    m_v = _RE_DOUBLE_VAL.match(text, pos)
                    if m_v:
                        val = _decode_escapes(_RE_DQUOTE_ESCAPES, m_v.group(1))
                        pos = m_v.end()
                    else:
                        error = True
                else:
                    m_v = _RE_UNQUOTED_VAL.match(text, pos)
                    if m_v:
                        part = m_v.group(1)
                        val = re.sub(r"\s+#.*", "", part).rstrip()
                        pos = m_v.end()
                    else:
                        val = ""
            else:
                val = None

            if not error:
                m_comm = _RE_COMMENT.match(text, pos)
                if m_comm:
                    pos = m_comm.end()
                m_eol = _RE_EOL.match(text, pos)
                if m_eol:
                    pos = m_eol.end()
                else:
                    error = True

        if error:
            m_rest = _RE_REST_OF_LINE.match(text, pos)
            pos = m_rest.end() if m_rest else length
        elif key is not None and val is not None:
            result[key] = val

    return result


def _find_env_file() -> "Path | None":
    """Ищет .env: сначала cwd, затем каталог модуля.

    Защищён от падения при удалённом или недоступном рабочем каталоге (os.getcwd).
    """
    directories: list[Path] = []
    try:
        directories.append(Path.cwd())
    except (OSError, ValueError):
        # Рабочий каталог процесса удалён или недоступен
        pass
    directories.append(_MODULE_DIR)

    for directory in directories:
        try:
            candidate = directory / ".env"
            if candidate.is_file():
                return candidate
        except (OSError, ValueError):
            # Невозможно статить каталог (сетевой диск, права, симлинк-цикл) — идём дальше.
            continue
    return None


def _apply_with_dotenv(source: Path, text: str) -> bool:
    """Прогоняет уже прочитанный НАМИ текст через python-dotenv, если он стоит.

    Путь dotenv не передаём: find_dotenv искал бы файл заново по своей
    стратегии и декодировал бы байты по своей кодировке. Здесь же обе ветки
    получают один и тот же текст.
    override=False сохраняет приоритет окружения над файлом,
    interpolate=False обеспечивает строгое совпадение с грамматикой.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return False

    try:
        return bool(load_dotenv(stream=io.StringIO(text), override=False, interpolate=False))
    except TypeError:
        # Старая версия dotenv без поддержки interpolate (до 0.21.0)
        try:
            return bool(load_dotenv(stream=io.StringIO(text), override=False))
        except Exception:
            return False
    except (OSError, ValueError) as exc:
        _warn(f"python-dotenv не смог разобрать {source}: {exc}; "
              f"использую встроенный парсер.")
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


def _env_path(name: str, default: str) -> Path:
    """Путь из окружения; относительные значения привязываем к каталогу модуля.

    Иначе файл состояния Handoff следует за текущим каталогом процесса:
    запуск из другого каталога искал бы handoff.md не там, где его оставил
    предыдущий запуск (например, при вызове с флагом --resume в Шаге 6).
    Абсолютный путь (включая пути Windows вида C:\\... или F:/...) остаётся
    абсолютным.
    """
    raw = _env_str(name, default)
    try:
        from pathlib import PureWindowsPath
        if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
            return Path(raw)
        value = _MODULE_DIR / raw
        return value.resolve()
    except (OSError, RuntimeError, ValueError):
        value = Path(default)
        if not value.is_absolute():
            value = _MODULE_DIR / value
        return value


# ---------------------------------------------------------------------------
# Константы конфигурации (переопределяются через .env / переменные окружения)
# ---------------------------------------------------------------------------

# Загружаем настройки из окружения и .env при импорте модуля.
_load_dotenv()

# Подключение к локальному серверу модели (по умолчанию — стиль LM Studio).
OPENAI_BASE_URL: str = _env_str("OPENAI_BASE_URL", "http://localhost:1234/v1")
OPENAI_API_KEY: str = _env_str("OPENAI_API_KEY", "EMPTY")
MODEL_NAME: str = _env_str("MODEL_NAME", "qwen-local")

# Дефолты и границы, к которым возвращаемся при некорректных значениях в .env.
_DEFAULT_MAX_CONTEXT_TOKENS = 32768
_MIN_CONTEXT_TOKENS = 1
_MAX_ALLOWED_CONTEXT_TOKENS = 1048576  # 1M токенов — потолок проверки; start_team.py подставляет реальный n_ctx сервера
_DEFAULT_MAX_STEP_ATTEMPTS = 3

# Полный размер контекстного окна локальной модели.
MAX_CONTEXT_TOKENS: int = _env_int("MAX_CONTEXT_TOKENS", _DEFAULT_MAX_CONTEXT_TOKENS)
if not _MIN_CONTEXT_TOKENS <= MAX_CONTEXT_TOKENS <= _MAX_ALLOWED_CONTEXT_TOKENS:
    _warn(f"MAX_CONTEXT_TOKENS={MAX_CONTEXT_TOKENS} вне осмысленного диапазона "
          f"{_MIN_CONTEXT_TOKENS}..{_MAX_ALLOWED_CONTEXT_TOKENS}; "
          f"использую дефолт {_DEFAULT_MAX_CONTEXT_TOKENS}.")
    MAX_CONTEXT_TOKENS = _DEFAULT_MAX_CONTEXT_TOKENS

# Единственное правило Handoff: как только контекст заполнен на 60 % и больше,
# завершённая роль ОБЯЗАНА обновить handoff.md и продолжить работу в чистом чате.
# Доля зашита в код, а не в .env: модель одна за запуск, и переключать под неё
# приходится только MAX_CONTEXT_TOKENS (его подставляет start_team.py).
HANDOFF_TRIGGER_RATIO: float = 0.60


def _handoff_limit_from_ratio() -> int:
    """Порог Handoff: 60 % контекстного окна, то есть 0.6 * MAX_CONTEXT_TOKENS."""
    max_tokens = MAX_CONTEXT_TOKENS
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        max_tokens = _DEFAULT_MAX_CONTEXT_TOKENS
    return min(max(int(max_tokens * HANDOFF_TRIGGER_RATIO), 1), max_tokens)


def _effective_handoff_limit() -> int:
    """Возвращает действующий порог Handoff, зажатый в 1..MAX_CONTEXT_TOKENS.

    Порог — глобальная переменная модуля: стресс-тест `--test-handoff` понижает
    её на время своего запуска, поэтому читаем значение в момент вызова.
    """
    limit = HANDOFF_TOKEN_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return _handoff_limit_from_ratio()
    max_tokens = MAX_CONTEXT_TOKENS
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        max_tokens = _DEFAULT_MAX_CONTEXT_TOKENS
    return min(max(limit, 1), max_tokens)


# Расчётный порог токенов: при его достижении пора делать Handoff.
# Напрямую не настраивается — всегда 60 % от MAX_CONTEXT_TOKENS.
HANDOFF_TOKEN_LIMIT: int = _handoff_limit_from_ratio()

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
_MIN_TOKEN_ESTIMATE_DIVISOR = 1
_MAX_TOKEN_ESTIMATE_DIVISOR = 16

TOKEN_ESTIMATE_DIVISOR: int = _env_int(
    "TOKEN_ESTIMATE_DIVISOR", _DEFAULT_TOKEN_ESTIMATE_DIVISOR
)
if not _MIN_TOKEN_ESTIMATE_DIVISOR <= TOKEN_ESTIMATE_DIVISOR <= _MAX_TOKEN_ESTIMATE_DIVISOR:
    _warn(f"TOKEN_ESTIMATE_DIVISOR={TOKEN_ESTIMATE_DIVISOR} вне диапазона "
          f"{_MIN_TOKEN_ESTIMATE_DIVISOR}..{_MAX_TOKEN_ESTIMATE_DIVISOR}; "
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

    if usage is None or callable(usage):
        return None

    if isinstance(usage, dict):
        total = usage.get("total_tokens")
    else:
        total = getattr(usage, "total_tokens", None)

    if isinstance(total, bool):
        return None

    try:
        total = int(total)
    except (TypeError, ValueError):
        return None
    return total if total >= 0 else None


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
            else:
                text = getattr(part, "text", getattr(part, "content", None))
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
        return _content_to_text(message.get("content", message.get("text", "")))
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
    месте: ноль, bool или мусор не должны уронить подсчёт делением на ноль.
    """
    divisor = TOKEN_ESTIMATE_DIVISOR
    if isinstance(divisor, bool) or not isinstance(divisor, (int, float)):
        return _DEFAULT_TOKEN_ESTIMATE_DIVISOR
    try:
        val = int(divisor)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_TOKEN_ESTIMATE_DIVISOR
    if not _MIN_TOKEN_ESTIMATE_DIVISOR <= val <= _MAX_TOKEN_ESTIMATE_DIVISOR:
        return _DEFAULT_TOKEN_ESTIMATE_DIVISOR
    return val


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
        if isinstance(message, dict):
            # Аргументы вызовов инструментов часто содержат целые файлы; без них
            # запасная оценка резко занижает размер истории при отсутствии usage.
            calls = message.get("tool_calls", [])
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict):
                        function = call.get("function")
                        if isinstance(function, dict):
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                total_chars += len(arguments)
                            elif arguments is not None:
                                try:
                                    total_chars += len(json.dumps(
                                        arguments, ensure_ascii=False, sort_keys=True
                                    ))
                                except (TypeError, ValueError):
                                    total_chars += len(str(arguments))
    return total_chars // _estimate_divisor()


def is_context_critical(current_tokens: int) -> bool:
    """True, если контекст заполнен на 60 % и больше — пора в чистый чат.

    Порог равен `HANDOFF_TOKEN_LIMIT` и всегда составляет
    `HANDOFF_TRIGGER_RATIO * MAX_CONTEXT_TOKENS`. Константа читается как
    глобальная переменная модуля в момент вызова с защитным клампом в диапазон
    1..MAX_CONTEXT_TOKENS (стресс-тест Шага 6 понижает её на время своего
    запуска, и порог не должен стать нулём или больше самого окна).
    """
    try:
        cur = int(current_tokens)
    except (TypeError, ValueError):
        cur = 0
    return cur >= _effective_handoff_limit()


def context_fill_percent(current_tokens: int) -> float:
    """Сколько процентов контекстного окна занято (для строк лога и итога).

    Знаменатель — `MAX_CONTEXT_TOKENS`, то есть реальное окно модели, а не порог
    Handoff: человек мыслит именно заполнением контекста («62 % — уже за 60 %»).
    """
    maximum = MAX_CONTEXT_TOKENS
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        maximum = _DEFAULT_MAX_CONTEXT_TOKENS
    try:
        cur = int(current_tokens)
    except (TypeError, ValueError):
        cur = 0
    return cur * 100.0 / maximum


# ---------------------------------------------------------------------------
# Точка входа: вывод текущих лимитов (критерий готовности Шага 1)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # В точке входа включаем UTF-8 для stdout/stderr согласно правилу 2 PLAN.md.
    _ensure_utf8_streams()

    print("=" * 62)
    print("team_config.py — ШАГ 1: конфигурация цикла loop_team")
    print("=" * 62)
    print(f"MODEL_NAME            : {MODEL_NAME}")
    print(f"OPENAI_BASE_URL       : {OPENAI_BASE_URL}")
    print(f"OPENAI_API_KEY        : {'(задан)' if OPENAI_API_KEY else '(пуст)'}")
    print("-" * 62)
    print(f"MAX_CONTEXT_TOKENS    : {MAX_CONTEXT_TOKENS}")
    print(f"HANDOFF_TRIGGER_RATIO : {HANDOFF_TRIGGER_RATIO} "
          f"(правило Handoff: 60 % заполненного контекста)")
    print(f"HANDOFF_TOKEN_LIMIT   : {HANDOFF_TOKEN_LIMIT} "
          f"(Handoff при {HANDOFF_TOKEN_LIMIT / MAX_CONTEXT_TOKENS:.0%} заполнении контекста)")
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
    got = extract_used_tokens(None, fallback_msgs)
    tz_estimate = 600 // 3  # По ТЗ (PLAN.md, ШАГ 1, п.3): len(text) // 3
    expected_estimate = 600 // TOKEN_ESTIMATE_DIVISOR
    status_est = "OK" if (got == expected_estimate and (TOKEN_ESTIMATE_DIVISOR != 3 or got == tz_estimate)) else "ОШИБКА"
    print(f"  extract_used_tokens(без usage, ~оценка) -> {got} "
          f"(600 симв. // {TOKEN_ESTIMATE_DIVISOR}, эталон ТЗ: {tz_estimate}) "
          f"[{status_est}]")

    below = is_context_critical(HANDOFF_TOKEN_LIMIT - 1)
    at = is_context_critical(HANDOFF_TOKEN_LIMIT)
    above = is_context_critical(HANDOFF_TOKEN_LIMIT + 1)
    status_crit = "OK" if (not below and at and above) else "ОШИБКА"
    print(f"  is_context_critical: limit-1={below}, limit={at}, limit+1={above} "
          f"[{status_crit}]")

    expected_limit = int(MAX_CONTEXT_TOKENS * HANDOFF_TRIGGER_RATIO)
    status_formula = "OK" if HANDOFF_TOKEN_LIMIT == expected_limit else "ОШИБКА"
    print(f"  HANDOFF_TOKEN_LIMIT = {HANDOFF_TRIGGER_RATIO:.0%} от "
          f"MAX_CONTEXT_TOKENS ({expected_limit}) [{status_formula}]")

    in_range = 1 <= HANDOFF_TOKEN_LIMIT <= MAX_CONTEXT_TOKENS
    print(f"  HANDOFF_TOKEN_LIMIT в диапазоне 1..{MAX_CONTEXT_TOKENS} "
          f"[{'OK' if in_range else 'ОШИБКА'}]")
    pct = context_fill_percent(HANDOFF_TOKEN_LIMIT)
    print(f"  context_fill_percent(порог) = {pct:.0f} % от окна "
          f"[{'OK' if abs(pct - HANDOFF_TRIGGER_RATIO * 100) < 1.0 else 'ОШИБКА'}]")
    print(f"  HANDOFF_FILE абсолютный (не зависит от cwd) "
          f"[{'OK' if HANDOFF_FILE.is_absolute() else 'ОШИБКА'}]")
