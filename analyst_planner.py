# Путь: F:\Projects\team_loop\analyst_planner.py
# Назначение: ШАГ 4 — Аналитик и декомпозиция плана. Берёт задачу пользователя
#             (или handoff.md Шага 3), спрашивает локальную модель и отдаёт
#             главному циклу Шага 5 чистый список шагов list[str].
# Кодировка исходника: UTF-8 (в Python 3 она включена по умолчанию).
"""
analyst_planner.py — ШАГ 4: Аналитик и декомпозиция плана.

Часть автономного цикла loop_team движка Strata
(Аналитик -> Исполнитель <-> Изолированный Ревьюер, механизм Handoff).

Возможности (см. PLAN.md, ШАГ 4):
  * build_execution_plan(user_task) — сначала вызывает load_handoff_if_exists()
    из Шага 3. Если handoff найден, Аналитик получает его содержимое с задачей
    «Проанализируй handoff.md и верни список ТОЛЬКО ОСТАВШИХСЯ невыполненных
    шагов для завершения проекта». Если handoff не найден, Аналитик получает
    текст задачи и разбивает её на атомарные последовательные шаги.
  * parse_plan_steps(answer) — надёжный парсер ответа модели. Модель заставляют
    отвечать JSON-массивом строк ["Шаг 1", "Шаг 2"], но локальные модели часто
    пишут нумерованным списком (1. ...), добавляют вступления и оборачивают
    ответ в блоки ```. Парсер выдерживает всё это, чистит пункты от нумерации,
    Markdown и кавычек и возвращает список строк без дублей.
  * Функция возвращает кортеж (список_шагов, текст_handoff_или_None, токены).

Главный принцип этого шага: у главного цикла всегда должен быть план. Падение
Аналитика остановило бы всю работу, поэтому любая неполадка (сервер модели
выключен, модель ответила не по формату, список оказался пустым) превращается
в предупреждение на русском языке в stderr, а планом становится сама поставленная
задача — один шаг с исходным текстом. Цикл Шага 5 в таком режиме продолжает
работать, а человек видит в консоли, что декомпозиции не было и почему.

Второй принцип: handoff — это точка продолжения работы, и Аналитик не имеет права
её потерять. load_handoff_if_exists() забирает handoff.md и убирает его в
handoff.bak, поэтому если план построить не удалось (модель молчит), прочитанный
текст возвращается обратно в handoff.md через finally при любом неуспешном выходе,
включая исключение парсера и прерывание пользователем. KeyboardInterrupt и SystemExit
не подавляются: файл восстанавливается до передачи прерывания вызывающему коду.
Следующий запуск снова продолжит работу с того же места, а не начнёт её заново.

Совместимость с Windows и защита от падения (правило 2 PLAN.md):
  * pathlib.Path для путей, явный encoding="utf-8" при восстановлении handoff.md;
  * пути handoff.md и handoff.bak читаются из team_config в момент вызова, поэтому
    работают переопределения из .env и запуск из любого каталога;
  * запрос к модели идёт через стандартную библиотеку (urllib), новых зависимостей
    нет; любая сетевая ошибка превращается в понятное русское сообщение, а не в
    traceback;
  * в промпт попадает обрезанный по длине текст: запрос Аналитика идёт в ту же
    модель с тем же окном контекста и не должен его переполнить;
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

# Модуль может быть запущен из любого каталога (python F:\\...\\analyst_planner.py),
# поэтому каталог проекта явно добавляется в sys.path: без этого import team_config
# работал бы только при запуске из папки проекта.
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

try:
    import team_config
except ImportError as _team_config_import_error:
    raise ImportError(
        "Не найден модуль team_config.py (ШАГ 1). Файл analyst_planner.py должен "
        f"лежать в той же папке, что и team_config.py: {_MODULE_DIR}"
    ) from _team_config_import_error

try:
    import handoff_manager
except ImportError as _handoff_import_error:
    raise ImportError(
        "Не найден модуль handoff_manager.py (ШАГ 3). Файл analyst_planner.py должен "
        f"лежать в той же папке, что и handoff_manager.py: {_MODULE_DIR}"
    ) from _handoff_import_error

__all__ = [
    "ANALYST_SYSTEM_PROMPT",
    "HANDOFF_ANALYSIS_INSTRUCTION",
    "parse_plan_steps",
    "build_execution_plan",
]

# ---------------------------------------------------------------------------
# Настройки, тексты промпта и границы безопасности
# ---------------------------------------------------------------------------

# Системный промпт Аналитика. Основной формат — JSON-массив: его модель портит
# реже всего. Нумерованный список разрешён явно (так требует ТЗ), и парсер
# разбирает оба варианта; вступления и блоки ``` запрещены, потому что их всё
# равно приходится вырезать.
ANALYST_SYSTEM_PROMPT = (
    "Ты — Аналитик в автономном цикле разработки. Ты не пишешь код, а раскладываешь "
    "работу на понятные последовательные шаги, которые по очереди выполнит "
    "Исполнитель. "
    "Отвечай строго на русском языке. В ответе верни ТОЛЬКО список шагов, без "
    "вступлений, пояснений, итогов и не оборачивай ответ в блоки кода. "
    "Основной формат ответа — JSON-массив строк: [\"Шаг 1\", \"Шаг 2\", \"Шаг 3\"]. "
    "Допустимый запасной формат — нумерованный список, где каждый шаг начинается с "
    "цифры: «1. ...», «2. ...». "
    "Каждый шаг — одно законченное проверяемое действие длиной до 300 символов: "
    "без подпунктов, без переносов строк и без слов «затем» и «потом» внутри шага. "
    "Шагов должно быть от 2 до 5; для очень большой задачи — максимум 8. "
    "Не придумывай шаги, которых нет в задаче или в handoff, и не объединяй несколько "
    "разных действий в один шаг."
)

# Дословная формулировка задачи для ветки handoff (ТЗ, PLAN.md, ШАГ 4, п.1).
HANDOFF_ANALYSIS_INSTRUCTION = (
    "Проанализируй handoff.md и верни список ТОЛЬКО ОСТАВШИХСЯ невыполненных шагов "
    "для завершения проекта"
)

# Сколько секунд ждём ответ локальной модели. Декомпозиция короткая, но локальная
# модель на CPU отвечает медленно; ожидание обязано остаться конечным, иначе
# зависший вызов остановил бы весь цикл Шага 5.
LLM_TIMEOUT_SECONDS = 300

# Ограничения промпта: окно контекста у Аналитика то же, что у остальных участников
# цикла, поэтому длинные тексты обрезаются. В handoff.md (его пишет Шаг 3) эти
# ограничения не влияют — здесь режется только запрос.
MAX_TASK_CHARS = 6000
MAX_HANDOFF_CHARS = 12000
MAX_ERROR_CHARS = 4000

# Разумный предел длины одного шага и всего списка. Модель, оставленная без рамки,
# может выдать пятьдесят микрошагов, и цикл Шага 5 будет выполнять их часами.
MAX_STEP_CHARS = 500
MAX_PLAN_STEPS = 12

# Строка длиннее этого порога в режиме «обычных строк без нумерации» пунктом плана
# не считается: скорее всего, это абзац рассуждений модели, а не шаг.
MAX_PLAIN_LINE_CHARS = 300

# Префикс сообщений о том, что план получить не удалось (сервер выключен, таймаут,
# ответ не по формату). Он попадает только в предупреждения stderr.
MODEL_ERROR_PREFIX = "План не получен: "

# Пометка в предупреждении, когда план собран без декомпозиции. Две формы —
# строчная (в середине фразы) и с большой буквы (отдельное предложение):
# str.capitalize() понижает регистр остальных букв, поэтому подстановку делаем
# готовой строкой, а не методом.
DEGRADED_NOTICE = "план не разбит на шаги: единственным шагом стала сама задача"
DEGRADED_NOTICE_SENTENCE = (
    "План не разбит на шаги: единственным шагом стала сама поставленная задача"
)

# ---------------------------------------------------------------------------
# Регулярные выражения парсера
# ---------------------------------------------------------------------------

# Markdown-украшения по краям строки: заголовки «#», цитаты «>», **жирный**,
# __подчёркивание__. Одинарные звёздочки и бэктики здесь не вырезаются:
# первые — маркеры списка, вторые убирает _final_polish.
_DECOR_HEAD_RE = re.compile(r"^(?:[#>\s]|(?:\*\*|__))+")
_DECOR_TAIL_RE = re.compile(r"(?:\*\*|__|\*|_|\s)+$")

# Префикс пункта списка: «1.», «1)», «1:», «1 -», «Шаг 3:», «- », «* », «• »,
# чекбокс «[ ]» и «[x]». Именно он отличает пункт плана от прозы модели.
_STEP_PREFIX_RE = re.compile(
    r"^(?:"
    r"шаг\s*(?:№|#)?\s*\d+\s*[:.\)\-–—]?"
    r"|\d+\s*[.)\]:]"
    r"|\d+\s*[-–—]"
    r"|[-*+\u2022\u25cf\u25e6]"
    r"|\[[\s xX✓✔☐☑]\]"
    r")\s*",
    re.IGNORECASE,
)

# Разделительные строки Markdown: «---», «***», «===».
_SEPARATOR_LINE_RE = re.compile(r"^[-*_=\s]{3,}$")

# Признак того, что в тексте есть список Markdown или нумерованный список.
# Нужен для защиты парсера JSON: строка чекбокса «- [ ] Сделать файл» содержит
# «[ ]», что является корректным пустым JSON-массивом. Без этой проверки такой
# список был бы принят за ответ модели «шагов не осталось».
_LIST_MARKER_RE = re.compile(
    r"^\s*(?:[-*+\u2022\u25cf\u25e6]|\d+\s*[.)\]:]|\[[\s xX✓✔☐☑]\])",
    re.MULTILINE,
)

# Строки-обёртки, которые модель пишет вместо шагов («Вот план шагов:», «Готово»).
_PREAMBLE_HEAD_RE = re.compile(
    r"^(?:вот|ниже|итого|готово|конечно|разумеется|отлично|результат|вывод)",
    re.IGNORECASE,
)
_PREAMBLE_ONLY_RE = re.compile(
    r"^(?:план|список|шаги|план\s+шагов|план\s+работы|список\s+шагов)[:.]?$",
    re.IGNORECASE,
)

# Хвостовой мусор пункта: двоеточия, точки с запятой, тире, звёздочки Markdown.
# Скобки и кавычки здесь НЕ вырезаются: шаг «Реализовать calc(a, b)» или
# «Создать файл "config.py"» потерял бы последний символ, и план стал бы
# недословным. Кавычки снимаёт _unquote() — только когда они парные или
# остались от обрезанного вступления модели.
_TRAILING_JUNK_CHARS = " \t:;,.-–—*_`"

# Парные кавычки, которыми модель иногда оборачивает весь пункт.
_QUOTE_PAIRS = (("\"", "\""), ("'", "'"), ("«", "»"), ("“", "”"))

# Ключи словаря, в которых модель может спрятать текст шага.
_STEP_ITEM_KEYS = (
    "step", "шаг", "text", "текст", "title", "название", "action", "действие",
    "description", "описание", "task", "задача", "name",
)


# ---------------------------------------------------------------------------
# Вспомогательные функции: вывод, текст, границы
# ---------------------------------------------------------------------------

def _warn(message: str) -> None:
    """Пишет предупреждение в stderr и никогда не бросает исключение.

    Сообщение содержит кириллицу, а консоль Windows бывает в cp866/cp1251:
    при UnicodeEncodeError выводим ASCII-вариант с \\u-экраниками вместо падения.
    """
    stream = getattr(sys, "stderr", None)
    if stream is None or not hasattr(stream, "write"):
        return
    line = f"[analyst_planner] {message}\n"
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


def _skeleton(text) -> str:
    """Только буквы и цифры нижним регистром — устойчивая сверка шагов между собой.

    Нужна для удаления дублей: модель вполне может написать «1. Создать файл» и
    «Создать файл» — для человека это один шаг, и в плане он должен остаться один.
    """
    return re.sub(r"[^0-9A-Za-z\u0400-\u04FF]+", "", _as_text(text)).lower()


# ---------------------------------------------------------------------------
# Парсер ответа Аналитика
# ---------------------------------------------------------------------------

def _unquote(text: str) -> str:
    """Снимает парные кавычки вокруг шага и одиночную кавычку в начале.

    Одиночную ЗАКРЫВАЮЩУЮ кавычку не трогаем: в шаге «Создать файл "config.py"»
    она является частью имени файла, а не обрамлением пункта.
    """
    for opening, closing in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            return text[1:-1].strip()
    return text.lstrip("\"'«“").strip()


def _final_polish(text) -> str:
    """Приводит текст одного шага к чистому виду одной строкой."""
    cleaned = " ".join(_as_text(text).split())
    cleaned = cleaned.replace("`", "")
    cleaned = cleaned.strip(_TRAILING_JUNK_CHARS)
    cleaned = _unquote(cleaned)
    return cleaned.strip(_TRAILING_JUNK_CHARS)


def _strip_decorations(text: str) -> str:
    """Снимает с краёв строки Markdown-украшения: #, >, **, __.

    Одинарные звёздочки и дефисы слева не трогаем — это маркеры списка, их
    убирает _STEP_PREFIX_RE. Одинарные кавычки-бэктики снимает _final_polish.
    """
    result = _DECOR_HEAD_RE.sub("", text)
    result = _DECOR_TAIL_RE.sub("", result)
    return result.strip()


def _clean_step_line(line: str) -> tuple[str, bool]:
    """Чистит одну строку ответа и говорит, была ли это строка списка.

    Возвращает пару (текст_шага, есть_маркер_списка). Маркер — нумерация, «Шаг N»,
    точка или чекбокс в начале строки: он служит доказательством того, что строка
    действительно пункт плана, а не рассуждение модели.
    """
    text = _as_text(line).strip()
    if not text or _SEPARATOR_LINE_RE.match(text):
        return ("", False)

    had_marker = False
    # До трёх проходов: «- [ ] **Текст**» теряет сначала тире, потом чекбокс,
    # потом звёздочки — за один проход такие вложения не разбираются.
    for _ in range(3):
        before = text.strip()
        text = _strip_decorations(text)
        match = _STEP_PREFIX_RE.match(text)
        if match:
            had_marker = True
            text = text[match.end():]
        if text.strip() == before:
            break

    cleaned = _final_polish(text)
    if len(cleaned) > MAX_STEP_CHARS:
        cleaned = cleaned[:MAX_STEP_CHARS].rstrip()
    return (cleaned, had_marker)


def _is_preamble_line(text: str) -> bool:
    """True для служебных фраз модели («Вот план шагов:», «Готово», «Итого»)."""
    low = _as_text(text).strip().lower()
    if not low:
        return True
    if _PREAMBLE_ONLY_RE.match(low):
        return True
    if _PREAMBLE_HEAD_RE.match(low) and (low.endswith(":") or len(low) <= 40):
        return True
    return False


def _step_text_from_item(item) -> str:
    """Достаёт текст шага из элемента JSON-массива любого вида."""
    if item is None or isinstance(item, bool):
        return ""
    if isinstance(item, str):
        return _final_polish(item)
    if isinstance(item, (int, float)):
        return _final_polish(str(item))
    if isinstance(item, dict):
        lowered = {str(key).strip().lower(): value for key, value in item.items()}
        for key in _STEP_ITEM_KEYS:
            value = lowered.get(key)
            if isinstance(value, str) and value.strip():
                return _final_polish(value)
        # Подходящего ключа нет: собираем все строковые значения словаря.
        parts = [value.strip() for value in item.values()
                 if isinstance(value, str) and value.strip()]
        return _final_polish(" ".join(parts))
    if isinstance(item, (list, tuple)):
        parts = [_step_text_from_item(part) for part in item]
        return " ".join(part for part in parts if part)
    return _final_polish(str(item))


def _strip_fence_lines(text: str) -> str:
    """Убирает строки-маркеры блоков кода (``` и ~~~), не трогая содержимое."""
    lines = []
    for line in text.splitlines():
        if line.strip().lower().startswith(("```", "~~~")):
            continue
        lines.append(line)
    return "\n".join(lines)


def _find_json_array(text: str):
    """Ищет первый корректный JSON-массив в тексте с посторонней прозой.

    raw_decode разбирает значение, начиная с указанной позиции, и молча пропускает
    вложенность и скобки внутри строк, поэтому «[пример]» вступительной фразы не
    мешает найти настоящий массив дальше по тексту.

    Среди всех подходящих кусков выбирается тот, в котором больше всего
    содержательных шагов: модель могла оставить «[]» в пояснении и дать настоящий
    список ниже. Пустой массив не принимается, если в тексте есть маркеры
    списка: скорее всего, это чекбокс «- [ ]», а не ответ модели.
    """
    decoder = json.JSONDecoder()
    best = None
    best_score = -1
    for match in re.finditer(r"\[", text):
        try:
            parsed, _end = decoder.raw_decode(text, match.start())
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        score = len(_steps_from_json_items(parsed))
        if score > best_score:
            best, best_score = parsed, score

    if best is None:
        return None
    if best_score == 0 and _LIST_MARKER_RE.search(text):
        return None
    return best


def _find_json_object(text: str):
    """Ищет первый корректный JSON-объект {"steps": [...]} в тексте ответа."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _end = decoder.raw_decode(text, match.start())
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _steps_from_json_items(items: list) -> list[str]:
    """Превращает элементы JSON-массива в список текстов шагов."""
    steps = []
    for item in items:
        text = _step_text_from_item(item)
        if text:
            steps.append(text)
    return steps


def _parse_json_steps(text: str) -> tuple[list[str], bool]:
    """Разбирает ответ как JSON: массив строк или объект со списком шагов.

    Возвращает пару (шаги, формат_распознан). Пустой массив [] — это тоже
    распознанный формат: так модель честно сообщает, что шагов не осталось.
    """
    array = _find_json_array(text)
    if array is not None:
        return _steps_from_json_items(array), True

    obj = _find_json_object(text)
    if obj is not None:
        for value in obj.values():
            if isinstance(value, list) and value:
                steps = _steps_from_json_items(value)
                if steps:
                    return steps, True
    return ([], False)


def _parse_list_steps(text: str) -> tuple[list[str], bool]:
    """Разбирает ответ как список строк: с нумерацией или без неё.

    Сначала ищем строки с маркером пункта (1., -, [ ], «Шаг 1:») — это надёжный
    признак. Если маркеров нет вовсе, пробуем обычные строки, но только когда их
    минимум две и они короткие: иначе прозу модели легко принять за план.
    """
    marked: list[str] = []
    plain: list[str] = []
    for line in text.splitlines():
        cleaned, had_marker = _clean_step_line(line)
        if not cleaned:
            continue
        if had_marker:
            marked.append(cleaned)
        elif len(cleaned) <= MAX_PLAIN_LINE_CHARS:
            plain.append(cleaned)

    if marked:
        return (marked, True)

    candidates = [step for step in plain
                  if not _is_preamble_line(step) and not step.endswith(":")]
    if len(candidates) >= 2:
        return (candidates, True)
    return ([], False)


def _finalize_steps(steps: list[str]) -> list[str]:
    """Убирает пустые строки и дубли, обрезает список до разумного размера."""
    result: list[str] = []
    seen: set[str] = set()
    for step in steps:
        text = _final_polish(step)
        if not text:
            continue
        key = _skeleton(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)

    if len(result) > MAX_PLAN_STEPS:
        _warn(f"модель вернула {len(result)} шагов — оставлены первые "
              f"{MAX_PLAN_STEPS}, чтобы цикл не выполнял задачу сутками.")
        result = result[:MAX_PLAN_STEPS]
    return result


def _parse_plan_answer(answer: str) -> tuple[list[str], bool]:
    """Разбирает ответ модели и сообщает, удалось ли распознать формат.

    Формат важен не ради красоты: пустой список из JSON-массива [] означает
    «шагов не осталось», а пустой результат из прозы означает «модель ответила
    не по формату». Эти два случая требуют противоположных реакций.
    """
    text = _as_text(answer).replace("\ufeff", "")
    if not text.strip():
        return ([], False)

    text = _strip_fence_lines(text)
    try:
        steps, recognized = _parse_json_steps(text)
    except RecursionError:
        # Слишком глубокий JSON может сломать и raw_decode (массив/объект), и
        # извлечение вложенных шагов. Это повреждённый ответ, НЕ пустой план []:
        # иначе handoff был бы ошибочно принят за уже завершённый проект.
        return ([], False)
    if recognized:
        return (_finalize_steps(steps), True)

    steps, recognized = _parse_list_steps(text)
    if recognized:
        return (_finalize_steps(steps), True)
    return ([], False)


def parse_plan_steps(answer: str) -> list[str]:
    """Парсер ответа Аналитика: приводит его к чистому списку строк (ШАГ 4, п.2).

    Понимает JSON-массив строк, объект {"steps": [...]}, нумерованный список
    («1. ...», «1) ...», «Шаг 1: ...»), маркированные пункты и чекбоксы
    («- [ ] ...»). Снимает Markdown, кавычки и вступления модели, удаляет дубли.
    Если распознать список не удалось, возвращает пустой список — это сигнал
    вызывающему коду переходить в запасной режим, а не ошибка.
    """
    steps, _recognized = _parse_plan_answer(answer)
    return steps


# ---------------------------------------------------------------------------
# Промпт Аналитика и запрос к локальной модели
# ---------------------------------------------------------------------------

def _build_plan_messages(user_task: str, handoff_text: "str | None") -> list:
    """Собирает РОВНО два сообщения: system с правилами и user с данными.

    Ветки две (ТЗ, ШАГ 4, п.1): с handoff модель ищет только оставшиеся шаги,
    без handoff — разбивает исходную задачу на атомарные последовательные шаги.

    Текст задачи при наличии handoff не выбрасывается: это уточнение человека
    («продолжи выполнение задачи», «сначала почини тесты»). Без этого блока
    напечатанный при возобновлении промпт доходил бы только до консоли.
    """
    if handoff_text:
        user_text = (
            f"{HANDOFF_ANALYSIS_INSTRUCTION}.\n\n"
            "Содержимое handoff.md:\n"
            f"{_truncate(handoff_text, MAX_HANDOFF_CHARS)}\n\n"
            "Как читать handoff.md:\n"
            "- в разделе «# Статус плана» выполненные шаги помечены [x], "
            "невыполненные — [ ];\n"
            "- верни только шаги с [ ]: текущий и все оставшиеся, в исходном "
            "порядке и исходной формулировке;\n"
            "- шаги с [x] не включай, новых шагов не придумывай;\n"
            "- если невыполненных шагов нет, верни пустой JSON-массив [].\n\n"
            "Формат ответа — JSON-массив строк: [\"Шаг 1\", \"Шаг 2\"]"
        )
        note = _as_text(user_task).strip()
        if note:
            user_text += (
                "\n\nУточнение пользователя к возобновлению работы (учти его, но "
                "не выдумывай из него новых шагов, если оно противоречит handoff):\n"
                f"{_truncate(note, MAX_TASK_CHARS)}"
            )
        return [
            {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

    user_text = (
        "Проанализируй задачу и разбей её на атомарные последовательные шаги, "
        "которые Исполнитель выполнит строго по очереди.\n\n"
        "Задача:\n"
        f"{_truncate(user_task, MAX_TASK_CHARS)}\n\n"
        "Требования к списку:\n"
        "- от 2 до 5 шагов (для очень большой задачи — максимум 8);\n"
        "- каждый шаг проверяем: по его концу видно, сделан он или нет;\n"
        "- шаги идут в реальном порядке выполнения, от первого файла до финальной "
        "проверки;\n"
        "- ничего не выдумывай: только то, что следует из задачи.\n\n"
        "Формат ответа — JSON-массив строк: [\"Шаг 1\", \"Шаг 2\", \"Шаг 3\"]"
    )
    return [
        {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
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
    сервер не должен ронять планирование работы.

    Функция своя, а не импортированная из isolated_reviewer.py или
    handoff_manager.py: там это приватные функции своих шагов, и их изменение
    разорвало бы чужой код.
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
    except Exception as exc:  # последняя страховка: планирование не должно падать
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

    text = content
    if isinstance(text, (list, tuple)):
        # OpenAI-стиль: [{"type": "text", "text": "..."}, ...]
        parts = []
        for part in text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text") or part.get("content") or ""
                if isinstance(value, str):
                    parts.append(value)
        text = "".join(parts)
    elif text is not None and not isinstance(text, str):
        text = getattr(text, "text", None)
        text = _as_text(text)

    return _as_text(text).replace("\ufeff", "").strip()


# ---------------------------------------------------------------------------
# Работа с handoff.md (ШАГ 3) и его защита
# ---------------------------------------------------------------------------

def _handoff_file_path() -> Path:
    """Текущий путь к handoff.md (читается из team_config в момент вызова).

    Функция своя, а не handoff_manager._handoff_file_path(): обращаться к
    приватной функции чужого шага нельзя — её переименование сломало бы Аналитика.
    Относительный путь отсчитывается от каталога модуля, как и в Шаге 3, поэтому
    запуск из любой папки находит тот же самый файл.
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


