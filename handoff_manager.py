# Путь: F:\Projects\team_loop\handoff_manager.py
# Назначение: ШАГ 3 — менеджер Handoff. Когда контекст локальной модели на исходе,
#             сжимает состояние работы и сохраняет его в handoff.md; при восстановлении
#             читает handoff.md, переименовывает его в handoff.bak и отдаёт текст в новый чат.
# Кодировка исходника: UTF-8 (в Python 3 она включена по умолчанию).
"""
handoff_manager.py — ШАГ 3: менеджер Handoff (сохранение и восстановление работы).

Часть автономного цикла loop_team движка Strata
(Аналитик -> Исполнитель <-> Изолированный Ревьюер, механизм Handoff).

Возможности (см. PLAN.md, ШАГ 3):
  * generate_handoff(task, plan, current_step_idx, recent_messages, blocker_reason)
    — формирует промпт локальной модели для сжатия контекста и требует ответ строго
    по Markdown-шаблону из 4 разделов:
        # Исходная задача
        # Статус плана
        # Артефакты и файлы на диске
        # Текущий контекст и блокер
    Результат записывается в handoff.md с encoding="utf-8".
  * load_handoff_if_exists() — если handoff.md читаемый и содержит четыре непустых
    раздела, читает его, переименовывает в handoff.bak (старый .bak перезаписывается
    через Path.replace) и возвращает текст; при явном запросе возобновления пустой,
    нечитаемый или неполный основной файл не затирает пригодный .bak.

Главный принцип этого шага: состояние работы теряться не должно. Поэтому, если модель
не ответила (сервер выключен, таймаут) или ответ не соответствует шаблону, файл всё
равно создаётся — недостающие разделы собираются автоматически из реальных данных
(текст задачи, план с отметками, причина остановки, последние сообщения). Так запуск
с переполненным контекстом не оставляет человека без точки продолжения.

Второй принцип: модель не имеет права тихо испортить исходные данные. Непустой раздел
ещё не значит достоверный, поэтому перед записью в файл ответ модели сверяется с тем,
что программа знает точно: полным текстом задачи и планом с отметками. Сокращённую или
пересказанную задачу, пропущенный шаг и отмеченный выполненным текущий шаг программа
подменяет достоверными данными и помечает раздел словами «исправлен программой
автоматически».

Совместимость с Windows и защита от падения (правило 2 PLAN.md):
  * pathlib.Path для путей, явный encoding="utf-8" при записи и чтении;
  * пути handoff.md и handoff.bak читаются из team_config в момент вызова, поэтому
    работают переопределения из .env и запуск из любого каталога;
  * запрос к модели идёт через стандартную библиотеку (urllib), новых зависимостей нет;
    любая сетевая ошибка превращается в понятное русское сообщение, а не в traceback;
  * в промпт попадает только хвост истории и только обрезанный по длине текст: запрос
    сжатия сам идёт в ту же модель с тем же окном контекста и не должен его переполнить;
    обрезка касается ТОЛЬКО промпта — в handoff.md текст задачи пишется целиком, иначе
    требования, не влезшие в окно, восстановить было бы неоткуда;
  * UTF-8 включается в stdout/stderr только в точке входа (if __name__ == "__main__":),
    при импорте модуля потоки процесса хоста не меняются.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

# Модуль может быть запущен из любого каталога (python F:\\...\\handoff_manager.py),
# поэтому каталог проекта явно добавляется в sys.path: без этого import team_config
# работал бы только при запуске из папки проекта.
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

try:
    import team_config
except ImportError as _team_config_import_error:
    raise ImportError(
        "Не найден модуль team_config.py (ШАГ 1). Файл handoff_manager.py должен "
        f"лежать в той же папке, что и team_config.py: {_MODULE_DIR}"
    ) from _team_config_import_error

__all__ = [
    "HANDOFF_HEADINGS",
    "HANDOFF_SYSTEM_PROMPT",
    "generate_handoff",
    "read_handoff_if_usable",
    "load_handoff_if_exists",
]

# ---------------------------------------------------------------------------
# Настройки, тексты промпта и границы безопасности
# ---------------------------------------------------------------------------

# Канонические заголовки шаблона — дословно из ТЗ (PLAN.md, ШАГ 3, п.1).
HANDOFF_HEADINGS = (
    "# Исходная задача",
    "# Статус плана",
    "# Артефакты и файлы на диске",
    "# Текущий контекст и блокер",
)

# Системный промпт: жёсткое требование шаблона. Модель склонна добавлять вступления
# («Вот ваш файл:») и оборачивать ответ в ```, поэтому это запрещено прямо.
HANDOFF_SYSTEM_PROMPT = (
    "Ты — модуль сжатия контекста (Handoff) в автономном цикле разработки. "
    "Твоя задача — сохранить состояние работы так, чтобы новый чат с пустой историей "
    "смог продолжить работу без потери данных. "
    "Отвечай строго на русском языке и строго по шаблону Markdown из четырёх разделов, "
    "начиная первую строку ответа с первого заголовка. Не пиши вступлений, пояснений "
    "и не оборачивай ответ в блоки кода. Заголовки верни ровно эти и ровно в этом порядке:\n"
    "# Исходная задача\n"
    "# Статус плана\n"
    "# Артефакты и файлы на диске\n"
    "# Текущий контекст и блокер\n"
    "Правила разделов:\n"
    "1) «# Исходная задача» — верни текст исходной задачи полностью, без потерь.\n"
    "2) «# Статус плана» — перечисли ВСЕ шаги плана строками списка: выполненные шаги "
    "пометь [x], текущий шаг и все оставшиеся — [ ]. Не меняй порядок и не придумывай новых шагов.\n"
    "3) «# Артефакты и файлы на диске» — укажи, какие файлы созданы или изменены и что "
    "в них находится. Не копируй сотни строк кода, только назначение и суть.\n"
    "4) «# Текущий контекст и блокер» — напиши, на каком шаге остановилась работа, "
    "последние замечания ревьюера или причину остановки, и что нужно сделать первым делом."
)

# Сколько секунд ждём ответ локальной модели. Сжатие контекста — генерация длинная,
# локальная модель на CPU считает медленно, но ожидание должно остаться конечным:
# зависший вызов остановил бы весь цикл Шага 5.
LLM_TIMEOUT_SECONDS = 300

# Запас истории на сжатие: берём только последние сообщения. Смысл Handoff в том,
# что контекст уже переполнен, поэтому полная история в промпт не помещается —
# запрос на сжатие переполнил бы окно сам.
MAX_RECENT_MESSAGES = 12
MAX_MESSAGE_CHARS = 1500
MAX_TASK_CHARS = 6000
MAX_BLOCKER_CHARS = 4000
MAX_ERROR_CHARS = 4000

# Пометка в автоматически собранном тексте: по ней человек и самопроверка видят,
# что часть разделов заполнена не моделью, а программой из реальных данных.
AUTO_ASSEMBLED_MARK = "собрана программой автоматически"
AUTO_ASSEMBLED_NOTE = f"Часть текста {AUTO_ASSEMBLED_MARK} из известных программе данных."

# Пометка в разделе, который программа подменила достоверными данными после сверки
# ответа модели с исходными сведениями: раздел был формально заполнен, но содержал
# сокращённую задачу, пропущенный шаг или неверную отметку [x]/[ ].
AUTO_CORRECTED_MARK = "исправлен программой автоматически"

# Нормализация заголовка: оставляет только буквы и цифры (кириллица, латиница) и
# переводит в нижний регистр. Так «## Статус плана», «**Статус плана:**» и
# «#  Статус плана» узнаются как нужный заголовок шаблона.
_HEADING_CLEAN_RE = re.compile(r"[^0-9A-Za-z\u0400-\u04FF]+")
_HEADING_LOOKUP = {_HEADING_CLEAN_RE.sub("", heading).lower(): heading for heading in HANDOFF_HEADINGS}

# Префикс сообщений о том, что сжать контекст моделью не удалось (в предупреждениях
# и в резертивном тексте файла).
MODEL_ERROR_PREFIX = "Модель не смогла сжать контекст: "


# ---------------------------------------------------------------------------
# Вспомогательные функции: вывод, текст, пути
# ---------------------------------------------------------------------------

def _warn(message: str) -> None:
    """Пишет предупреждение в stderr и никогда не бросает исключение.

    Сообщение может содержать кириллицу, а консоль Windows бывает в cp866/cp1251:
    при UnicodeEncodeError выводим ASCII-вариант с \\u-экраниками вместо падения.
    """
    stream = getattr(sys, "stderr", None)
    if stream is None or not hasattr(stream, "write"):
        return
    line = f"[handoff_manager] {message}\n"
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


def _truncate(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """Обрезает слишком длинный текст, сохраняя начало и пояснение на русском."""
    if len(text) <= limit:
        return text
    hidden = len(text) - limit
    return (
        text[:limit]
        + f"\n... (текст обрезан: скрыто ещё {hidden} символов, "
        "чтобы не переполнять контекст модели)"
    )


def _finish_sentence(text: str) -> str:
    """Возвращает текст одной фразой с одной точкой на конце.

    Пользовательский текст блокера или замечаний ревьюера часто уже кончается
    точкой: без нормализации в handoff.md появлялось «..», что мешало чтению.
    """
    cleaned = _as_text(text).strip()
    if not cleaned:
        return ""
    return cleaned.rstrip(". \t\r\n") + "."


def _handoff_file_path() -> Path:
    """Текущий путь к handoff.md (читается из team_config в момент вызова).

    Обращение через атрибут модуля, а не через `from team_config import HANDOFF_FILE`:
    во-первых, значения можно переопределить в .env, во-вторых — самопроверка точкой
    входа подменяет их на временные файлы, чтобы не затирать реальное состояние работы.
    """
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


def _backup_file_path() -> Path:
    """Текущий путь к handoff.bak (читается из team_config в момент вызова)."""
    raw = getattr(team_config, "HANDOFF_BACKUP", None)
    if raw is None or not _as_text(raw).strip():
        return _MODULE_DIR / "handoff.bak"
    try:
        path = Path(_as_text(raw).strip())
    except (TypeError, ValueError):
        return _MODULE_DIR / "handoff.bak"
    if not path.is_absolute():
        path = _MODULE_DIR / path
    return path


# ---------------------------------------------------------------------------
# Разбор входных данных
# ---------------------------------------------------------------------------

def _normalize_plan(plan) -> list[str]:
    """Приводит план любого вида к списку непустых строк.

    Допускает None, одну строку, Path, список/кортеж из str и Path. Ничего не бросает:
    падение Handoff лишило бы человека сохранённого состояния работы.
    """
    if plan is None:
        return []
    if isinstance(plan, (str, Path)):
        candidates = [plan]
    else:
        try:
            candidates = list(plan)
        except TypeError:
            candidates = [plan]

    steps: list[str] = []
    for item in candidates:
        text = _as_text(item).strip()
        if text:
            steps.append(text)
    return steps


def _clamp_step_index(current_step_idx, steps_count: int) -> int:
    """Целый индекс текущего шага в безопасных границах 0..steps_count-1."""
    try:
        index = int(current_step_idx)
    except (TypeError, ValueError):
        index = 0
    if steps_count <= 0:
        return 0
    if index < 0:
        return 0
    if index > steps_count - 1:
        return steps_count - 1
    return index


def _content_to_plain_text(content) -> str:
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


def _message_role_and_text(message) -> tuple[str, str]:
    """Вытаскивает (роль, текст) из сообщения чата любого распространённого вида.

    Понимает str, dict {"role": ..., "content": ...}, кортеж ("role", "content")
    и объекты с атрибутами .role / .content.
    """
    if message is None:
        return ("", "")

    if isinstance(message, str):
        return ("", message)

    if isinstance(message, dict):
        role = _as_text(message.get("role", message.get("author", "")))
        text = _content_to_plain_text(message.get("content", message.get("text", "")))
        return (role.strip(), text)

    if isinstance(message, (tuple, list)):
        # Формат ("role", "content"): текст — последний непустой элемент.
        text = ""
        for part in reversed(message):
            candidate = _content_to_plain_text(part)
            if candidate:
                text = candidate
                break
        role = _as_text(message[0]) if message else ""
        return (role.strip(), text)

    role = _as_text(getattr(message, "role", ""))
    text = _content_to_plain_text(getattr(message, "content", message))
    return (role.strip(), text)


def _messages_to_text(recent_messages) -> str:
    """Формирует блок «последние сообщения» для промпта и для резервного текста.

    Берутся только последние MAX_RECENT_MESSAGES сообщений, каждый текст обрезается.
    Это граница безопасности: промпт сжатия отправляется в ту же модель с тем же
    окном контекста и не должен его переполнить.
    """
    if recent_messages is None:
        return ""
    try:
        messages = list(recent_messages)
    except TypeError:
        messages = [recent_messages]

    tail = messages[-MAX_RECENT_MESSAGES:]
    lines = []
    for message in tail:
        role, text = _message_role_and_text(message)
        text = text.strip()
        if not text:
            continue
        label = role.strip() or "сообщение"
        lines.append(f"[{label}] {_truncate(text, MAX_MESSAGE_CHARS)}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Сборка промпта и запрос к модели
# ---------------------------------------------------------------------------

def _build_handoff_messages(task: str, steps: list[str], step_idx: int,
                            messages_text: str, blocker: str) -> list:
    """Собирает запрос к модели: system с шаблоном + user с фактическими данными.

    Сюда передают ПОЛНЫЙ текст задачи, а в промпт он уходит обрезанным по
    MAX_TASK_CHARS: запрос сжатия идёт в ту же модель с тем же окном контекста,
    которое уже переполнено. Ограничение размера живёт здесь и касается только
    промпта — в handoff.md задача пишется целиком (см. _build_fallback_sections).
    """
    plan_lines = []
    for idx, step in enumerate(steps):
        if idx < step_idx:
            status = "выполнен"
        elif idx == step_idx:
            status = "ТЕКУЩИЙ (его нужно продолжить)"
        else:
            status = "не начат"
        plan_lines.append(f"{idx + 1}. [{status}] {step}")
    plan_text = "\n".join(plan_lines) if plan_lines else "(список шагов пуст)"

    user_text = (
        "Сожми контекст и верни ответ строго по шаблону из четырёх разделов.\n\n"
        "Исходная задача:\n"
        f"{_truncate(task, MAX_TASK_CHARS) or '(текст задачи не передан)'}\n\n"
        "План шагов (порядок менять нельзя):\n"
        f"{plan_text}\n\n"
        f"Текущий шаг: номер {step_idx + 1} из {len(steps) if steps else 0} "
        f"(индекс {step_idx} в списке).\n\n"
        "Последние сообщения диалога (обрезаны, полная история недоступна):\n"
        f"{messages_text or '(сообщений нет)'}\n\n"
        "Причина остановки (блокер):\n"
        f"{_truncate(blocker, MAX_BLOCKER_CHARS) or '(блокер не передан)'}\n\n"
        "Напоминание шаблона — первой строкой ответ начни с заголовка «# Исходная задача», "
        "затем «# Статус плана», «# Артефакты и файлы на диске», «# Текущий контекст и блокер». "
        "В разделе статуса плана выполненные шаги отметь как [x], текущий и оставшиеся — как [ ]."
    )
    return [
        {"role": "system", "content": HANDOFF_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


def _chat_endpoint(base_url: str) -> str:
    """Превращает OPENAI_BASE_URL в полный адрес метода chat/completions."""
    base = _as_text(base_url).strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _call_local_model(messages: list) -> tuple:
    """Отправляет stateless-запрос локальной модели с temperature=0.0.

    Возвращает пару (словарь_ответа, None) при успехе или (None, русское сообщение)
    при любой сетевой ошибке. Исключение наружу не выпускается никогда: выключенный
    сервер не должен ронять сохранение состояния работы.

    Функция своя, а не импортированная из isolated_reviewer.py: там это приватная
    функция Шага 2, и её изменение разорвало бы Шаг 3.
    """
    url = _chat_endpoint(getattr(team_config, "OPENAI_BASE_URL", ""))
    if not url:
        return None, (
            f"{MODEL_ERROR_PREFIX}переменная OPENAI_BASE_URL пуста — непонятно, куда "
            "отправлять запрос. Укажи адрес локального сервера в файле .env "
            "(см. .env.example)."
        )

    payload = {
        "model": getattr(team_config, "MODEL_NAME", ""),
        "messages": messages,
        "temperature": 0.0,
        "stream": False,
    }
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
        return None, f"{MODEL_ERROR_PREFIX}не удалось подготовить запрос: {exc}"

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        return None, (
            f"{MODEL_ERROR_PREFIX}локальный сервер ответил ошибкой HTTP {exc.code} "
            f"на {url}.\n{_truncate(detail, 1500)}\n"
            f"Частая причина — неверный MODEL_NAME в .env: проверь, что модель "
            f"«{getattr(team_config, 'MODEL_NAME', '')}» загружена на сервере."
        )
    except urllib.error.URLError as exc:
        return None, (
            f"{MODEL_ERROR_PREFIX}нет связи с локальным сервером модели по адресу "
            f"{url} (причина: {exc.reason}).\n"
            "Что проверить: запущен ли сервер модели, совпадает ли OPENAI_BASE_URL "
            "в .env с его адресом и портом, не блокирует ли брандмауэр localhost."
        )
    except TimeoutError:
        return None, (
            f"{MODEL_ERROR_PREFIX}модель не ответила за {LLM_TIMEOUT_SECONDS} секунд. "
            "Локальная модель может быть ещё не загружена или отвечать слишком медленно."
        )
    except OSError as exc:
        return None, f"{MODEL_ERROR_PREFIX}сетевая ошибка при запросе к {url}: {exc}"
    except Exception as exc:  # последняя страховка: сохранение состояния не должно падать
        return None, f"{MODEL_ERROR_PREFIX}непредвиденная ошибка запроса к {url}: {exc}"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, (
            f"{MODEL_ERROR_PREFIX}сервер вернул ответ не в формате JSON: "
            f"{_truncate(raw, 500)}"
        )

    if not isinstance(parsed, dict):
        return None, (
            f"{MODEL_ERROR_PREFIX}сервер вернул неожиданный тип ответа: "
            f"{type(parsed).__name__}"
        )
    return parsed, None


def _extract_answer_text(response) -> str:
    """Достаёт текст ответа модели из choices[0].message.content."""
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""

    message = first.get("message")
    content = None
    if isinstance(message, dict):
        content = message.get("content")
    elif message is not None:
        content = getattr(message, "content", None)
    if content is None:
        # Некоторые серверы отдают готовый текст в поле text.
        content = first.get("text")

    return _content_to_plain_text(content).replace("\ufeff", "").strip()


# ---------------------------------------------------------------------------
# Разбор ответа модели в шаблон из 4 разделов
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Снимает обрамляющий блок ``` , если модель обернула им весь ответ.

    Модель просят не оборачивать, но локальные модели делают это регулярно;
    из-за трёх backtick-строк файл перестал бы быть чистым Markdown-шаблоном.
    """
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[0].strip().lower().startswith("```"):
        lines.pop(0)
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def _split_into_sections(text: str) -> dict:
    """Разбивает ответ модели на разделы по каноническим заголовкам шаблона.

    Всё, что написано до первого заголовка («Вот ваш файл:» и т. п.), отбрасывается:
    файл обязан содержать ровно 4 раздела шаблона. Возвращает словарь
    {канонический_заголовок: текст_раздела}; заголовков в ответе может и не быть —
    тогда словарь пустой.
    """
    collected: dict = {}
    current: str | None = None
    for line in text.splitlines():
        key = _HEADING_CLEAN_RE.sub("", line).lower()
        heading = _HEADING_LOOKUP.get(key)
        if heading is not None:
            current = heading
            collected.setdefault(heading, [])
            continue
        if current is not None:
            collected[current].append(line)

    return {heading: "\n".join(lines).strip() for heading, lines in collected.items()}


