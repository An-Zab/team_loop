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

Совместимость с Windows: pathlib.Path, явный encoding="utf-8",
принудительная UTF-8 консоль при прямом запуске скрипта.
"""

from __future__ import annotations

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
    "HANDOFF_FILE",
    "HANDOFF_BACKUP",
    "extract_used_tokens",
    "is_context_critical",
]

# ---------------------------------------------------------------------------
# Загрузка .env (без тяжёлых зависимостей).
# Если python-dotenv установлен — используем его; иначе парсим .env сами.
# ---------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def _parse_env_file(path: Path) -> dict:
    """Минимальный парсер .env: строки вида KEY=VALUE, комментарии через #."""
    result = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
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
            result[key] = _strip_quotes(value)
    return result


def _load_dotenv() -> None:
    """Загружает первый найденный .env (из cwd, затем из каталога модуля).

    Уже установленные переменные окружения НЕ перезаписываются
    (стандартная семантика dotenv: окружение важнее файла).
    """
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
        return
    except ImportError:
        pass

    for candidate in (Path.cwd() / ".env", _MODULE_DIR / ".env"):
        if candidate.is_file():
            for key, value in _parse_env_file(candidate).items():
                os.environ.setdefault(key, value)
            break


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
        print(
            f"[team_config] Переменная {name}={raw!r} не является целым числом; "
            f"использую дефолт {default}.",
            file=sys.stderr,
        )
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        print(
            f"[team_config] Переменная {name}={raw!r} не является числом; "
            f"использую дефолт {default}.",
            file=sys.stderr,
        )
        return default


def _env_path(name: str, default: str) -> Path:
    return Path(_env_str(name, default))


# ---------------------------------------------------------------------------
# Константы конфигурации (переопределяются через .env / переменные окружения)
# ---------------------------------------------------------------------------

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
    print(
        f"[team_config] MAX_CONTEXT_TOKENS={MAX_CONTEXT_TOKENS} должно быть больше 0; "
        f"использую дефолт {_DEFAULT_MAX_CONTEXT_TOKENS}.",
        file=sys.stderr,
    )
    MAX_CONTEXT_TOKENS = _DEFAULT_MAX_CONTEXT_TOKENS

# Доля контекста, которую нужно оставить свободной (порог срабатывания Handoff).
HANDOFF_FREE_RATIO: float = _env_float("HANDOFF_FREE_RATIO", 0.25)
if not 0.0 < HANDOFF_FREE_RATIO < 1.0:
    print(
        f"[team_config] HANDOFF_FREE_RATIO={HANDOFF_FREE_RATIO} вне диапазона (0; 1); "
        f"использую дефолт 0.25.",
        file=sys.stderr,
    )
    HANDOFF_FREE_RATIO = 0.25

# Расчётный порог токенов: при его достижении пора делать Handoff.
# Может быть явно переопределён в .env (например, для стресс-теста).
HANDOFF_TOKEN_LIMIT: int = _env_int(
    "HANDOFF_TOKEN_LIMIT",
    int(MAX_CONTEXT_TOKENS * (1.0 - HANDOFF_FREE_RATIO)),
)
if HANDOFF_TOKEN_LIMIT < 0:
    print(
        f"[team_config] HANDOFF_TOKEN_LIMIT={HANDOFF_TOKEN_LIMIT} не может быть "
        f"отрицательным; пересчитываю по формуле.",
        file=sys.stderr,
    )
    HANDOFF_TOKEN_LIMIT = int(MAX_CONTEXT_TOKENS * (1.0 - HANDOFF_FREE_RATIO))

# Максимум попыток исправления одного шага плана.
MAX_STEP_ATTEMPTS: int = _env_int("MAX_STEP_ATTEMPTS", _DEFAULT_MAX_STEP_ATTEMPTS)
if MAX_STEP_ATTEMPTS < 1:
    print(
        f"[team_config] MAX_STEP_ATTEMPTS={MAX_STEP_ATTEMPTS} должно быть не меньше 1; "
        f"использую дефолт {_DEFAULT_MAX_STEP_ATTEMPTS}.",
        file=sys.stderr,
    )
    MAX_STEP_ATTEMPTS = _DEFAULT_MAX_STEP_ATTEMPTS

# Файлы состояния механизма Handoff.
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


def extract_used_tokens(api_response, messages_fallback: list) -> int:
    """Возвращает число токенов, занятых текущим контекстом.

    1) Пытается достать `usage.total_tokens` из ответа локального сервера.
    2) Если сервер не вернул `usage` — считает приближённо по массиву
       messages_fallback: суммарная длина всех строк контента, делённая на 3.
    """
    total = _extract_total_tokens(api_response)
    if total is not None:
        return total

    total_chars = 0
    for message in messages_fallback or []:
        total_chars += len(_message_content_text(message))
    return total_chars // 3


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
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

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
    print(f"HANDOFF_FILE          : {HANDOFF_FILE.resolve()}")
    print(f"HANDOFF_BACKUP        : {HANDOFF_BACKUP.resolve()}")
    print("-" * 62)
    print("Самопроверка утилит:")

    fake_response = {"usage": {"total_tokens": 12345}}
    got = extract_used_tokens(fake_response, [])
    print(f"  extract_used_tokens(ответ с usage)      -> {got} "
          f"[{'OK' if got == 12345 else 'ОШИБКА'}]")

    fallback_msgs = [{"role": "user", "content": "x" * 300},
                     ("assistant", "y" * 300)]
    got = extract_used_tokens(None, fallback_msgs)
    print(f"  extract_used_tokens(без usage, ~оценка) -> {got} "
          f"[{'OK' if got == 200 else 'ОШИБКА'}]")

    below = is_context_critical(HANDOFF_TOKEN_LIMIT - 1)
    at = is_context_critical(HANDOFF_TOKEN_LIMIT)
    print(f"  is_context_critical(limit-1)={below}, (limit)={at} "
          f"[{'OK' if (not below and at) else 'ОШИБКА'}]")