def _load_handoff(include_backup: bool = False) -> "str | None":
    """Забирает handoff у Шага 3. Ошибка чтения не должна останавливать Аналитика."""
    loader = getattr(handoff_manager, "load_handoff_if_exists", None)
    if not callable(loader):
        _warn("в handoff_manager.py нет функции load_handoff_if_exists — "
              "работаю без handoff.")
        return None
    try:
        text = loader(include_backup=include_backup)
    except TypeError:
        # Совместимость с уже подключёнными реализациями и заглушками без нового
        # необязательного параметра: резервный файл в таком случае не запрашиваем.
        if include_backup:
            _warn("менеджер handoff не поддерживает явное чтение .bak.")
            return None
        try:
            text = loader()
        except Exception as exc:
            _warn(f"не удалось прочитать handoff.md: {type(exc).__name__}: {exc}. "
                  "Работаю без handoff.")
            return None
    except Exception as exc:
        _warn(f"не удалось прочитать handoff.md: {type(exc).__name__}: {exc}. "
              "Работаю без handoff.")
        return None

    if text is None:
        return None
    text = _as_text(text)
    if not text.strip():
        return None
    return text


def _restore_handoff_after_failure(handoff_text: "str | None") -> None:
    """Возвращает handoff.md на диск, если план построить не удалось.

    load_handoff_if_exists() забирает handoff.md и убирает его в handoff.bak. Если
    при этом модель не ответила, работа осталась бы без плана И без файла
    продолжения: следующий запуск начал бы всё заново. Возврат файла лишает нас
    только одного — он снова будет прочитан при следующем запуске, что здесь и
    нужно.
    """
    if not handoff_text:
        return
    path = _handoff_file_path()
    try:
        if path.exists():
            # Файл на месте (например, Шагу 3 не удалось его переименовать) —
            # писать нечего, ничего не теряется.
            return
    except OSError as exc:
        _warn(f"не удалось проверить {path}: {exc}. Handoff не восстановлен.")
        return

    try:
        path.write_text(handoff_text, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        reason = _as_text(exc.strerror) or _as_text(exc)
        _warn(
            f"не удалось вернуть handoff.md на диск: {reason}. Текст handoff "
            "сохранился в handoff.bak — при необходимости скопируй его обратно "
            "в handoff.md."
        )
        return

    _warn(
        f"{path} восстановлен из прочитанного текста: план не построен, поэтому "
        "следующий запуск продолжит работу с того же места."
    )


# ---------------------------------------------------------------------------
# Запасной режим: план без декомпозиции
# ---------------------------------------------------------------------------

def _degraded_plan(user_task: str, handoff_text: "str | None") -> list[str]:
    """План из одного шага, когда модель не разложила задачу на шаги.

    Единственный шаг — исходная задача дословно. Это не «красивый» план, но цикл
    Шага 5 может его выполнить, а человек увидит в консоли предупреждение о том,
    что декомпозиции не было.
    """
    task = _as_text(user_task).strip()
    if task:
        # Текст задачи сворачиваем только по пробелам: формулировка пользователя
        # должна дойти до Исполнителя дословно, включая знаки препинания.
        return [" ".join(task.split())]
    if handoff_text:
        return [
            "Прочитай handoff.md (или handoff.bak) и выполни незавершённые шаги "
            "плана, отмеченные как [ ]"
        ]
    return [
        "Уточни у пользователя исходную задачу: текст задачи не передан, "
        "а handoff.md не найден"
    ]


# ---------------------------------------------------------------------------
# Публичная функция Шага 4
# ---------------------------------------------------------------------------

def build_execution_plan(
    user_task: str = "", *, resume_backup: bool = False
) -> tuple[list[str], "str | None", int]:
    """Составляет план работы: список шагов, handoff и потраченные токены.

    Аргументы:
      user_task      — текст задачи пользователя;
      resume_backup  — разрешает читать handoff.bak только при явном возобновлении.

    Возвращает кортеж (steps, handoff_text, tokens):
      steps        — список шагов list[str], всегда непустой;
      handoff_text — содержимое handoff.md, если он был, иначе None;
      tokens       — сколько токенов стоил запрос Аналитика (0, если сервер
                     не ответил).

    Логика (ТЗ, PLAN.md, ШАГ 4, п.1):
      * handoff найден  -> модель ищет ТОЛЬКО оставшиеся невыполненные шаги;
      * handoff нет     -> модель разбивает задачу на атомарные шаги.

    Запасной режим: если модель недоступна или ответ нельзя разобрать, шаговым
    списком становится сама задача (один шаг), а в stderr пишется объяснение на
    русском. Handoff при этом возвращается на диск, чтобы работа не осталась
    без точки продолжения.
    """
    task = _as_text(user_task).strip()
    handoff_text = None
    tokens = 0
    plan_built = False
    handoff_from_backup = False

    # С момента загрузки handoff любой неуспешный выход обязан вернуть файл.
    # finally покрывает и незапланированные исключения, и KeyboardInterrupt /
    # SystemExit; последние намеренно не поглощаем обработчиком Exception.
    try:
        if resume_backup:
            try:
                handoff_from_backup = not _handoff_file_path().is_file()
            except OSError:
                # При ошибке проверки считаем источник первичным, чтобы не потерять
                # текст, если ниже он всё же будет прочитан.
                handoff_from_backup = False
        handoff_text = _load_handoff(include_backup=resume_backup)
        if not task and handoff_text is None:
            # Запрашивать у модели план для пустоты бессмысленно: получили бы
            # пять минут ожидания и мусор вместо плана.
            _warn(
                "текст задачи не передан и handoff.md не найден — "
                f"{DEGRADED_NOTICE}."
            )
            return (_degraded_plan(task, handoff_text), None, 0)

        messages = _build_plan_messages(task, handoff_text)
        response, error = _call_local_model(messages)

        if error is not None:
            _warn(f"{error} {DEGRADED_NOTICE_SENTENCE}.")
            return (_degraded_plan(task, handoff_text), handoff_text, 0)

        answer = _extract_answer_text(response)
        tokens = team_config.extract_used_tokens(response, messages)

        if not answer:
            _warn(
                f"{MODEL_ERROR_PREFIX}модель вернула пустой ответ без текста. Обычно "
                "так бывает, если модель ещё не загрузила weights или генерация "
                f"обрезалась нулевой длиной. {DEGRADED_NOTICE_SENTENCE}."
            )
            return (_degraded_plan(task, handoff_text), handoff_text, tokens)

        steps, recognized = _parse_plan_answer(answer)

        if not recognized:
            _warn(
                f"{MODEL_ERROR_PREFIX}ответ модели не похож ни на JSON-массив, ни на "
                f"нумерованный список. Ответ модели: {_truncate(answer, 800)} "
                f"{DEGRADED_NOTICE_SENTENCE}."
            )
            return (_degraded_plan(task, handoff_text), handoff_text, tokens)

        if not steps:
            if handoff_text is None:
                _warn(
                    f"{MODEL_ERROR_PREFIX}модель вернула пустой список шагов. "
                    f"{DEGRADED_NOTICE_SENTENCE}."
                )
                return (_degraded_plan(task, handoff_text), handoff_text, tokens)
            # Модель посмотрела на handoff и не нашла невыполненных шагов.
            # Придумывать работу за неё не будем: отдаём один контрольный шаг.
            _warn(
                "модель не нашла в handoff невыполненных шагов — план состоит из "
                "одного контрольного шага."
            )
            steps = [
                "Сверь результат с исходной задачей из handoff и заверши "
                "проект: невыполненных шагов в плане не осталось"
            ]

        plan_built = True
        return (steps, handoff_text, tokens)
    except Exception as exc:
        _warn(
            f"{MODEL_ERROR_PREFIX}непредвиденная ошибка планирования: "
            f"{type(exc).__name__}: {_truncate(_as_text(exc), 800)}. "
            f"{DEGRADED_NOTICE_SENTENCE}."
        )
        return (_degraded_plan(task, handoff_text), handoff_text, tokens)
    finally:
        if not plan_built and handoff_text:
            if handoff_from_backup:
                _warn("план не построен; исходный handoff.bak сохранён без изменений.")
            else:
                _restore_handoff_after_failure(handoff_text)


# ---------------------------------------------------------------------------
# Точка входа: демонстрация критерия готовности Шага 4
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


def _parser_demo() -> bool:
    """Проверка парсера без обращения к модели: ответы разные, результат один.

    Это не автоматический тест (pytest/unittest не используются), а наглядная
    демонстрация того, что парсер выдерживает реальные выверты локальных моделей.
    """
    print("ПРОВЕРКА ПАРСЕРА (модель не нужна):")

    cases = [
        (
            "JSON-массив строк",
            '["Создать модуль", "Проверить синтаксис", "Сделать коммит"]',
            ["Создать модуль", "Проверить синтаксис", "Сделать коммит"],
        ),
        (
            "JSON в блоке ``` с вступлением и финалом",
            "Вот план:\n```json\n[\"Шаг один\", \"Шаг два\"]\n```\nГотово!",
            ["Шаг один", "Шаг два"],
        ),
        (
            "Нумерованный список",
            "Вот план шагов:\n1. Подготовить конфигурацию\n2. Написать аналитика\n"
            "3. Проверить запуск",
            ["Подготовить конфигурацию", "Написать аналитика", "Проверить запуск"],
        ),
        (
            "Чекбоксы, жирный шрифт и «Шаг N»",
            "- [ ] **Создать файл**\n- [x] Проверить синтаксис\n**Шаг 3.** Сделать коммит",
            ["Создать файл", "Проверить синтаксис", "Сделать коммит"],
        ),
        (
            "Дубли и пустые строки удаляются",
            '["А", "", "   ", "а", "Б"]',
            ["А", "Б"],
        ),
        (
            "JSON-массив объектов",
            '[{"step": "Создать файл"}, {"шаг": "Проверить синтаксис"}]',
            ["Создать файл", "Проверить синтаксис"],
        ),
        (
            "Объект {\"steps\": [...]}",
            '{"steps": ["Первый шаг", "Второй шаг"]}',
            ["Первый шаг", "Второй шаг"],
        ),
        (
            "Обычные строки без нумерации",
            "Создать конфиг\nНаписать модуль\nПроверить запуск",
            ["Создать конфиг", "Написать модуль", "Проверить запуск"],
        ),
        ("Пустой ответ", "", []),
        ("Проза вместо плана", "Я не могу выполнить этот запрос.", []),
    ]

    ok = True
    for title, answer, expected in cases:
        got = parse_plan_steps(answer)
        passed = got == expected
        ok = ok and passed
        print(f"  {title}")
        print(f"    получено : {got}")
        print(f"    ожидалось: {expected}   [{'OK' if passed else 'ОШИБКА'}]")

    # Пустой JSON-массив — отдельный смысл: «шагов не осталось», а не «мусор».
    _steps, recognized_empty = _parse_plan_answer("[]")
    _steps, recognized_garbage = _parse_plan_answer("Ничего не могу сказать")
    formats_ok = recognized_empty and not recognized_garbage
    print(f"  [] распознаётся как формат, а проза — нет: "
          f"[{'OK' if formats_ok else 'ОШИБКА'}]")
    return ok and formats_ok


def _print_plan(plan: list) -> None:
    """Печатает план так, чтобы было видно тип и чистоту каждого шага."""
    print(f"    тип: {type(plan).__name__}, длина: {len(plan)}")
    for idx, step in enumerate(plan):
        kind = type(step).__name__
        print(f"    [{idx}] ({kind}) {step}")


def _plan_demo() -> bool:
    """Демонстрация build_execution_plan: обычная задача и работа из handoff.

    Демонстрация идёт с ВРЕМЕННЫМИ путями handoff, поэтому реальный handoff.md
    проекта не подхватывается и не затирается. Если сервер модели выключен,
    включается запасной режим — он тоже проверяется, и это штатное поведение.
    """
    print("\nПРОВЕРКА build_execution_plan (нужен локальный сервер модели;")
    print(" если он выключен, сработает запасной режим — это не ошибка кода):")

    ok = True
    saved_file = getattr(team_config, "HANDOFF_FILE", None)
    saved_backup = getattr(team_config, "HANDOFF_BACKUP", None)
    temp_dir: Path | None = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="team_loop_analyst_"))
        team_config.HANDOFF_FILE = temp_dir / "handoff.md"
        team_config.HANDOFF_BACKUP = temp_dir / "handoff.bak"
        handoff_path = _handoff_file_path()
        backup_path = temp_dir / "handoff.bak"

        print(f"  (временная папка демонстрации: {temp_dir})")

        print("\n  СЛУЧАЙ 1. Обычная задача, handoff отсутствует:")
        task = ("Собрать модуль аналитика для локального движка Strata: получение "
                "плана из задачи, разбор ответа модели и запуск из консоли.")
        plan, handoff_text, tokens = build_execution_plan(task)
        shape_ok = (
            isinstance(plan, list) and len(plan) >= 1
            and all(isinstance(step, str) and step.strip() for step in plan)
            and handoff_text is None and isinstance(tokens, int) and tokens >= 0
        )
        _print_plan(plan)
        print(f"    handoff_text: {handoff_text}, токены: {tokens}")
        print(f"    чистый list[str] и непустые шаги : [{'OK' if shape_ok else 'ОШИБКА'}]")
        ok = ok and shape_ok
        if plan == [task]:
            print("    [ВНИМАНИЕ] план совпадает с задачей целиком: модель недоступна")
            print("    или ответ не разобран. Это запасной режим Шага 4.")
        else:
            count_ok = 2 <= len(plan) <= 5
            print(f"    шагов 2-5 (критерий готовности)  : "
                  f"[{'OK' if count_ok else f'ВНИМАНИЕ: {len(plan)}'}]")

        print("\n  СЛУЧАЙ 2. Возобновление работы из handoff.md (без текста задачи):")
        handoff_document = (
            "# Исходная задача\n"
            "Собрать автономный цикл loop_team с механизмом Handoff.\n\n"
            "# Статус плана\n"
            "- [x] Шаг 1. Создать team_config.py\n"
            "- [x] Шаг 2. Создать isolated_reviewer.py\n"
            "- [ ] Шаг 3. Создать analyst_planner.py\n\n"
            "# Артефакты и файлы на диске\n"
            "team_config.py — лимиты токенов; isolated_reviewer.py — вердикт "
            "ПРИНЯТО/ДОРАБОТАТЬ.\n\n"
            "# Текущий контекст и блокер\n"
            "Остановились на шаге 3: нужен модуль Аналитика.\n"
        )
        handoff_path.write_text(handoff_document, encoding="utf-8")

        plan2, handoff_text2, tokens2 = build_execution_plan("")
        handoff_ok = (
            isinstance(plan2, list) and len(plan2) >= 1
            and all(isinstance(step, str) and step.strip() for step in plan2)
            and handoff_text2 is not None
            and handoff_text2.strip() == handoff_document.strip()
            and isinstance(tokens2, int) and tokens2 >= 0
        )
        _print_plan(plan2)
        print(f"    handoff передан в цикл            : "
              f"[{'OK' if handoff_text2 is not None else 'ОШИБКА'}]")
        print(f"    handoff подхвачен и убран в .bak  : "
              f"[{'OK' if (not handoff_path.exists() and backup_path.is_file()) else '—'}]")
        if handoff_path.exists():
            restored = False
            try:
                restored = handoff_path.read_text(encoding="utf-8").strip() == handoff_document.strip()
            except (OSError, UnicodeDecodeError, ValueError):
                restored = False
            print(f"    модель недоступна -> handoff.md возвращён на диск: "
                  f"[{'OK' if restored else 'ОШИБКА'}]")
            ok = ok and restored
        print(f"    план непустой и handoff прочитан  : "
              f"[{'OK' if handoff_ok else 'ОШИБКА'}]")
        ok = ok and handoff_ok

        print("\n  СЛУЧАЙ 3. Ни задачи, ни handoff (защита от пустого запуска):")
        # В запасном режиме Случая 2 handoff.md вернулся на диск; его нужно убрать,
        # иначе здесь снова читался бы handoff и проверка пустого запуска не удалась бы.
        for stale_path in (handoff_path, backup_path):
            try:
                stale_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"    [ВНИМАНИЕ] не удалось удалить {stale_path}: {exc}")

        plan3, handoff_text3, tokens3 = build_execution_plan("")
        empty_ok = (isinstance(plan3, list) and len(plan3) >= 1
                    and all(isinstance(step, str) and step.strip() for step in plan3)
                    and handoff_text3 is None and tokens3 == 0)
        _print_plan(plan3)
        print(f"    непустой план без обращения к модели: "
              f"[{'OK' if empty_ok else 'ОШИБКА'}]")
        ok = ok and empty_ok

    except OSError as exc:
        print(f"  [ОШИБКА] не удалось подготовить временную папку: {exc}")
        return False
    except Exception as exc:  # последняя страховка точки входа
        print(f"  [ОШИБКА] самопроверка завершилась непредвиденной ошибкой: "
              f"{type(exc).__name__}: {exc}")
        return False
    finally:
        team_config.HANDOFF_FILE = saved_file
        team_config.HANDOFF_BACKUP = saved_backup
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return ok


def _self_check() -> bool:
    """Демонстрация критерия готовности Шага 4."""
    print("=" * 62)
    print("analyst_planner.py — ШАГ 4: самопроверка Аналитика")
    print("=" * 62)
    print(f"MODEL_NAME      : {getattr(team_config, 'MODEL_NAME', '')}")
    print(f"OPENAI_BASE_URL : {getattr(team_config, 'OPENAI_BASE_URL', '')}")
    print(f"HANDOFF_FILE    : {_handoff_file_path()}")
    print("-" * 62)
    print("Предупреждения [analyst_planner] в консоли при недоступной модели —")
    print("часть проверки: так работает запасной режим, код не падает.")

    ok = _parser_demo()
    ok = _plan_demo() and ok

    print("\n" + "-" * 62)
    print("ИТОГ: " + ("парсер разбирает ответы модели, план возвращается чистым "
                      "list[str]." if ok else
                      "есть замечания — см. строки [ОШИБКА] выше."))
    return ok


if __name__ == "__main__":
    # В точке входа включаем UTF-8 для stdout/stderr согласно правилу 2 PLAN.md.
    _ensure_console_utf8()
    sys.exit(0 if _self_check() else 1)