def _format_plan_checklist(steps: list[str], step_idx: int) -> str:
    """Список шагов с отметками по правилу ТЗ: [x] выполненные, [ ] текущий и дальше."""
    if not steps:
        return "- Список шагов плана пуст — шаг не определён."
    lines = []
    for idx, step in enumerate(steps):
        mark = "x" if idx < step_idx else " "
        suffix = " — текущий шаг, продолжить с него" if idx == step_idx else ""
        lines.append(f"- [{mark}] Шаг {idx + 1}. {step}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Сверка разделов модели с исходными данными
# ---------------------------------------------------------------------------

# Строка списка — кандидат в пункты плана. Без этого признака вступительная проза
# модели («Ниже статус плана:») посчиталась бы лишним шагом и раздел заменился бы
# без реальной необходимости.
_LIST_PREFIX_RE = re.compile(
    r"^\s*(?:[-*+\u2022]|\d+[.)]|\[[^\]]*\]|шаг[\s\u2116#:]*\d+)", re.IGNORECASE
)

# Отметка пункта в квадратных скобках: [x], [ ], [выполнен], [ТЕКУЩИЙ].
_CHECK_RE = re.compile(r"\[\s*([^\]\r\n]*)\s*\]")

# Что внутри скобок означает «шаг сделан». Пустая скобка — «не сделан».
_DONE_BOX_MARKS = frozenset(
    {"x", "х", "✓", "✔", "*", "done", "выполнено", "выполнен", "готово", "сделано"}
)

# Слова-и-символы статусов, когда модель отметку скобкой не ставила. Отрицания
# проверяются первыми: во «не выполнен» уже есть слово «выполнен».
_DONE_WORDS = ("выполнен", "заверш", "сделано", "готово", "done", "complete",
               "accepted", "✅", "✔", "☑")
_PENDING_WORDS = ("не выполнен", "не заверш", "не сделан", "не начат", "не готов",
                  "в процессе", "текущ", "pending", "todo", "current", "⬜", "❌")

# Порог похожести текста шага (0..1), при котором пункт ещё считается тем же
# самым шагом, а не выдуманным. Ниже — уже расхождение, которое надо исправлять.
_STEP_MATCH_RATIO = 0.6


def _heading_title(heading: str) -> str:
    """Название раздела без решётки: «# Статус плана» -> «Статус плана»."""
    return _as_text(heading).lstrip("#").strip()


def _skeleton(text) -> str:
    """Только буквы и цифры нижним регистром — устойчивая сверка фрагментов текста.

    Пробелы, звёздочки Markdown, кавычки-«ёлочки», дефисы и нумерацию модель меняет
    свободно, а смысл шага или задачи от этого не меняется. Сверяем «скелет» текста,
    чтобы не ругать на косметику и не пропустить настоящую подмену слов.
    """
    return _HEADING_CLEAN_RE.sub("", _as_text(text)).lower()


def _correction_note(heading: str, problems: list) -> str:
    """Пометка в начале исправленного раздела: что не так и откуда взят текст."""
    return (
        f"Примечание: раздел «{_heading_title(heading)}» {AUTO_CORRECTED_MARK}: "
        + "; ".join(problems)
        + ". Ниже — достоверные исходные данные, а не пересказ модели."
    )


def _corrected_section(heading: str, problems: list, body: str) -> str:
    """Собирает исправленный раздел: пометка вверху, достоверный текст ниже."""
    return f"{_correction_note(heading, problems)}\n\n{body}"


def _item_mark(has_box: bool, status: str, text: str):
    """Отметка пункта плана: True — выполнен, False — нет, None — отметки нет.

    Скобка надёжнее слов, но локальные модели пишут и «[выполнен]», и «готово»,
    и галочку без скобок, поэтому слова и символы тоже смотрим. Отрицания («не
    выполнен», «текущий») проверяем первыми: в них уже есть слово «выполнен».
    """
    mark = _as_text(status).strip().lower()
    if has_box and not mark:
        return False
    if mark in _DONE_BOX_MARKS:
        return True
    probe = f"{text} {mark}".lower()
    if any(word in probe for word in _PENDING_WORDS):
        return False
    if any(word in probe for word in _DONE_WORDS):
        return True
    return None


def _checklist_items(section: str) -> list:
    """Разбирает раздел «Статус плана» на пункты: пары (текст, отметка или None).

    Пунктом считается строка списка, нумерованная строка, строка со словом «Шаг N»
    или с квадратной скобкой. Вступительная проза модели пунктом не считается.
    """
    items = []
    for line in _as_text(section).splitlines():
        if not line.strip():
            continue
        if _LIST_PREFIX_RE.match(line) is None and _CHECK_RE.search(line) is None:
            continue
        match = _CHECK_RE.search(line)
        status = match.group(1).strip() if match else ""
        text = (line[:match.start()] + " " + line[match.end():]) if match else line
        items.append((text.strip(), _item_mark(match is not None, status, text)))
    return items


def _match_steps_and_items(steps: list, items: list) -> tuple:
    """Сопоставляет шаги плана с пунктами раздела, чтобы проверить отметки.

    Порядок строк модель может менять, поэтому ищем не по позиции, а по тексту:
    сначала точное совпадение «скелета», затем вхождение, затем похожий текст
    (модель могла перефразировать шаг). Возвращает тройку: соответствие шаг->пункт,
    номера пропущенных шагов, номера лишних пунктов.
    """
    step_keys = [_skeleton(step) for step in steps]
    item_keys = [_skeleton(text) for text, _ in items]

    matched: dict = {}
    used: set = set()

    # 1-й проход: точное совпадение, 2-й: пункт содержит шаг или шаг содержит пункт.
    for pass_no in (1, 2):
        for s_idx, key in enumerate(step_keys):
            if s_idx in matched or not key:
                continue
            for i_idx, item_key in enumerate(item_keys):
                if i_idx in used or not item_key:
                    continue
                exact = item_key == key
                inside = key in item_key or item_key in key
                if exact or (pass_no == 2 and inside):
                    matched[s_idx] = i_idx
                    used.add(i_idx)
                    break

    # 3-й проход: похожий текст, если модель перефразировала шаг своими словами.
    for s_idx, key in enumerate(step_keys):
        if s_idx in matched or not key:
            continue
        best_idx = None
        best_ratio = 0.0
        for i_idx, item_key in enumerate(item_keys):
            if i_idx in used or not item_key:
                continue
            ratio = difflib.SequenceMatcher(None, key, item_key).ratio()
            if ratio > best_ratio:
                best_idx, best_ratio = i_idx, ratio
        if best_idx is not None and best_ratio >= _STEP_MATCH_RATIO:
            matched[s_idx] = best_idx
            used.add(best_idx)

    missing = [idx for idx in range(len(steps)) if idx not in matched]
    extra = [idx for idx in range(len(items)) if idx not in used]
    return matched, missing, extra


def _verify_task_section(model_section: str, task_full: str) -> tuple:
    """Сверяет раздел «Исходная задача» с настоящим текстом задачи.

    Возвращает пару (текст_раздела, список_замечаний). Замечаний нет — модель вернула
    задачу без потерь, оставляем её текст. Иначе раздел заменяется полным исходным
    текстом: программа знает задачу дословно, а потерянное условие в новом чате
    восстановить будет неоткуда.
    """
    if not task_full or not _as_text(model_section).strip():
        # Сверять не с чем (задача не передана) либо раздел пуст — его заполнит
        # резервный текст из реальных данных ниже.
        return model_section, []

    truth = _skeleton(task_full)
    got = _skeleton(model_section)
    if not got or got == truth:
        return model_section, []

    if truth.startswith(got):
        problem = (f"модель вернула задачу не полностью, сокращено ~"
                   f"{len(truth) - len(got)} знаков")
    elif got in truth:
        problem = (f"модель вернула только часть задачи, не хватает ~"
                   f"{len(truth) - len(got)} знаков")
    elif truth in got:
        problem = "модель добавила к задаче посторонний текст"
    else:
        problem = "модель пересказала задачу и изменила её формулировку"
    return _corrected_section(HANDOFF_HEADINGS[0], [problem], task_full), [problem]


def _verify_plan_section(model_section: str, steps: list, step_idx: int) -> tuple:
    """Сверяет раздел «Статус плана» с настоящим планом и индексом текущего шага.

    Проверяет три вещи: все ли шаги на месте, не придумала ли модель лишних и
    правильные ли отметки [x]/[ ]. Главная опасность — отмеченный выполненным текущий
    шаг: новый чат тогда пропустит незавершённую работу. Возвращает пару
    (текст_раздела, список_замечаний); при замечаниях раздел пересобирается из плана.
    """
    items = _checklist_items(model_section)

    if not steps:
        if not items:
            return model_section, []
        problem = "список шагов пуст, а модель перечислила шаги"
        return (_corrected_section(HANDOFF_HEADINGS[1], [problem],
                                   _format_plan_checklist(steps, step_idx)),
                [problem])

    if not items:
        problem = "модель не вернула ни одного пункта списка шагов"
        return (_corrected_section(HANDOFF_HEADINGS[1], [problem],
                                   _format_plan_checklist(steps, step_idx)),
                [problem])

    matched, missing_steps, extra_items = _match_steps_and_items(steps, items)
    problems = []
    if missing_steps:
        problems.append("модель пропустила шаг(и) из плана: "
                        + ", ".join(str(idx + 1) for idx in missing_steps))
    if extra_items:
        problems.append("модель добавила шаги, которых нет в плане: "
                        + ", ".join(str(idx + 1) for idx in extra_items))

    for s_idx, i_idx in matched.items():
        _text, checked = items[i_idx]
        expected_done = s_idx < step_idx
        if checked is None:
            problems.append(f"у шага {s_idx + 1} нет отметки [x] или [ ]")
        elif checked == expected_done:
            continue
        elif s_idx == step_idx:
            problems.append(f"текущий шаг {s_idx + 1} помечен выполненным")
        elif expected_done:
            problems.append(f"выполненный шаг {s_idx + 1} помечен невыполненным")
        else:
            problems.append(f"шаг {s_idx + 1} ещё не выполнен, но помечен выполненным")

    if not problems:
        return model_section, []
    return (_corrected_section(HANDOFF_HEADINGS[1], problems,
                               _format_plan_checklist(steps, step_idx)),
            problems)


def _verify_model_sections(sections: dict, task_full: str, steps: list,
                           step_idx: int) -> list:
    """Сверяет разделы модели с исходными данными и исправляет их на месте.

    Проверка нужна, потому что непустой раздел ещё не значит достоверный: модель
    может сократить задачу, пропустить шаг плана или отметить выполненным текущий
    шаг. Программа эти данные знает точно, поэтому подменяет спорный раздел
    достоверным текстом и сообщает о каждом исправлении в stderr.

    Разделы, пришедшие не из ответа модели (их собрал _build_fallback_sections),
    сюда не попадают: они и так собраны из исходных данных.
    """
    problems: list = []

    task_section = _as_text(sections.get(HANDOFF_HEADINGS[0]))
    if task_section.strip():
        fixed, found = _verify_task_section(task_section, task_full)
        if found:
            sections[HANDOFF_HEADINGS[0]] = fixed
            problems.extend(f"«{_heading_title(HANDOFF_HEADINGS[0])}»: {text}"
                            for text in found)

    plan_section = _as_text(sections.get(HANDOFF_HEADINGS[1]))
    if plan_section.strip():
        fixed, found = _verify_plan_section(plan_section, steps, step_idx)
        if found:
            sections[HANDOFF_HEADINGS[1]] = fixed
            problems.extend(f"«{_heading_title(HANDOFF_HEADINGS[1])}»: {text}"
                            for text in found)

    if problems:
        _warn("Разделы модели сверены с исходными данными; исправлено: "
              + "; ".join(problems))
    return problems


def _build_fallback_sections(task: str, steps: list[str], step_idx: int,
                             messages_text: str, blocker: str,
                             model_error: str) -> dict:
    """Резервное содержимое разделов из реальных данных, когда модель не помогла.

    Разделы «Исходная задача» и «Статус плана» программа знает сама и точнее, чем
    модель: задача передаётся дословно и целиком, а отметки [x]/[ ] выводятся из
    индекса шага.
    """
    note_lines = []
    if model_error:
        note_lines.append(f"Примечание: {model_error}")
        note_lines.append(AUTO_ASSEMBLED_NOTE)
    else:
        note_lines.append(f"Примечание: {AUTO_ASSEMBLED_NOTE}")

    context_lines = []
    if steps:
        context_lines.append(
            f"Работа остановлена на шаге {step_idx + 1} из {len(steps)}: {steps[step_idx]}"
        )
    else:
        context_lines.append("Список шагов плана пуст — текущий шаг не определён.")
    context_lines.append(
        "Причина остановки (блокер): "
        f"{_finish_sentence(blocker) or 'блокер не передан.'}"
    )
    context_lines.extend(note_lines)
    if messages_text:
        context_lines.append("")
        context_lines.append("Последние сообщения диалога (обрезаны):")
        context_lines.append(messages_text)
    context_lines.append("")
    context_lines.append(
        "Что сделать первым делом: продолжить выполнение текущего шага с учётом "
        "замечаний ревьюера и причины остановки выше."
    )

    return {
        # Полный текст задачи, без обрезки по MAX_TASK_CHARS: handoff.md — это
        # состояние работы на диске, а не запрос модели. Обрезка нужна только чтобы
        # не переполнить окно модели, а потерянные здесь требования новый чат уже бы
        # не восстановил — он видит только этот файл.
        HANDOFF_HEADINGS[0]: _as_text(task).strip()
        or "Текст исходной задачи не передан — уточни его у пользователя.",
        HANDOFF_HEADINGS[1]: _format_plan_checklist(steps, step_idx),
        HANDOFF_HEADINGS[2]: (
            "Сведения об артефактах модель не передала. " + AUTO_ASSEMBLED_NOTE + "\n"
            f"Проверь содержимое каталога проекта: {_MODULE_DIR}\n"
            "Открой созданные и изменённые файлы и сверь их с планом выше — "
            "там актуальное состояние работы."
        ),
        HANDOFF_HEADINGS[3]: "\n".join(context_lines),
    }


def _compose_document(sections: dict) -> str:
    """Собирает итоговый текст файла: ровно 4 раздела в порядке шаблона."""
    blocks = []
    for heading in HANDOFF_HEADINGS:
        body = _as_text(sections.get(heading)).strip()
        blocks.append(f"{heading}\n{body}\n")
    return "\n".join(blocks)


def _write_handoff_file(document: str) -> bool:
    """Пишет handoff.md в UTF-8 и проверяет, что файл читается обратно.

    Проверка чтения — не формальность: критерий готовности Шага 3 требует читаемый
    файл в UTF-8, а записать его может помешать блокировка редактором или антивирусом.
    """
    path = _handoff_file_path()
    try:
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(
            f"Не удалось создать каталог {path.parent} для handoff.md: "
            f"{exc.strerror or exc}. Файл не сохранён."
        )
        return False

    try:
        path.write_text(document, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        _warn(
            f"Не удалось записать {path}: {reason}. Часто файл открыт в Блокноте или "
            "его держит антивирус — закрой файл и повтори запуск."
        )
        return False

    try:
        saved = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _warn(
            f"{path} записан, но не читается как UTF-8: {exc}. Проверь, что путь "
            "HANDOFF_FILE в .env указывает на обычный файл, а не на каталог."
        )
        return False

    if not saved.strip():
        _warn(f"{path} записан пустым — сохранение считается неудачным.")
        return False
    return True


# ---------------------------------------------------------------------------
# Публичные функции Шага 3
# ---------------------------------------------------------------------------

def generate_handoff(task: str, plan: list[str], current_step_idx: int,
                     recent_messages: list, blocker_reason: str) -> "str | None":
    """Сохраняет состояние работы в handoff.md (ШАГ 3, п.1).

    Аргументы:
      task              — текст исходной задачи: в handoff.md попадает целиком,
                          в промпт модели — обрезанным по MAX_TASK_CHARS;
      plan              — список шагов плана;
      current_step_idx  — индекс текущего (незавершённого) шага, с нуля;
      recent_messages   — последние сообщения основного чата (обрезаются);
      blocker_reason    — причина остановки: замечания ревьюера или переполнение контекста.

    Возвращает текст, записанный в handoff.md, или None, если записать не удалось
    (тогда в stderr уже написано русское объяснение причины).

    Модель вызывается один раз коротким stateless-запросом (правило 3 PLAN.md). Если она
    недоступна или ответ не соответствует шаблону, файл всё равно создаётся: пустые
    разделы заполняются резервным текстом из реальных данных.

    Формально заполненный раздел ещё не значит достоверный, поэтому ответ модели
    сверяется с исходными данными (_verify_model_sections): сокращённую задачу,
    пропущенный шаг плана или отмеченный выполненным текущий шаг программа исправляет
    сама и помечает раздел словами «исправлен программой автоматически».
    """
    steps = _normalize_plan(plan)
    step_idx = _clamp_step_index(current_step_idx, len(steps))
    # Полный текст задачи — единственная копия исходных требований: именно она
    # попадёт в handoff.md. Ограничение по длине нужно только промпту (окно контекста
    # уже переполнено), и применяет его _build_handoff_messages. Раньше задача
    # обрезалась здесь и искалеченный текст уходил и в модель, и в файл.
    task_full = _as_text(task).strip()
    blocker = _truncate(_as_text(blocker_reason), MAX_BLOCKER_CHARS).strip()
    messages_text = _messages_to_text(recent_messages)

    prompt = _build_handoff_messages(task_full, steps, step_idx, messages_text, blocker)
    response, error = _call_local_model(prompt)
    model_error = _as_text(error)
    model_text = ""
    if error is None:
        model_text = _strip_code_fences(_extract_answer_text(response))
        if not model_text:
            model_error = (
                f"{MODEL_ERROR_PREFIX}модель вернула пустой ответ без текста. Обычно "
                "так бывает, если модель ещё не загрузила weights или генерация "
                "обрезалась нулевой длиной."
            )
    else:
        _warn(model_error)

    sections = _split_into_sections(model_text) if model_text else {}
    if model_text and not sections:
        # Заголовков нет, но текст есть: сохраняем его в разделе про контекст,
        # остальное восстановим из известных данных.
        sections[HANDOFF_HEADINGS[3]] = model_text

    if model_text and sections:
        missing = [heading for heading in HANDOFF_HEADINGS if not sections.get(heading, "").strip()]
        if missing:
            _warn(
                "В ответе модели нет разделов: "
                + ", ".join(missing)
                + " — они заполнены из известных программе данных."
            )
        # Разделы модели непустые, но их содержание ещё надо сверить с исходными
        # данными: модель может сократить задачу, пропустить шаг или отметить
        # выполненным текущий шаг. Исправление автоматическое — без него такой ответ
        # ушёл бы в handoff.md и новый чат продолжил бы работу по искажённым данным.
        _verify_model_sections(sections, task_full, steps, step_idx)

    fallback = _build_fallback_sections(
        task_full, steps, step_idx, messages_text, blocker, model_error
    )
    for heading in HANDOFF_HEADINGS:
        if not _as_text(sections.get(heading)).strip():
            sections[heading] = fallback[heading]

    document = _compose_document(sections)
    if not _write_handoff_file(document):
        return None
    return document


def _read_handoff_text(path: Path) -> "str | None":
    """Читает handoff.md, возвращает None при недоступности файла (файл сохраняется).

    Сначала честный UTF-8 (utf-8-sig понимает и BOM, и обычный UTF-8). Если не читается,
    пробуем CP1251: его даёт `Get-Content ... | Set-Content ...` в Windows PowerShell 5.1,
    когда новичок пересохраняет файл. Это последнее средство, и оно обязательно
    сопровождается предупреждением: потерять точку продолжения работы хуже, чем прочитать
    текст с искажённой кириллицей.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        pass
    except (OSError, ValueError) as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        _warn(
            f"Не удалось прочитать {path}: {reason}. Файл оставлен на диске без "
            "изменений; содержимое не использовано."
        )
        return None

    try:
        text = path.read_text(encoding="cp1251")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        _warn(
            f"{path} не читается ни как UTF-8, ни как CP1251: {reason}. Файл оставлен "
            "на диске без изменений и не использован. Открой его в Блокноте и сохрани "
            "в кодировке UTF-8, после чего повтори запуск."
        )
        return None

    _warn(
        f"{path} сохранён не в UTF-8, поэтому прочитан как CP1251: кириллица может "
        "быть искажена. Пересохраните файл в UTF-8 (Блокнот: «Сохранить как» -> "
        "Кодировка: UTF-8)."
    )
    return text


def read_handoff_if_usable(path: Path) -> "str | None":
    """Читает непустой handoff без перемещения или изменения файла.

    Используется точкой входа для проверки `--resume` до запуска модели и самим
    загрузчиком при выборе между основным и резервным состоянием. Нечитаемый,
    пустой или не содержащий всех четырёх непустых разделов файл не считается точкой
    продолжения и никогда не архивируется поверх существующего `.bak`.
    """
    try:
        if not path.is_file():
            return None
    except (OSError, ValueError) as exc:
        _warn(f"Не удалось проверить наличие {path}: {_as_text(exc)}.")
        return None

    text = _read_handoff_text(path)
    if text is None:
        return None
    if not text.strip():
        _warn(f"{path} пуст — не использую его как точку продолжения.")
        return None

    sections = _split_into_sections(text)
    missing = [
        heading for heading in HANDOFF_HEADINGS
        if not _as_text(sections.get(heading)).strip()
    ]
    if missing:
        _warn(
            f"{path} не подходит как точка продолжения: отсутствуют или пусты "
            f"разделы {', '.join(missing)}. Файл оставлен без изменений."
        )
        return None
    return text


def _archive_handoff(path: Path) -> bool:
    """Переименовывает handoff.md в handoff.bak, перезаписывая старый .bak.

    Path.replace (как и os.replace) на Windows заменяет существующий файл-приёмник,
    поэтому старый handoff.bak не мешает. При неудаче исходный файл остаётся на диске:
    переименование — это учёт, а потеря состояния работы недопустима.
    """
    backup = _backup_file_path()

    # Страховка от опечатки в .env: если HANDOFF_BACKUP указывает на сам handoff.md,
    # replace просто уничтожил бы файл состояния.
    try:
        if os.path.normcase(str(backup)) == os.path.normcase(str(path)):
            _warn(
                "HANDOFF_BACKUP в .env совпадает с HANDOFF_FILE — переименование "
                "удалило бы файл состояния. Оставляем handoff.md на диске; исправь "
                "значения в .env (handoff.md и handoff.bak)."
            )
            return False
    except (TypeError, ValueError):
        return False

    try:
        path.replace(backup)
        return True
    except FileNotFoundError:
        _warn(f"{path} исчез перед переименованием — возможно, его уже перенесли. "
              "Ничего страшного: текст handoff уже прочитан.")
    except PermissionError:
        _warn(
            f"Не удалось переименовать {path} в {backup}: файл открыт в другой программе "
            "(Блокнот, редактор) или его держит антивирус. Закрой файл. handoff.md "
            "остался на диске, текст передан в работу."
        )
    except OSError as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        _warn(
            f"Не удалось переименовать {path} в {backup}: {reason}. handoff.md остался "
            "на диске, текст передан в работу."
        )
    return False


def load_handoff_if_exists(include_backup: bool = False) -> "str | None":
    """Читает используемое состояние Handoff (ШАГ 3, п.2).

    Читаемый `handoff.md` со всеми четырьмя непустыми разделами имеет приоритет и
    архивируется в `.bak`. При явном возобновлении `include_backup=True` пустой,
    нечитаемый или неполный основной файл не затирает пригодную резервную копию.
    Обычный запуск не подхватывает резервную копию, чтобы старое состояние не
    перехватывало новые задачи.
    """
    path = _handoff_file_path()
    try:
        primary_exists = path.is_file()
    except (OSError, ValueError) as exc:
        _warn(f"Не удалось проверить наличие {path}: {_as_text(exc)}.")
        primary_exists = False

    primary_text = read_handoff_if_usable(path) if primary_exists else None
    if primary_text is not None:
        _archive_handoff(path)
        return primary_text

    if include_backup:
        backup = _backup_file_path()
        backup_text = read_handoff_if_usable(backup)
        if backup_text is not None:
            if primary_exists:
                _warn(
                    f"Основной файл {path} непригоден; использую резервный "
                    f"{backup} и оставляю основной файл без изменений."
                )
            return backup_text

    # Не перемещаем пустой/нечитаемый основной файл в `.bak`: там может находиться
    # единственная пригодная резервная копия. Для новой задачи он просто игнорируется.
    return None


# ---------------------------------------------------------------------------
# Точка входа: демонстрация критерия готовности Шага 3
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


def _print_document(path: Path) -> None:
    """Печатает содержимое handoff.md, чтобы человек мог его прочитать глазами."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"  [ОШИБКА] не удалось прочитать {path} как UTF-8: {exc}")
        return
    print("  --- содержимое handoff.md (читается как UTF-8) ---")
    for line in content.splitlines():
        print(f"  | {line}")
    print("  ------------------------------------------------")


def _verification_demo() -> bool:
    """Проверка без обращения к модели: полный текст задачи и сверка разделов.

    Демонстрация работает офлайн, поэтому её видно и с выключенным сервером модели,
    — именно в запасном режиме важнее всего не потерять требования и отметки плана.
    В stderr намеренно печатаются предупреждения об исправлениях: так и задумано.
    """
    print("\nПРОВЕРКА БЕЗ МОДЕЛИ. Полный текст задачи и сверка ответа модели:")
    print("(в stderr появятся предупреждения [handoff_manager] — это проверяемые")
    print(" исправления, они и есть предмет проверки)")
    ok = True

    steps = ["Подготовить конфигурацию", "Написать ревьюера",
             "Написать менеджер Handoff"]
    step_idx = 2
    long_task = ("Собрать автономный цикл loop_team с механизмом Handoff. " * 300).strip()

    # 1. Задача длиннее лимита: в файл (резервный раздел) она обязана попасть целиком.
    fallback = _build_fallback_sections(long_task, steps, step_idx, "",
                                        "Контекст заполнен.", "")
    task_section = fallback[HANDOFF_HEADINGS[0]]
    kept_full = task_section == long_task and "текст обрезан" not in task_section
    print(f"  задача длиннее лимита ({len(long_task)} > {MAX_TASK_CHARS} символов),")
    print(f"  в handoff.md она попала целиком        : "
          f"[{'OK' if kept_full else 'ОШИБКА'}]")
    ok = ok and kept_full

    # 2. Промпт модели остаётся ограниченным: окно контекста не переполняется.
    prompt_text = _build_handoff_messages(long_task, steps, step_idx, "",
                                          "Контекст заполнен.")[1]["content"]
    prompt_bounded = ("текст обрезан" in prompt_text) and len(prompt_text) < len(long_task)
    print(f"  в промпт модель задача ушла обрезанной : [{'OK' if prompt_bounded else 'ОШИБКА'}]")
    ok = ok and prompt_bounded

    # 3. Модель пересказала задачу: раздел заменяется полным исходным текстом.
    sections = {HANDOFF_HEADINGS[0]: "Собрать цикл loop_team (кратко)."}
    problems = _verify_model_sections(sections, long_task, steps, step_idx)
    task_fixed = bool(problems) and sections[HANDOFF_HEADINGS[0]].endswith(long_task)
    print(f"  пересказанная задача восстановлена     : [{'OK' if task_fixed else 'ОШИБКА'}]")
    ok = ok and task_fixed

    # 4. Модель отметила выполненным текущий шаг: отметки пересобираются из плана.
    wrong = "\n".join(f"- [x] Шаг {idx + 1}. {step}" for idx, step in enumerate(steps))
    sections = {HANDOFF_HEADINGS[1]: wrong}
    problems = _verify_model_sections(sections, "Задача", steps, step_idx)
    marks = sections[HANDOFF_HEADINGS[1]]
    marks_fixed = (bool(problems) and "- [x] Шаг 1." in marks and "- [ ] Шаг 3." in marks
                   and "текущий шаг 3 помечен выполненным" in marks)
    print(f"  отметки исправлены (шаг 3 снова [ ])   : [{'OK' if marks_fixed else 'ОШИБКА'}]")
    ok = ok and marks_fixed

    # 5. Модель пропустила шаг плана: раздел пересобирается со всеми шагами.
    partial = ("- [x] Шаг 1. Подготовить конфигурацию\n"
               "- [ ] Шаг 2. Написать ревьюера")
    sections = {HANDOFF_HEADINGS[1]: partial}
    problems = _verify_model_sections(sections, "Задача", steps, step_idx)
    step_added = (bool(problems)
                  and "Шаг 3. Написать менеджер Handoff" in sections[HANDOFF_HEADINGS[1]])
    print(f"  пропущенный шаг плана возвращён       : [{'OK' if step_added else 'ОШИБКА'}]")
    ok = ok and step_added

    # 6. Честный ответ модели править нельзя: ложных срабатываний быть не должно.
    honest = _format_plan_checklist(steps, step_idx)
    sections = {HANDOFF_HEADINGS[0]: long_task, HANDOFF_HEADINGS[1]: honest}
    problems = _verify_model_sections(sections, long_task, steps, step_idx)
    untouched = not problems and sections[HANDOFF_HEADINGS[1]] == honest
    print(f"  честный ответ модели не тронут         : [{'OK' if untouched else 'ОШИБКА'}]")
    ok = ok and untouched

    return ok


def _self_check() -> bool:
    """Демонстрация критерия готовности Шага 3: создание handoff.md и его загрузка.

    Это не автоматический тест (pytest/unittest не используются), а ручной запуск
    `python handoff_manager.py`. Все файлы создаются во ВРЕМЕННОЙ папке: реальные
    handoff.md и handoff.bak проекта не затираются, поэтому демонстрацию можно
    запускать даже посреди рабочей сессии.
    """
    print("=" * 62)
    print("handoff_manager.py — ШАГ 3: самопроверка менеджера Handoff")
    print("=" * 62)
    print(f"MODEL_NAME      : {getattr(team_config, 'MODEL_NAME', '')}")
    print(f"OPENAI_BASE_URL : {getattr(team_config, 'OPENAI_BASE_URL', '')}")
    print(f"HANDOFF_FILE    : {_handoff_file_path()}")
    print(f"HANDOFF_BACKUP  : {_backup_file_path()}")
    print("-" * 62)
    print("Демонстрация идёт во временной папке, рабочие файлы не трогаем.")
    print("Вызов модели может занять несколько минут; если сервер выключен,")
    print("файл соберётся автоматически — это штатный запасной режим.")

    # Сначала проверки, которым модель не нужна: они честны при любом сервере.
    ok = _verification_demo()
    temp_dir: Path | None = None
    saved_file = getattr(team_config, "HANDOFF_FILE", None)
    saved_backup = getattr(team_config, "HANDOFF_BACKUP", None)
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="team_loop_handoff_"))
        team_config.HANDOFF_FILE = temp_dir / "handoff.md"
        team_config.HANDOFF_BACKUP = temp_dir / "handoff.bak"

        task = ("Собрать автономный цикл loop_team: Аналитик -> Исполнитель <-> "
                "Изолированный Ревьюер с механизмом Handoff.")
        plan = [
            "Создать team_config.py с лимитами токенов",
            "Создать isolated_reviewer.py с объективной проверкой",
            "Создать handoff_manager.py с сохранением и загрузкой состояния",
        ]
        messages = [
            {"role": "assistant", "content": "Создал isolated_reviewer.py, проверил синтаксис."},
            {"role": "user", "content": "Ревьюер принят. Переходим к шагу 3."},
        ]
        blocker = ("Контекст заполнен: использовано почти всё окно модели, "
                   "нужен перенос состояния в новый чат.")

        print("\nШАГ ДЕМОНСТРАЦИИ 1. generate_handoff(...) создаёт handoff.md:")
        document = generate_handoff(task, plan, 2, messages, blocker)
        handoff_path = _handoff_file_path()

        if document is None:
            print("  [ОШИБКА] generate_handoff вернул None — файл не записан.")
            return False
        if not handoff_path.is_file():
            print(f"  [ОШИБКА] файла нет на диске: {handoff_path}")
            return False

        size = handoff_path.stat().st_size
        print(f"  файл создан: {handoff_path} ({size} байт, UTF-8)")
        missing = [heading for heading in HANDOFF_HEADINGS if heading not in document]
        print(f"  все 4 раздела шаблона на месте: "
              f"[{'OK' if not missing else 'ОШИБКА: нет ' + ', '.join(missing)}]")
        ok = ok and not missing
        if AUTO_ASSEMBLED_MARK in document:
            print("  [ВНИМАНИЕ] часть разделов собрана автоматически: модель недоступна")
            print("  или ответ не соответствовал шаблону. Это штатный запасной режим.")
        if AUTO_CORRECTED_MARK in document:
            print("  [ВНИМАНИЕ] модель исказила задачу или отметки плана — эти разделы")
            print("  исправлены программой по исходным данным. Продолжать работу")
            print("  можно по ним: там дословная задача и настоящие отметки [x]/[ ].")
        _print_document(handoff_path)

        print("\nШАГ ДЕМОНСТРАЦИИ 2. load_handoff_if_exists() читает и переименовывает:")
        loaded = load_handoff_if_exists()
        if loaded is None:
            print("  [ОШИБКА] load_handoff_if_exists вернул None, хотя файл был.")
            return False
        print(f"  текст получен: {len(loaded)} символов, "
              f"[{'OK' if loaded.strip() == document.strip() else 'ОШИБКА: текст не совпал'}]")
        ok = ok and loaded.strip() == document.strip()
        still_there = handoff_path.exists()
        backup_path = _backup_file_path()
        print(f"  handoff.md убран с дороги : [{'OK' if not still_there else 'ОШИБКА'}]")
        print(f"  handoff.bak создан        : [{'OK' if backup_path.is_file() else 'ОШИБКА'}]")
        ok = ok and (not still_there) and backup_path.is_file()

        print("\nШАГ ДЕМОНСТРАЦИИ 3. Повторная загрузка из резервного файла:")
        again = load_handoff_if_exists(include_backup=True)
        resumed = again is not None and again.strip() == document.strip()
        print(f"  handoff.bak доступен для продолжения: [{'OK' if resumed else 'ОШИБКА'}]")
        ok = ok and resumed

        print("\nШАГ ДЕМОНСТРАЦИИ 4. Второй handoff перезаписывает старый .bak:")
        second = generate_handoff(task, plan, 1, messages, "Проверка перезаписи .bak.")
        loaded_second = load_handoff_if_exists()
        replaced = (second is not None and loaded_second is not None
                    and backup_path.is_file())
        print(f"  .bak перезаписан без ошибки: [{'OK' if replaced else 'ОШИБКА'}]")
        ok = ok and replaced

    except OSError as exc:
        print(f"[ОШИБКА] не удалось подготовить временную папку: {exc}")
        return False
    except Exception as exc:  # последняя страховка точки входа
        print(f"[ОШИБКА] самопроверка завершилась непредвиденной ошибкой: "
              f"{type(exc).__name__}: {exc}")
        return False
    finally:
        # Возвращаем боевые пути и убираем временные файлы.
        team_config.HANDOFF_FILE = saved_file
        team_config.HANDOFF_BACKUP = saved_backup
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n" + "-" * 62)
    print("ИТОГ: " + ("handoff создаётся, читается и переименовывается в .bak."
                      if ok else "есть замечания — см. строки [ОШИБКА] выше."))
    return ok


if __name__ == "__main__":
    # В точке входа включаем UTF-8 для stdout/stderr согласно правилу 2 PLAN.md.
    _ensure_console_utf8()
    sys.exit(0 if _self_check() else 1)
