# Путь: F:\Projects\team_loop\loop_team.py
# Назначение: ШАГ 5 — главный цикл команды: Аналитик -> Исполнитель <-> Изолированный
#             Ревьюер с проверкой контекста и механизмом Handoff. Соединяет Шаги 1-4
#             в один пайплайн: план, выполнение шага инструментами чтения и записи
#             файлов, изолированная проверка, очистка контекста, перенос состояния.
# Кодировка исходника: UTF-8 (в Python 3 она включена по умолчанию).
"""
loop_team.py — ШАГ 5: модернизация главного цикла loop_team.

Автономный цикл движка Strata: Аналитик -> Исполнитель <-> Изолированный Ревьюер
с механизмом Handoff (перенос состояния в новый чат при нехватке контекста).

Алгоритм (дословно по PLAN.md, ШАГ 5, п.2):
  * Старт: `build_execution_plan(task)` (ШАГ 4). Если был загружен `handoff.md`,
    его краткая сводка добавляется в системный контекст Исполнителя.
  * Внешний цикл по шагам плана: `for step_idx, step_text in enumerate(plan)`.
    Перед шагом запоминается длина истории `history_checkpoint`.
  * Внутренний цикл `while attempts < MAX_STEP_ATTEMPTS`:
      1. Проверка контекста ПЕРЕД вызовом: `is_context_critical(current_tokens)`
         -> `generate_handoff(...)`, уведомление в консоль, штатная остановка.
      2. Вызов Исполнителя: `step_text` и `feedback` ревьюера (если попытки > 0).
         Исполнитель читает и пишет файлы стандартными инструментами
         read_file / write_file / list_files.
      3. Обновление `current_tokens` через `extract_used_tokens(...)`.
      4. Вызов Изолированного Ревьюера: `review_step(step_text, отчёт, файлы)`.
      5. Вердикт: ПРИНЯТО -> очистка памяти (Context Pruning) при attempts > 0 и
         переход к следующему шагу; ДОРАБОТАТЬ -> attempts += 1 и новый `feedback`.
  * Аварийный выход по попыткам: `generate_handoff(...)` с описанием блокера и
    остановка выполнения.

Почему здесь нет LangChain и LangGraph. Старая версия этого файла была собрана на
них, что прямо запрещено правилом 1 PLAN.md («Никаких тяжелых фреймворков»). Кроме
того, пакет `langchain_openai` на машине не установлен, поэтому прежний файл падал
ещё на импорте. Алгоритм ШАГА 5 описан обычными циклами `for` и `while`, а Шаги 1-4
уже работают поверх OpenAI-совместимого HTTP-адреса локальной модели, поэтому цикл
написан на чистой стандартной библиотеке: ничего дополнительно ставить не нужно,
второй инстанс модели в памяти не поднимается (правило 3 PLAN.md).

Совместимость с Windows и защита от падения (правило 2 PLAN.md):
  * pathlib.Path для путей, явный encoding="utf-8" при любом чтении и записи файлов;
  * инструменты Исполнителя работают ТОЛЬКО внутри папки проекта: путь вне её
    отклоняется с русским объяснением, а не записывается куда попало;
  * запрос к модели идёт через стандартную библиотеку (urllib) с таймаутом; любая
    сетевая ошибка превращается в понятное русское сообщение, а не в traceback;
  * HTTP-ошибка при запросе с инструментами не трактуется как отсутствие поддержки:
    шаг не принимается без успешного ответа и проверяемых файлов на диске;
  * непредвиденная ошибка внутри цикла сохраняет состояние в handoff.md и завершает
    работу с объяснением на русском;
  * UTF-8 включается в stdout/stderr только в точке входа (if __name__ == "__main__":),
    при импорте модуля потоки процесса хоста не меняются.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

# Модуль может быть запущен из любого каталога (python F:\\...\\loop_team.py),
# поэтому каталог проекта явно добавляется в sys.path: без этого импорты Шагов 1-4
# работали бы только при запуске из папки проекта.
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

try:
    import team_config
except ImportError as _team_config_import_error:
    raise ImportError(
        "Не найден модуль team_config.py (ШАГ 1). Файл loop_team.py должен лежать "
        f"в той же папке, что и team_config.py: {_MODULE_DIR}"
    ) from _team_config_import_error

try:
    import isolated_reviewer
except ImportError as _reviewer_import_error:
    raise ImportError(
        "Не найден модуль isolated_reviewer.py (ШАГ 2). Файл loop_team.py должен "
        f"лежать в той же папке, что и isolated_reviewer.py: {_MODULE_DIR}"
    ) from _reviewer_import_error

try:
    import handoff_manager
except ImportError as _handoff_import_error:
    raise ImportError(
        "Не найден модуль handoff_manager.py (ШАГ 3). Файл loop_team.py должен "
        f"лежать в той же папке, что и handoff_manager.py: {_MODULE_DIR}"
    ) from _handoff_import_error

try:
    import analyst_planner
except ImportError as _analyst_import_error:
    raise ImportError(
        "Не найден модуль analyst_planner.py (ШАГ 4). Файл loop_team.py должен "
        f"лежать в той же папке, что и analyst_planner.py: {_MODULE_DIR}"
    ) from _analyst_import_error

__all__ = [
    "EXECUTOR_SYSTEM_PROMPT",
    "TOOL_DEFINITIONS",
    "run_team_loop",
]

# ---------------------------------------------------------------------------
# Настройки, тексты промпта и границы безопасности
# ---------------------------------------------------------------------------

# Корень проекта: за него инструменты Исполнителя не выходят. Переменная читается
# в момент вызова (а не зафиксирована в инструментах), поэтому офлайн-самопроверка
# может временно направить её во временную папку и не испортить рабочие файлы.
_PROJECT_DIR: Path = _MODULE_DIR

# Системный промпт Исполнителя. Главное требование — реальная работа инструментами:
# без вызова write_file шаг считается невыполненным, иначе Ревьюер (ШАГ 2) честно
# вернёт «файлы не найдены на диске» и попытка будет потрачена впустую.
EXECUTOR_SYSTEM_PROMPT = (
    "Ты — Исполнитель в автономном цикле разработки. Ты выполняешь ровно один шаг "
    "плана за раз и обязан довести его до реального результата на диске. "
    "Отвечай строго на русском языке. "
    "Правила работы: "
    "1) Файлы создавай и меняй только инструментом write_file, читай их инструментом "
    "read_file, содержимое папок смотри через list_files. Слова «я создал файл» без "
    "вызова инструмента считаются невыполненной работой. "
    "2) Пиши файл целиком: без заглушек вида «... остальной код здесь ...», без "
    "многоточий вместо кода и без комментариев TODO. "
    "3) Пути указывай относительно папки проекта, например helper.py или "
    "src\\helper.py. За пределы папки проекта не выходи. "
    "4) Не задавай вопросов и не проси подтверждения: выполни шаг и отчитайся. "
    "5) Завершая шаг, ответь обычным текстом без вызова инструментов: краткий отчёт, "
    "что сделано, какие файлы созданы или изменены (по одному пути в строке) и как "
    "это проверить."
)

# Задача для запуска без аргументов (критерий готовности Шага 5: цикл проходит
# многошаговую задачу от начала до конца). Файлы демо начинаются с demo_, они
# добавлены в .gitignore, чтобы не попасть в репозиторий.
DEMO_TASK = (
    "Создай в папке проекта файл demo_palindrome.py с функцией "
    "is_palindrome(text: str) -> bool: проверка, является ли строка палиндромом, "
    "без учёта регистра, пробелов и знаков препинания, с корректной обработкой "
    "пустой строки. В конце файла добавь блок if __name__ == '__main__': с тремя "
    "проверками на русском языке. Затем создай файл demo_palindrome_readme.md "
    "с описанием этой функции на русском языке."
)

# Сколько секунд ждём ответ модели. Исполнитель генерирует целые файлы, локальная
# модель на CPU считает медленно, но ожидание обязано остаться конечным: зависший
# вызов остановил бы весь цикл.
LLM_TIMEOUT_SECONDS = 600

# Температура Исполнителя: нужен воспроизводимый код, а не творчество.
EXECUTOR_TEMPERATURE = 0.2

# Максимум раундов вызовов инструментов за один шаг. Без рамки модель может
# бесконечно перечитывать один и тот же файл.
MAX_TOOL_ROUNDS = 8

# Границы содержимого: локальное контекстное окно одно на всех участников цикла,
# поэтому длинные тексты обрезаются, а не уходят в запрос целиком.
MAX_TOOL_RESULT_CHARS = 8000
MAX_FILE_READ_CHARS = 20000
# Ограничиваем один аргумент записи с запасом до Handoff, чтобы содержимое файла
# вместе с историей не могло в одиночку съесть всё окно модели.
MAX_FILE_WRITE_CHARS = max(1, min(200000, int(
    getattr(team_config, "HANDOFF_TOKEN_LIMIT", 24576)
)))
MAX_LIST_ENTRIES = 200
MAX_FEEDBACK_CHARS = 6000
HANDOFF_SUMMARY_CHARS = 4000
PRINT_FEEDBACK_CHARS = 700

# Папки, которые инструменты не перечисляют: в них нет содержательных файлов,
# а перечисление съело бы весь лимит выдачи.
SKIPPED_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".idea", ".vscode", ".pytest_cache",
})

# Префикс сообщений о том, что Исполнитель не смог получить ответ модели.
MODEL_ERROR_PREFIX = "Исполнитель не смог получить ответ: "

# Описание инструментов Исполнителя в формате OpenAI function calling.
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Читает текстовый файл проекта и возвращает его содержимое.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу относительно папки проекта.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Записывает файл целиком (перекодирует в UTF-8, создаёт нужные "
                "папки). Содержимое нужно передавать полностью, без заглушек."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу относительно папки проекта.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Полное содержимое файла.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Перечисляет файлы и папки внутри папки проекта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Папка относительно папки проекта. Пустая строка — корень "
                            "проекта."
                        ),
                    }
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Вспомогательные функции: вывод и текст
# ---------------------------------------------------------------------------

def _warn(message: str) -> None:
    """Пишет предупреждение в stderr и никогда не бросает исключение.

    Сообщение содержит кириллицу, а консоль Windows бывает в cp866/cp1251:
    при UnicodeEncodeError выводим ASCII-вариант с \\u-экраниками вместо падения.
    """
    stream = getattr(sys, "stderr", None)
    if stream is None or not hasattr(stream, "write"):
        return
    line = f"[loop_team] {message}\n"
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

    safe = line.encode("ascii", errors="backslashreplace").decode("ascii")
    try:
        stream.write(safe)
        if hasattr(stream, "flush"):
            stream.flush()
    except Exception:
        pass


def _as_text(value) -> str:
    """Безопасно приводит любое значение к строке (None -> пустая строка)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _truncate(text: str, limit: int) -> str:
    """Обрезает слишком длинный текст, сохраняя начало и пояснение на русском."""
    if len(text) <= limit:
        return text
    hidden = len(text) - limit
    return (
        text[:limit]
        + f"\n... (текст обрезан: скрыто ещё {hidden} символов, "
        "чтобы не переполнять контекст модели)"
    )


def _content_to_text(content) -> str:
    """Приводит поле content сообщения к простому тексту.

    OpenAI-совместимые серверы отдают строку, но встречаются списки частей
    вида [{"type": "text", "text": "..."}].
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _max_attempts() -> int:
    """Максимум попыток одного шага, читаемый из Шага 1 в момент вызова.

    Значение можно переопределить в .env, поэтому обращаемся к атрибуту модуля,
    а не копируем константу при импорте. Некорректное значение не должно обнулить
    цикл: минимум одна попытка обязана остаться.
    """
    value = getattr(team_config, "MAX_STEP_ATTEMPTS", 3)
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return 3
    return attempts if attempts >= 1 else 3


def _handoff_file_path() -> Path:
    """Текущий путь к handoff.md (читается из team_config в момент вызова)."""
    raw = getattr(team_config, "HANDOFF_FILE", None)
    if raw is None or not _as_text(raw).strip():
        return _MODULE_DIR / "handoff.md"
    try:
        path = Path(_as_text(raw).strip())
    except (TypeError, ValueError):
        return _MODULE_DIR / "handoff.md"
    if not path.is_absolute():
        path = _MODULE_DIR / path
    return path


# ---------------------------------------------------------------------------
# Инструменты Исполнителя: чтение, запись и перечисление файлов проекта
# ---------------------------------------------------------------------------

def _project_dir() -> Path:
    """Корень проекта, за который Исполнитель не выходит."""
    return _PROJECT_DIR


def _safe_project_path(raw):
    """Проверяет путь и возвращает пару (Path или None, текст_ошибки).

    Относительный путь отсчитывается от корня проекта. Модель могла предложить
    путь с «..», с диском C:\\ или с недопустимыми для Windows символами: запись
    за пределы проекта недопустима, поэтому такой путь отклоняется с объяснением
    на русском, а не выполняется.
    """
    root = _project_dir()
    text = _as_text(raw).strip().strip('"').strip("'")
    if not text:
        return None, "Инструмент не получил параметр path — укажи путь к файлу."
    try:
        candidate = Path(text)
    except (TypeError, ValueError) as exc:
        return None, f"Некорректный путь «{text}»: {exc}"

    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"Не удалось разобрать путь «{text}»: {exc}"

    if resolved != root and root not in resolved.parents:
        return None, (
            f"Путь «{text}» находится вне папки проекта {root}. "
            "Разрешены только файлы внутри папки проекта."
        )
    return resolved, ""


def _relative_display(path: Path) -> str:
    """Путь для показа человеку: относительно корня проекта, с обратными слэшами."""
    try:
        return str(path.relative_to(_project_dir()))
    except ValueError:
        return str(path)


def _tool_read_file(arguments: dict) -> str:
    """Инструмент read_file: содержимое файла проекта или русское объяснение неудачи."""
    path, error = _safe_project_path(arguments.get("path"))
    if path is None:
        return error

    try:
        if not path.is_file():
            return f"Файл не найден: {_relative_display(path)}. Проверь путь через list_files."
    except OSError as exc:
        return f"Не удалось проверить файл {_relative_display(path)}: {exc}"

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError, ValueError) as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        return f"Не удалось прочитать {_relative_display(path)}: {reason}"

    if not text.strip():
        return f"Файл {_relative_display(path)} пустой."
    return _truncate(text, MAX_FILE_READ_CHARS)


def _tool_write_file(arguments: dict, touched_files: list) -> str:
    """Инструмент write_file: запись файла целиком в UTF-8 внутри папки проекта."""
    path, error = _safe_project_path(arguments.get("path"))
    if path is None:
        return error

    content = arguments.get("content")
    if content is None or not _as_text(content).strip():
        return (
            "Инструмент write_file не получил параметр content. Передай полное "
            "содержимое файла целиком, без заглушек."
        )
    text = _as_text(content)
    if len(text) > MAX_FILE_WRITE_CHARS:
        return (
            f"Слишком большое содержимое ({len(text)} символов, максимум "
            f"{MAX_FILE_WRITE_CHARS}). Разбей файл на части или сократи его."
        )

    try:
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        return f"Не удалось создать папку для {_relative_display(path)}: {reason}"

    try:
        path.write_text(text, encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        return (
            f"Не удалось записать {_relative_display(path)}: {reason}. Часто файл "
            "открыт в Блокноте или его держит антивирус — закрой файл."
        )

    display = _relative_display(path)
    if path not in touched_files:
        touched_files.append(path)
    return f"Файл записан: {display} ({len(text)} символов, UTF-8)."


def _tool_list_files(arguments: dict) -> str:
    """Инструмент list_files: содержимое папки проекта (служебные папки пропускаются)."""
    raw = _as_text(arguments.get("path")).strip()
    if raw:
        folder, error = _safe_project_path(raw)
        if folder is None:
            return error
    else:
        folder = _project_dir()

    try:
        if not folder.is_dir():
            return f"Папка не найдена: {_relative_display(folder)}"
        entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        return f"Не удалось перечислить {_relative_display(folder)}: {reason}"

    lines = []
    for entry in entries:
        if entry.name in SKIPPED_DIRS:
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        lines.append(f"{'ПАПКА' if is_dir else 'ФАЙЛ '} {_relative_display(entry)}")
        if len(lines) >= MAX_LIST_ENTRIES:
            lines.append(f"... (список обрезан: показано только {MAX_LIST_ENTRIES} записей)")
            break

    if not lines:
        return f"В папке {_relative_display(folder)} нет файлов."
    return "\n".join(lines)


def _parse_arguments(raw) -> dict:
    """Разбирает аргументы инструмента: строка JSON или готовый словарь."""
    if isinstance(raw, dict):
        return raw
    text = _as_text(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _execute_tool_call(name: str, raw_arguments, touched_files: list) -> str:
    """Выполняет один вызов инструмента и всегда возвращает текст для модели.

    Любая ошибка превращается в русское сообщение: исключение разорвало бы
    агентский цикл Исполнителя и остановило весь шаг.
    """
    try:
        arguments = _parse_arguments(raw_arguments)
        if name == "read_file":
            return _tool_read_file(arguments)
        if name == "write_file":
            return _tool_write_file(arguments, touched_files)
        if name == "list_files":
            return _tool_list_files(arguments)
        return (
            f"Инструмент «{name}» неизвестен. Доступны: read_file, write_file, "
            "list_files."
        )
    except Exception as exc:  # последняя страховка: цикл не должен падать
        return (
            f"Инструмент «{name}» завершился ошибкой: {type(exc).__name__}: "
            f"{_as_text(exc)}"
        )


# ---------------------------------------------------------------------------
# Запрос к локальной модели и разбор ответа
# ---------------------------------------------------------------------------

def _chat_endpoint(base_url: str) -> str:
    """Превращает OPENAI_BASE_URL в полный адрес метода chat/completions."""
    base = _as_text(base_url).strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _call_local_model(messages: list, tools: list | None = None) -> tuple:
    """Отправляет запрос локальной модели и возвращает (ответ, ошибка, код_HTTP).

    При успехе: (словарь_ответа, "", None). При неудаче: (None, русское сообщение,
    код_ошибки_или_None). Исключение наружу не выпускается никогда: выключенный
    сервер не должен ронять автономный цикл.

    Код HTTP нужен вызывающему коду: по нему видно, что сервер отверг сами вызовы
    инструментов, а не сеть или модель.
    """
    url = _chat_endpoint(getattr(team_config, "OPENAI_BASE_URL", ""))
    if not url:
        return None, (
            f"{MODEL_ERROR_PREFIX}переменная OPENAI_BASE_URL пуста — непонятно, куда "
            "отправлять запрос. Укажи адрес локального сервера в файле .env "
            "(см. .env.example)."
        ), None

    payload = {
        "model": getattr(team_config, "MODEL_NAME", ""),
        "messages": messages,
        "temperature": EXECUTOR_TEMPERATURE,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    api_key = _as_text(getattr(team_config, "OPENAI_API_KEY", "")).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return None, f"{MODEL_ERROR_PREFIX}не удалось подготовить запрос: {exc}", None

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        message = (
            f"{MODEL_ERROR_PREFIX}локальный сервер ответил ошибкой HTTP {exc.code} "
            f"на {url}.\n{_truncate(detail, 1500)}\n"
            f"Частая причина — неверный MODEL_NAME в .env: проверь, что модель "
            f"«{getattr(team_config, 'MODEL_NAME', '')}» загружена на сервере."
        )
        return None, message, exc.code
    except urllib.error.URLError as exc:
        return None, (
            f"{MODEL_ERROR_PREFIX}нет связи с локальным сервером модели по адресу "
            f"{url} (причина: {exc.reason}).\n"
            "Что проверить: запущен ли сервер модели, совпадает ли OPENAI_BASE_URL "
            "в .env с его адресом и портом, не блокирует ли брандмауэр localhost."
        ), None
    except TimeoutError:
        return None, (
            f"{MODEL_ERROR_PREFIX}модель не ответила за {LLM_TIMEOUT_SECONDS} секунд. "
            "Локальная модель может быть ещё не загружена или отвечать слишком медленно."
        ), None
    except OSError as exc:
        return None, f"{MODEL_ERROR_PREFIX}сетевая ошибка при запросе к {url}: {exc}", None
    except Exception as exc:  # последняя страховка: цикл не должен падать
        return None, (
            f"{MODEL_ERROR_PREFIX}непредвиденная ошибка запроса к {url}: "
            f"{type(exc).__name__}: {_as_text(exc)}"
        ), None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, (
            f"{MODEL_ERROR_PREFIX}сервер вернул ответ не в формате JSON: "
            f"{_truncate(raw, 500)}"
        ), None

    if not isinstance(parsed, dict):
        return None, (
            f"{MODEL_ERROR_PREFIX}сервер вернул неожиданный тип ответа: "
            f"{type(parsed).__name__}"
        ), None
    return parsed, "", None


def _extract_assistant_message(response) -> tuple:
    """Достаёт из ответа пару (текст, список вызовов инструментов).

    Если tool_calls отсутствует, вызывающий цикл всё равно передаёт ответ
    изолированному Ревьюеру: файловые шаги не могут быть приняты без артефактов
    на диске, даже если отчёт модели выглядит убедительно.
    """
    if not isinstance(response, dict):
        return "", []
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", []
    first = choices[0]
    if not isinstance(first, dict):
        return "", []

    message = first.get("message")
    if not isinstance(message, dict):
        return _content_to_text(getattr(message, "content", "")).strip(), []

    content = _content_to_text(message.get("content")).replace("\ufeff", "").strip()
    if not content:
        # Некоторые серверы отдают готовый текст в поле text верхнего уровня.
        content = _content_to_text(first.get("text")).strip()

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        tool_calls = []
    return content, [call for call in tool_calls if isinstance(call, dict)]


# ---------------------------------------------------------------------------
# Контекст Исполнителя
# ---------------------------------------------------------------------------

def _handoff_summary(handoff_text) -> str:
    """Краткая сводка handoff.md для системного контекста Исполнителя (ШАГ 5, п.2)."""
    text = _as_text(handoff_text).strip()
    if not text:
        return ""
    return _truncate(text, HANDOFF_SUMMARY_CHARS)


def _build_executor_system(handoff_note: str) -> str:
    """Собирает системный контекст Исполнителя: правила, корень проекта, handoff."""
    parts = [
        EXECUTOR_SYSTEM_PROMPT,
        f"Корень проекта (все пути только внутри него): {_project_dir()}",
    ]
    if handoff_note:
        parts.append(
            "Краткая сводка из handoff.md — работа продолжается после переноса "
            "контекста, начинай с текущего шага и уже созданных файлов:\n"
            + handoff_note
        )
    return "\n\n".join(parts)


def _build_step_message(step_text: str, feedback: str) -> str:
    """Сообщение Исполнителю о текущем шаге и замечаниях ревьюера (ШАГ 5, п.2.2)."""
    lines = [f"Текущий шаг плана: {step_text}"]
    if feedback:
        lines.append(
            "\nЗамечания изолированного ревьюера — их нужно устранить обязательно "
            "(шаг уже проверен и не принят):\n"
            + _truncate(feedback, MAX_FEEDBACK_CHARS)
        )
    lines.append(
        "\nВыполни этот шаг сейчас с помощью инструментов read_file, write_file и "
        "list_files, а затем ответь обычным текстом без вызова инструментов: что "
        "сделано и какие файлы созданы или изменены."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Исполнитель: агентский цикл с вызовами инструментов
# ---------------------------------------------------------------------------

def _run_executor(messages: list, step_message: str,
                  touched_files: list) -> tuple:
    """Один ход Исполнителя: модель вызывает инструменты, затем отчитывается.

    Аргументы:
      messages      — история основного чата (изменяется на месте);
      step_message  — сообщение с целью шага и замечаниями ревьюера;
      touched_files — список записанных файлов (дополняется, Ревьюер его проверяет).

    Возвращает пару (отчёт_Исполнителя, последний_ответ_сервера). Отчётом всегда
    является строка: сетевая ошибка тоже становится текстом отчёта, и тогда Ревьюер
    вернёт ДОРАБОТАТЬ с понятной человеку причиной, а цикл продолжит попытки.
    """
    messages.append({"role": "user", "content": step_message})

    for _round_index in range(MAX_TOOL_ROUNDS):
        response, error, _code = _call_local_model(messages, TOOL_DEFINITIONS)

        if error:
            # HTTP-ошибка может означать переполнение контекста или сбой запроса,
            # а не отсутствие поддержки инструментов. Текстовый fallback запрещён:
            # его содержимое не будет автоматически записано и проверено на диске.
            return (error, None)

        content, tool_calls = _extract_assistant_message(response)

        if not tool_calls:
            messages.append({"role": "assistant", "content": content})
            return (content or "(модель вернула пустой ответ без текста)", response)

        # История OpenAI требует assistant-сообщение с tool_calls и ответ по каждому
        # вызову: без них следующий запрос сервер отвергнет ошибкой.
        messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": tool_calls,
        })
        for call_index, call in enumerate(tool_calls):
            function = call.get("function")
            if not isinstance(function, dict):
                function = {}
            call_id = _as_text(call.get("id")).strip() or f"call_{call_index}"
            name = _as_text(function.get("name")).strip()
            result = _execute_tool_call(name, function.get("arguments"), touched_files)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _truncate(result, MAX_TOOL_RESULT_CHARS),
            })

        estimated_tokens = team_config.extract_used_tokens(response, messages)
        if team_config.is_context_critical(estimated_tokens):
            # Не начинаем очередной запрос Исполнителя с заполненным окном; вызывающий
            # цикл сохранит handoff до Ревьюера и следующего шага.
            return ("Контекст достиг порога Handoff во время вызовов инструментов.", response)

    # Раунды кончились, а модель всё ещё вызывает инструменты: просим итог без них.
    _warn(
        f"Исполнитель не завершился за {MAX_TOOL_ROUNDS} раундов вызова инструментов "
        "— запрашиваю итоговый отчёт без инструментов."
    )
    messages.append({
        "role": "user",
        "content": (
            "Хватит вызывать инструменты. Кратко опиши текстом, что уже сделано и "
            "какие файлы созданы или изменены."
        ),
    })
    response, error, _code = _call_local_model(messages, None)
    if error:
        return (error, None)

    content, _tool_calls = _extract_assistant_message(response)
    messages.append({"role": "assistant", "content": content})
    return (content or "(модель вернула пустой ответ без текста)", response)


# ---------------------------------------------------------------------------
# Очистка памяти (Context Pruning) — ШАГ 5, п.5
# ---------------------------------------------------------------------------

def _prune_step_history(messages: list, checkpoint: int, final_report: str) -> int:
    """Убирает из истории промежуточные неудачные сообщения этого шага.

    Остаётся ровно два сообщения шага: постановка цели и финальный успешный ответ.
    Всё, что между ними (неудачные попытки, оправдания, замечания ревьюера),
    удаляется, чтобы не засорять контекст следующих шагов — ради этого и был
    придуман Context Pruning.

    Возвращает число удалённых сообщений (0, если удалять было нечего).
    """
    if checkpoint < 0 or checkpoint >= len(messages):
        return 0

    step_message = messages[checkpoint]
    kept = [step_message, {"role": "assistant", "content": _as_text(final_report)}]
    removed = len(messages) - checkpoint - len(kept)
    messages[checkpoint:] = kept
    return removed if removed > 0 else 0


# ---------------------------------------------------------------------------
# Штатная остановка с сохранением состояния (Handoff)
# ---------------------------------------------------------------------------

def _stop_for_handoff(task_text: str, steps: list, step_idx: int, messages: list,
                      blocker: str, status: str, current_tokens: int) -> dict:
    """Сохраняет handoff.md, уведомляет консоль и формирует итог остановки.

    Используется в трёх случаях из ТЗ: контекст критичен, попытки исчерпаны,
    непредвиденная ошибка. Работа всегда завершается штатно: без traceback и без
    потерянного состояния.
    """
    print("\n" + "=" * 62)
    print("СОХРАНЯЮ СОСТОЯНИЕ В handoff.md И ОСТАНАВЛИВАЮСЬ")
    print(f"Причина: {blocker}")
    print("=" * 62)

    document = None
    try:
        document = handoff_manager.generate_handoff(
            task_text, steps, step_idx, messages, blocker
        )
    except Exception as exc:  # сохранение не должно ронять цикл
        _warn(
            f"не удалось сохранить handoff.md: {type(exc).__name__}: "
            f"{_truncate(_as_text(exc), 500)}"
        )

    handoff_path = _handoff_file_path()
    saved = document is not None
    if saved:
        print(f"handoff.md сохранён: {handoff_path}")
        print("Продолжить работу можно запуском цикла из Шага 6 — он подхватит этот файл.")
    else:
        print(
            "handoff.md НЕ сохранён (причина напечатана выше). Проверь, не открыт ли "
            "файл handoff.md в Блокноте и правильно ли задан HANDOFF_FILE в .env."
        )

    return {
        "status": status,
        "completed_steps": step_idx,
        "total_steps": len(steps),
        "stopped_at_step": step_idx + 1,
        "handoff_saved": saved,
        "handoff_path": str(handoff_path),
        "tokens": current_tokens,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Публичная функция Шага 5: главный цикл
# ---------------------------------------------------------------------------

def run_team_loop(task: str = "") -> dict:
    """Запускает полный цикл: план -> (Исполнитель <-> Ревьюер) по каждому шагу.

    Аргумент: task — текст задачи пользователя. При продолжении работы из
    handoff.md его можно оставить пустым (ШАГ 4 сам достанет задачу из файла).

    Возвращает словарь итога:
      status          — "completed" (все шаги приняты), "context_handoff" (контекст
                        заполнен, состояние сохранено), "attempts_exhausted" (шаг не
                        принят за отведённые попытки) или "error";
      completed_steps / total_steps / stopped_at_step — где остановились;
      handoff_saved / handoff_path — что с файлом состояния;
      tokens          — сколько токенов занимает основной чат на момент остановки;
      messages        — история основного чата.
    """
    task_text = _as_text(task).strip()

    # СТАРТ: Аналитик (ШАГ 4). handoff_text — содержимое handoff.md, если он был.
    steps, handoff_text, plan_tokens = analyst_planner.build_execution_plan(task_text)
    if not task_text and handoff_text:
        # При возобновлении исходные требования берём из сохранённого раздела,
        # чтобы последующий handoff не заменил их сообщением о пустом аргументе.
        match = re.search(
            r"(?ms)^# Исходная задача\s*\n(.*?)(?=^# |\Z)", handoff_text
        )
        if match:
            task_text = match.group(1).strip()
            if task_text.startswith("Текст исходной задачи не передан"):
                task_text = ""
    handoff_note = _handoff_summary(handoff_text)

    messages: list = [{"role": "system", "content": _build_executor_system(handoff_note)}]
    # Начальная оценка контекста: системный промпт и сводка handoff ещё не были
    # в ответе сервера, поэтому считаем её приближённо (ШАГ 1, п.3).
    current_tokens = team_config.extract_used_tokens(None, messages)

    # Для каждого запроса инструменты остаются включёнными: единичный HTTP-код
    # не используется как основание отключать их до конца запуска.
    max_attempts = _max_attempts()
    step_idx = 0

    print("\n" + "=" * 62)
    print("loop_team — ШАГ 5: главный цикл команды")
    print("=" * 62)
    print(f"Задача            : {_truncate(task_text or '(из handoff.md)', 300)}")
    print(f"Шагов в плане     : {len(steps)}")
    print(f"Handoff подхвачен : {'да' if handoff_text else 'нет'}")
    print(f"Токены Аналитика  : {plan_tokens}")
    print(f"Попыток на шаг    : {max_attempts}")
    print(f"Лимит Handoff     : {getattr(team_config, 'HANDOFF_TOKEN_LIMIT', 0)} токенов")

    try:
        # ВНЕШНИЙ ЦИКЛ: по шагам плана.
        for step_idx, step_text in enumerate(steps):
            print("\n" + "-" * 62)
            print(f"ШАГ {step_idx + 1} из {len(steps)}: {step_text}")
            print("-" * 62)

            attempts = 0
            feedback = ""
            # Запоминаем длину истории до начала шага: по ней потом отсекается
            # промежуточный мусор неудачных попыток.
            history_checkpoint = len(messages)
            touched_files: list = []
            step_accepted = False

            # ВНУТРЕННИЙ ЦИКЛ: выполнение и ревью одного шага.
            while attempts < max_attempts:
                # 1. Проверка контекста ПЕРЕД вызовом модели.
                if team_config.is_context_critical(current_tokens):
                    limit = getattr(team_config, "HANDOFF_TOKEN_LIMIT", 0)
                    return _stop_for_handoff(
                        task_text, steps, step_idx, messages,
                        (
                            f"Контекст модели на исходе: использовано "
                            f"{current_tokens} токенов, порог Handoff — {limit}. "
                            f"Работа остановлена на шаге {step_idx + 1}: {step_text}"
                        ),
                        "context_handoff", current_tokens,
                    )

                attempts += 1
                print(
                    f"\nПопытка {attempts} из {max_attempts} "
                    f"[Used: {current_tokens} / Limit: "
                    f"{getattr(team_config, 'HANDOFF_TOKEN_LIMIT', 0)}]"
                )

                # 2. Вызов Исполнителя (цель шага и замечания ревьюера).
                step_message = _build_step_message(step_text, feedback)
                report, response = _run_executor(
                    messages, step_message, touched_files
                )

                # 3. Обновление current_tokens по реальному ответу сервера.
                current_tokens = team_config.extract_used_tokens(response, messages)
                if team_config.is_context_critical(current_tokens):
                    return _stop_for_handoff(
                        task_text, steps, step_idx, messages,
                        (
                            f"Контекст достиг порога Handoff внутри попытки: "
                            f"{current_tokens} токенов; остановка перед следующим "
                            "вызовом модели."
                        ),
                        "context_handoff", current_tokens,
                    )
                print(
                    f"Ответ Исполнителя получен "
                    f"[Used: {current_tokens} / Limit: "
                    f"{getattr(team_config, 'HANDOFF_TOKEN_LIMIT', 0)}], "
                    f"затронутых файлов: {len(touched_files)}"
                )
                # Начало отчёта показываем целиком в свёрнутом виде: человеку видно,
                # что именно модель считает сделанным, не открывая логов.
                preview = _truncate(" ".join(report.split()), 300)
                print(f"Начало отчёта Исполнителя: {preview}")

                # 4. Вызов Изолированного Ревьюера (ШАГ 2) — отдельным stateless
                #    запросом, без истории основного чата.
                if response is None:
                    review = {
                        "accepted": False,
                        "feedback": (
                            "Запрос Исполнителя завершился ошибкой; шаг не может быть "
                            "принят без успешного ответа сервера. " + _truncate(report, 1000)
                        ),
                        "review_tokens": 0,
                    }
                else:
                    review = isolated_reviewer.review_step(
                        step_text, report, list(touched_files)
                    )
                if not isinstance(review, dict):
                    review = {
                        "accepted": False,
                        "feedback": "Ревьюер вернул неожиданный результат — шаг не принят.",
                        "review_tokens": 0,
                    }

                accepted = bool(review.get("accepted"))
                review_tokens = review.get("review_tokens", 0)
                print(
                    f"Вердикт ревьюера: {'ПРИНЯТО' if accepted else 'ДОРАБОТАТЬ'} "
                    f"(отдельный запрос: {review_tokens} токенов)"
                )

                # 5. Оценка вердикта.
                if accepted:
                    if attempts > 1:
                        removed = _prune_step_history(
                            messages, history_checkpoint, report
                        )
                        print(
                            f"Очистка контекста: удалено сообщений: {removed} "
                            "(промежуточные сообщения неудачных попыток)"
                        )
                    step_accepted = True
                    break

                feedback = _as_text(review.get("feedback"))
                short_feedback = _truncate(feedback, PRINT_FEEDBACK_CHARS)
                print("Замечания ревьюера:")
                for line in short_feedback.splitlines():
                    print(f"  | {line}")

            if not step_accepted:
                # АВАРИЙНЫЙ ВЫХОД ПО ПОПЫТКАМ: шаг не принят, дальше идти нельзя.
                return _stop_for_handoff(
                    task_text, steps, step_idx, messages,
                    (
                        f"Шаг {step_idx + 1} не принят ревьюером, попытки исчерпаны "
                        f"({max_attempts}): {step_text}. Последние замечания ревьюера: "
                        f"{_truncate(feedback, MAX_FEEDBACK_CHARS) or 'замечаний нет'}"
                    ),
                    "attempts_exhausted", current_tokens,
                )

            print(f"Шаг {step_idx + 1} принят. Перехожу к следующему.")
    except Exception as exc:
        # Непредвиденная ошибка: состояние всё равно должно быть сохранено.
        _warn(
            f"непредвиденная ошибка главного цикла: {type(exc).__name__}: "
            f"{_truncate(_as_text(exc), 800)}"
        )
        return _stop_for_handoff(
            task_text, steps, step_idx, messages,
            f"Непредвиденная ошибка цикла: {type(exc).__name__}: {_as_text(exc)}",
            "error", current_tokens,
        )

    print("\n" + "=" * 62)
    print(f"ЗАДАЧА ВЫПОЛНЕНА: принято шагов {len(steps)} из {len(steps)}.")
    print(f"Основной чат занимает {current_tokens} токенов.")
    print("=" * 62)
    return {
        "status": "completed",
        "completed_steps": len(steps),
        "total_steps": len(steps),
        "stopped_at_step": None,
        "handoff_saved": False,
        "handoff_path": str(_handoff_file_path()),
        "tokens": current_tokens,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def _ensure_console_utf8() -> None:
    """Включает UTF-8 в консоли (правило 2 PLAN.md) — только для точки входа."""
    ensure = getattr(team_config, "_ensure_utf8_streams", None)
    if callable(ensure):
        ensure()
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def _offline_check() -> bool:
    """Проверка цикла без обращения к модели: инструменты, пути, очистка истории.

    Это не автоматический тест (pytest/unittest не используются), а ручной запуск
    `python loop_team.py`: он подтверждает, что инструменты пишут файлы в UTF-8,
    не выходят за пределы папки проекта и что очистка контекста убирает именно
    промежуточные сообщения. Все файлы создаются во ВРЕМЕННОЙ папке Windows.
    """
    global _PROJECT_DIR

    print("=" * 62)
    print("loop_team.py — ШАГ 5: проверка инструментов и логики без модели")
    print("=" * 62)
    print(f"MODEL_NAME         : {getattr(team_config, 'MODEL_NAME', '')}")
    print(f"OPENAI_BASE_URL    : {getattr(team_config, 'OPENAI_BASE_URL', '')}")
    print(f"MAX_STEP_ATTEMPTS  : {_max_attempts()}")
    print(f"HANDOFF_TOKEN_LIMIT: {getattr(team_config, 'HANDOFF_TOKEN_LIMIT', 0)}")
    print(f"Папка проекта      : {_project_dir()}")
    print("-" * 62)

    ok = True
    saved_root = _PROJECT_DIR
    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="team_loop_cycle_"))
        _PROJECT_DIR = temp_dir
        touched: list = []

        # 1. Путь за пределами проекта должен отклоняться, а не выполняться.
        outside, error = _safe_project_path("..\\..\\windows\\win.ini")
        blocked = outside is None and bool(error)
        print(f"  путь вне проекта отклонён        : [{'OK' if blocked else 'ОШИБКА'}]")
        ok = ok and blocked

        # 2. Запись файла с кириллицей в UTF-8 и вложенной папкой.
        source = "# -*- coding: utf-8 -*-\nprint('привет')\n"
        write_result = _tool_write_file({"path": "demo_pkg\\demo_mod.py",
                                         "content": source}, touched)
        written = (temp_dir / "demo_pkg" / "demo_mod.py")
        try:
            saved_text = written.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            saved_text = ""
        # Инструмент хранит разрешённый (resolve) путь, поэтому и ожидаемый путь
        # получаем тем же способом: иначе сравнение зависело бы от регистра букв
        # и коротких имён папок Windows.
        try:
            expected_path = written.resolve()
        except OSError:
            expected_path = written
        write_ok = (bool(write_result) and saved_text == source
                    and touched == [expected_path])
        print(f"  write_file пишет UTF-8           : [{'OK' if write_ok else 'ОШИБКА'}]")
        print(f"    {write_result}")
        ok = ok and write_ok

        # 3. Чтение и перечисление.
        read_result = _tool_read_file({"path": "demo_pkg\\demo_mod.py"})
        read_ok = "привет" in read_result
        print(f"  read_file читает файл            : [{'OK' if read_ok else 'ОШИБКА'}]")
        ok = ok and read_ok

        list_result = _tool_list_files({"path": "demo_pkg"})
        list_ok = "demo_mod.py" in list_result
        print(f"  list_files видит файл            : [{'OK' if list_ok else 'ОШИБКА'}]")
        ok = ok and list_ok

        # 4. Отсутствие содержимого и неизвестный инструмент — текст, а не падение.
        empty_write = _tool_write_file({"path": "demo_pkg\\no_content.py"}, touched)
        no_content_ok = "content" in empty_write and not (
            temp_dir / "demo_pkg" / "no_content.py"
        ).exists()
        print(f"  пустой content не пишет файл     : [{'OK' if no_content_ok else 'ОШИБКА'}]")
        ok = ok and no_content_ok

        unknown = _execute_tool_call("delete_everything", "{}", touched)
        unknown_ok = "неизвестен" in unknown
        print(f"  неизвестный инструмент отклонён  : [{'OK' if unknown_ok else 'ОШИБКА'}]")
        ok = ok and unknown_ok

        broken_json = _execute_tool_call("write_file", "{битый json", touched)
        broken_ok = bool(broken_json)
        print(f"  битый JSON аргументов не роняет  : [{'OK' if broken_ok else 'ОШИБКА'}]")
        ok = ok and broken_ok

        # 5. Очистка контекста: остаются цель шага и финальный ответ.
        history = [
            {"role": "system", "content": "правила"},
            {"role": "user", "content": "Текущий шаг: сделать файл"},
            {"role": "assistant", "content": "попытка 1"},
            {"role": "user", "content": "замечания ревьюера"},
            {"role": "assistant", "content": "попытка 2"},
        ]
        removed = _prune_step_history(history, 1, "ИТОГОВЫЙ ОТВЕТ")
        prune_ok = (
            removed == 2 and len(history) == 3
            and history[1]["role"] == "user"
            and history[2]["content"] == "ИТОГОВЫЙ ОТВЕТ"
        )
        print(f"  очистка контекста (2 удалено)    : [{'OK' if prune_ok else 'ОШИБКА'}]")
        ok = ok and prune_ok

        # 6. Сообщение шага содержит и цель, и замечания ревьюера.
        step_message = _build_step_message("Сделать модуль", "Нет обработки пустой строки")
        message_ok = ("Сделать модуль" in step_message
                      and "Нет обработки пустой строки" in step_message)
        print(f"  сообщение шага содержит правки   : [{'OK' if message_ok else 'ОШИБКА'}]")
        ok = ok and message_ok

        # 7. Описание инструментов — корректный JSON (его принимает сервер).
        try:
            json.dumps(TOOL_DEFINITIONS, ensure_ascii=False)
            tools_ok = True
        except (TypeError, ValueError):
            tools_ok = False
        print(f"  описание инструментов валидно    : [{'OK' if tools_ok else 'ОШИБКА'}]")
        ok = ok and tools_ok

    except OSError as exc:
        print(f"  [ОШИБКА] не удалось создать временную папку: {exc}")
        return False
    except Exception as exc:  # последняя страховка точки входа
        print(f"  [ОШИБКА] проверка завершилась ошибкой: {type(exc).__name__}: {exc}")
        return False
    finally:
        _PROJECT_DIR = saved_root
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    print("-" * 62)
    print("ИТОГ: " + ("инструменты и логика цикла работают, можно запускать модель."
                      if ok else "есть замечания — см. строки [ОШИБКА] выше."))
    return ok


def main() -> int:
    """Точка входа: офлайн-проверка, затем реальный цикл по задаче."""
    if not _offline_check():
        print("\n[ОШИБКА] проверка инструментов не пройдена — цикл не запускается.")
        return 1

    # Полноценный командный интерфейс (флаги --resume и --test-handoff) делается
    # в Шаге 6. Здесь задача берётся одной строкой из аргументов, чтобы можно было
    # проверить цикл на своей задаче прямо сейчас.
    task = " ".join(sys.argv[1:]).strip() or DEMO_TASK

    print("\n" + "=" * 62)
    print("ЗАПУСК ЦИКЛА ПО ЛОКАЛЬНОЙ МОДЕЛИ")
    print("=" * 62)
    print("Исполнитель СОЗДАЁТ И МЕНЯЕТ файлы в папке проекта — это его работа.")
    print("Нужен запущенный локальный сервер модели (см. .env и .env.example).")
    print("Остановка: Ctrl+C (загруженный handoff остаётся доступен в handoff.bak).")
    print(f"Задача: {_truncate(task, 300)}")

    try:
        result = run_team_loop(task)
    except KeyboardInterrupt:
        print("\n[ПРЕРВАНО] работа остановлена клавишей Ctrl+C.")
        print("Новый handoff не создан. Если был загружен handoff, исходная точка остаётся в handoff.bak.")
        return 130
    except Exception as exc:  # последняя страховка: без голого traceback
        print(f"\n[ОШИБКА] цикл завершился непредвиденной ошибкой: "
              f"{type(exc).__name__}: {_truncate(_as_text(exc), 800)}")
        return 1

    status = _as_text(result.get("status"))
    if status == "completed":
        print("\nРЕЗУЛЬТАТ: все шаги плана приняты ревьюером.")
        return 0

    # Для остальных исходов важно сказать правду о файле состояния: его запись
    # могла не удаться (файл открыт в Блокноте, неверный HANDOFF_FILE в .env).
    saved = bool(result.get("handoff_saved"))
    state_note = ("состояние сохранено в handoff.md"
                  if saved else "handoff.md НЕ сохранён — причина в строках выше")

    if status == "context_handoff":
        print(f"\nРЕЗУЛЬТАТ: контекст модели заполнен, {state_note}.")
        if saved:
            print("Это штатная остановка: продолжай работу новым запуском цикла.")
            return 0
        return 1
    if status == "attempts_exhausted":
        print(f"\nРЕЗУЛЬТАТ: шаг не принят ревьюером, {state_note}.")
        print("Что делать: открой handoff.md, прочитай замечания ревьюера и уточни")
        print("формулировку задачи (или уменьши её), затем запусти цикл заново.")
        return 1

    print(f"\nРЕЗУЛЬТАТ: работа остановлена из-за ошибки, {state_note}.")
    print("Подробности — в строках [loop_team] выше.")
    return 1


if __name__ == "__main__":
    # В точке входа включаем UTF-8 для stdout/stderr согласно правилу 2 PLAN.md.
    _ensure_console_utf8()
    sys.exit(main())
