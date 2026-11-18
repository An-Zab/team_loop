# Путь: F:\Projects\team_loop\isolated_reviewer.py
# Назначение: ШАГ 2 — изолированный Ревьюер. Сначала объективная проверка файлов
#             на диске (существование + компиляция Python через py_compile), затем
#             отдельный stateless-запрос к локальной модели БЕЗ истории основного
#             чата: Ревьюер не видит метаний Исполнителя и опирается на факты.
# Кодировка исходника: UTF-8 (в Python 3 она включена по умолчанию).
"""
isolated_reviewer.py — ШАГ 2: изолированный Ревьюер с объективной проверкой.

Часть автономного цикла loop_team движка Strata
(Аналитик -> Исполнитель <-> Изолированный Ревьюер, механизм Handoff).

Возможности (см. PLAN.md, ШАГ 2):
  * run_objective_checks(file_paths) — проверяет, что файлы реально есть на диске,
    и для каждого .py запускает проверку синтаксиса отдельным процессом Python
    (`-m py_compile`). Возвращает текстовый отчёт.
  * review_step(step_goal, executor_report, modified_files) — сначала объективная
    проверка; при синтаксической ошибке вердикт выдаётся СРАЗУ, без обращения к
    модели. Если синтаксис чист — отправляется запрос ровно с двумя сообщениями
    (system + user) и temperature=0.0. Возвращает словарь
    {"accepted": bool, "feedback": str, "review_tokens": int}.

Почему это «изоляция»: сюда никогда не передаётся массив истории чата Исполнителя.
Запрос stateless (правило 3 PLAN.md) — второй инстанс модели в памяти не поднимается,
тот же локальный сервер получает короткий отдельный запрос.

Совместимость с Windows и защита от падения (правило 2 PLAN.md):
  * pathlib.Path для путей, явный encoding="utf-8" при работе с файлами;
  * subprocess получает sys.executable (остаёмся в активном .venv), text=True,
    encoding="utf-8", errors="replace" и timeout — зависший процесс не остановит цикл;
  * сетевой запрос к модели идёт через стандартную библиотеку (urllib), новых
    зависимостей нет; любая сетевая ошибка превращается в понятное русское сообщение
    в поле feedback, а не в traceback;
  * UTF-8 включается в stdout/stderr только в точке входа (if __name__ == "__main__":),
    при импорте модуля потоки процесса хоста не меняются.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

# Модуль может быть запущен из любого каталога (python F:\\...\\isolated_reviewer.py),
# поэтому каталог проекта явно добавляется в sys.path: без этого import team_config
# работал бы только при запуске из папки проекта.
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

try:
    import team_config
except ImportError as _team_config_import_error:
    raise ImportError(
        "Не найден модуль team_config.py (ШАГ 1). Файл isolated_reviewer.py должен "
        f"лежать в той же папке, что и team_config.py: {_MODULE_DIR}"
    ) from _team_config_import_error

__all__ = [
    "REVIEWER_SYSTEM_PROMPT",
    "OBJECTIVE_OK_MESSAGE",
    "run_objective_checks",
    "review_step",
]

# ---------------------------------------------------------------------------
# Настройки и тексты промпта
# ---------------------------------------------------------------------------

# Системный промпт Ревьюера — дословно из ТЗ (PLAN.md, ШАГ 2, п.2).
REVIEWER_SYSTEM_PROMPT = (
    "Ты — строгий, но прагматичный код-ревьюер. Оцени, выполнен ли текущий шаг плана. "
    "Не придирайся к стилю, проверяй только работоспособность и соответствие задаче "
    "шага. Если всё выполнено — первой строкой напиши строго слово ПРИНЯТО. "
    "Если есть баги или шаг не доделан — первой строкой напиши ДОРАБОТАТЬ "
    "и перечисли конкретные правки."
)

# Точная фраза успешной проверки (её ожидает и человек, и код следующих шагов).
OBJECTIVE_OK_MESSAGE = "Синтаксис всех файлов корректен"

# Сколько секунд ждём завершение py_compile. Проверка синтаксиса локального файла
# занимает доли секунды; минуты означают, что процесс завис (антивирус, сетевой
# диск, блокировка файла) — ждать бессмысленно, иначе цикл Шага 5 встанет навечно.
PY_COMPILE_TIMEOUT_SECONDS = 60

# Сколько секунд ждём ответ локальной модели. Локальная генерация на CPU бывает
# медленной, поэтому запас большой, но конечный.
LLM_TIMEOUT_SECONDS = 300

# Ограничения каждого блока stateless-промпта: отчёт может содержать полные
# исходники, а список проверенных не-Python файлов — тысячи путей. Ни один из
# этих входов не должен бесконтрольно занимать окно той же локальной модели.
MAX_STEP_GOAL_CHARS = 6000
MAX_EXECUTOR_REPORT_CHARS = 12000
MAX_CHECK_REPORT_CHARS = 4000

# Ограничение на длину текста ошибки в отчёте: полный вывод паники компилятора или
# многотысячный лог не должен съедать контекстное окно модели (см. ШАГ 1).
MAX_ERROR_CHARS = 4000

# Префикс сообщений о том, что сам вердикт модели получить не удалось
# (сервер выключен, таймаут, неверный формат ответа). По нему вызывающий код
# отличает «модель не ответила» от «модель ответила ДОРАБОТАТЬ».
MODEL_ERROR_PREFIX = "Проверка модели не выполнена: "

# Ошибки компиляции, которые действительно означают «код сломан синтаксически».
# Прочие ненулевые коды возврата py_compile — это проблемы окружения
# (нет прав на запись __pycache__, файл занят антивирусом): их честно называем
# отдельно, чтобы Ревьюер и человек не искали несуществующий SyntaxError.
_SYNTAX_ERROR_MARKERS = ("SyntaxError", "IndentationError", "TabError", "InvalidToken")

# Слова вердикта. Сравниваются по началу первой непустой строки после очистки
# от пунктуации, пробелов и звёздочек Markdown. Проверяются и русские, и английские
# варианты: локальные модели иногда отвечают по-английски, несмотря на промпт.
_ACCEPT_WORDS = ("ПРИНЯТО", "ПРИНЯЛ", "ACCEPTED", "ACCEPT", "APPROVED", "PASS")
_REJECT_WORDS = (
    "ДОРАБОТАТЬ",
    "ДОРАБОТКА",
    "НЕПРИНЯТО",
    "НЕ ПРИНЯТО",
    "REJECTED",
    "REJECT",
    "FAILED",
    "FAIL",
)

# Оставляет в строке только буквы и цифры (кириллица, латиница) и переводит в верхний
# регистр: «**ПРИНЯТО.**» и «принято» превращаются в один и тот же «ПРИНЯТО».
_VERDICT_CLEAN_RE = re.compile(r"[^0-9A-Za-z\u0400-\u04FF]+")
_ACCEPT_WORDS_CLEAN = tuple(_VERDICT_CLEAN_RE.sub("", word).upper() for word in _ACCEPT_WORDS)
_REJECT_WORDS_CLEAN = tuple(_VERDICT_CLEAN_RE.sub("", word).upper() for word in _REJECT_WORDS)


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


def _truncate(text: str, limit: int = MAX_ERROR_CHARS, *, keep_tail: bool = False) -> str:
    """Оставляет не более limit исходных символов и пояснение об обрезке.

    Для отчёта Исполнителя сохраняем начало и конец: итог и результаты тестов
    обычно идут после кода. Для ошибок компилятора по-прежнему важнее начало.
    """
    if len(text) <= limit:
        return text
    hidden = len(text) - limit
    notice = (
        f"\n... (текст обрезан: скрыто ещё {hidden} символов, "
        "чтобы не переполнять контекст модели)"
    )
    if keep_tail:
        tail_chars = limit // 2
        return text[:limit - tail_chars] + notice + "\n" + text[len(text) - tail_chars:]
    return text[:limit] + notice


def _normalize_file_paths(file_paths) -> list[Path]:
    """Приводит вход любого вида к списку путей без дублей.

    Допускает: None, одну строку, один Path, список/кортеж из str и Path.
    Ничего не бросает: мусорные элементы просто отбрасываются, потому что падение
    Ревьюера остановило бы весь цикл Шага 5.
    """
    if file_paths is None:
        return []
    if isinstance(file_paths, (str, Path)):
        candidates = [file_paths]
    else:
        try:
            candidates = list(file_paths)
        except TypeError:
            candidates = [file_paths]

    result: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        if item is None:
            continue
        raw = _as_text(item).strip()
        if not raw:
            continue
        try:
            path = Path(raw)
        except (TypeError, ValueError):
            # Недопустимые символы в пути (Windows) — молча пропускаем такой элемент.
            continue
        # Windows не различает регистр в именах файлов: один и тот же файл,
        # указанный дважды с разным регистром, проверяем один раз.
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _content_to_plain_text(content) -> str:
    """Приводит поле content ответа модели к простому тексту.

    OpenAI-совместимые серверы отдают строку, но встречаются и списки частей
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


def _compile_check(path: Path) -> tuple[str, str]:
    """Проверяет синтаксис одного .py файла отдельным процессом Python.

    Команда — ровно `sys.executable -m py_compile <путь>` (правило 2 PLAN.md:
    sys.executable, чтобы оставаться в активном .venv). Добавлены только
    errors="replace" (вывод дочернего процесса может быть в cp866/cp1251 и не должен
    ронять чтение) и timeout (зависший процесс не должен вешать цикл).

    Возвращает пару (вид, текст): ("ok", "") — синтаксис чист;
    ("syntax", текст) — реальная ошибка синтаксиса;
    ("other", текст) — проверить не удалось (нет прав, файл занят, таймаут).
    """
    command = [sys.executable, "-m", "py_compile", str(path)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PY_COMPILE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            "other",
            f"Проверка синтаксиса не завершилась за {PY_COMPILE_TIMEOUT_SECONDS} "
            "секунд и была остановлена.",
        )
    except OSError as exc:
        return (
            "other",
            f"Не удалось запустить проверку синтаксиса командой "
            f"{' '.join(command)}: {exc}",
        )

    if completed.returncode == 0:
        return ("ok", "")

    error_text = _as_text(completed.stderr).strip() or _as_text(completed.stdout).strip()
    if not error_text:
        error_text = (
            f"Компилятор завершился с кодом {completed.returncode} "
            "и не вывел текста ошибки."
        )
    kind = "syntax" if any(marker in error_text for marker in _SYNTAX_ERROR_MARKERS) else "other"
    return (kind, error_text)


def _collect_objective_checks(file_paths) -> dict:
    """Собирает результаты объективной проверки в структурированный словарь.

    Ключи: "report" (текст для промпта), "missing" (нет на диске),
    "syntax_errors" и "other_errors" (списки пар путь -> текст),
    "py_ok" (успешно скомпилированные .py), "other_files" (существующие не-.py).
    """
    result = {
        "report": "",
        "missing": [],
        "syntax_errors": [],
        "other_errors": [],
        "py_ok": [],
        "other_files": [],
    }

    paths = _normalize_file_paths(file_paths)
    if not paths:
        result["report"] = (
            "Файлы для объективной проверки не переданы — проверять синтаксис нечего. "
            "Оценивай шаг только по цели и отчёту исполнителя."
        )
        return result

    for path in paths:
        try:
            exists = path.is_file()
        except OSError:
            # Недопустимый путь (запрещённые символы Windows, недоступный сетевой диск).
            exists = False

        if not exists:
            result["missing"].append(str(path))
            continue

        if path.suffix.lower() != ".py":
            # Не Python: синтаксис не проверяем, но сам факт существования фиксируем.
            result["other_files"].append(str(path))
            continue

        kind, text = _compile_check(path)
        if kind == "ok":
            result["py_ok"].append(str(path))
        elif kind == "syntax":
            result["syntax_errors"].append((str(path), text))
        else:
            result["other_errors"].append((str(path), text))

    problems = []
    for path in result["missing"]:
        problems.append(f"[Файл не найден на диске] {path}")
    for path, text in result["syntax_errors"]:
        problems.append(f"[Ошибка компиляции Python] {path}\n{_truncate(text)}")
    for path, text in result["other_errors"]:
        problems.append(f"[Не удалось проверить синтаксис] {path}\n{_truncate(text)}")

    if problems:
        result["report"] = (
            f"Объективная проверка выявила проблем: {len(problems)}\n\n"
            + "\n\n".join(problems)
        )
        return result

    lines = []
    if result["py_ok"]:
        # Точная фраза из ТЗ без лишних пояснений, когда всё чисто и .py файлы были.
        lines.append(OBJECTIVE_OK_MESSAGE)
    else:
        lines.append(
            "Среди переданных файлов нет ни одного .py — проверять синтаксис "
            "Python нечего."
        )
    if result["other_files"]:
        lines.append(
            "Файлы существуют на диске (синтаксис не проверялся, это не .py): "
            + ", ".join(result["other_files"])
        )
    result["report"] = "\n".join(lines)
    return result


def _join_errors(items: list) -> str:
    """Собирает пары путь -> текст ошибки в один блок для поля feedback."""
    blocks = [f"{path}\n{text}" for path, text in items]
    return _truncate("\n\n".join(blocks))


def _build_review_messages(step_goal: str, executor_report: str, check_report: str) -> list:
    """Собирает РОВНО два сообщения: system и user.

    Истории основного чата здесь нет намеренно: изоляция Ревьюера в том, что он не
    видит оправданий и метаний Исполнителя, а получает цель шага, его отчёт и
    результат машинной проверки файлов. Размер каждого блока ограничен;
    обрезка отчёта сохраняет также его финал и явно обозначается в промпте.
    """
    user_text = (
        "Цель текущего шага:\n"
        f"{_truncate(step_goal or '(цель не передана)', MAX_STEP_GOAL_CHARS)}\n\n"
        "Отчет/код исполнителя:\n"
        f"{_truncate(executor_report or '(отчет не передан)', MAX_EXECUTOR_REPORT_CHARS, keep_tail=True)}\n\n"
        "Результат объективной проверки файлов:\n"
        f"{_truncate(check_report, MAX_CHECK_REPORT_CHARS)}"
    )
    return [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
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
    сервер не должен ронять автономный цикл.
    """
    url = _chat_endpoint(team_config.OPENAI_BASE_URL)
    if not url:
        return None, (
            f"{MODEL_ERROR_PREFIX}переменная OPENAI_BASE_URL пуста — непонятно, куда "
            "отправлять запрос. Укажи адрес локального сервера в файле .env "
            "(см. .env.example)."
        )

    payload = {
        "model": team_config.MODEL_NAME,
        "messages": messages,
        "temperature": 0.0,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    api_key = _as_text(team_config.OPENAI_API_KEY).strip()
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
            f"«{team_config.MODEL_NAME}» загружена на сервере."
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
    except Exception as exc:  # последняя страховка: цикл не должен падать
        return None, f"{MODEL_ERROR_PREFIX}непредвиденная ошибка запроса к {url}: {exc}"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, (
            f"{MODEL_ERROR_PREFIX}сервер вернул ответ не в формате JSON: "
            f"{_truncate(raw, 500)}"
        )

    if not isinstance(parsed, dict):
        return None, f"{MODEL_ERROR_PREFIX}сервер вернул неожиданный тип ответа: {type(parsed).__name__}"
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


def _parse_verdict(answer: str):
    """Определяет вердикт по первой непустой строке ответа.

    Возвращает True (ПРИНЯТО), False (ДОРАБОТАТЬ) или None, если первая строка
    вообще не похожа ни на одно из слов вердикта.
    """
    for line in answer.splitlines():
        cleaned = _VERDICT_CLEAN_RE.sub("", line).upper()
        if not cleaned:
            continue
        # Сначала «отрицательные» слова: безопаснее переспросить, чем принять мусор.
        for word in _REJECT_WORDS_CLEAN:
            if cleaned.startswith(word):
                return False
        for word in _ACCEPT_WORDS_CLEAN:
            if cleaned.startswith(word):
                return True
        return None
    return None


# ---------------------------------------------------------------------------
# Публичные функции Шага 2
# ---------------------------------------------------------------------------

def run_objective_checks(file_paths: list[str]) -> str:
    """Объективная проверка файлов без участия модели (ШАГ 2, п.1).

    1) Проверяет, существуют ли указанные файлы на диске.
    2) Для каждого .py запускает проверку синтаксиса отдельным процессом Python.
    3) Возвращает текстовый отчёт: либо точную фразу «Синтаксис всех файлов
       корректен», либо список найденных проблем с точным stderr компилятора.
    """
    return _collect_objective_checks(file_paths)["report"]


def review_step(step_goal: str, executor_report: str, modified_files: list[str] = None) -> dict:
    """Вердикт изолированного Ревьюера по одному шагу плана (ШАГ 2, п.2).

    Порядок: сначала машинная проверка файлов, и только при чистом синтаксисе —
    обращение к модели. Возвращает словарь
    {"accepted": bool, "feedback": str, "review_tokens": int}.
    Ключей меньше трёх не бывает никогда, поэтому вызывающий код (ШАГ 5) может
    безопасно читать все три.
    """
    goal = _as_text(step_goal)
    report = _as_text(executor_report)
    checks = _collect_objective_checks(modified_files)

    # Синтаксическая ошибка — вердикт сразу, модель не вызываем (требование ТЗ):
    # незачем тратить токены и время на оценку кода, который даже не компилируется.
    if checks["syntax_errors"]:
        return {
            "accepted": False,
            "feedback": f"Ошибка компиляции Python:\n{_join_errors(checks['syntax_errors'])}",
            "review_tokens": 0,
        }

    # Проверка не удалась по причинам окружения (нет прав, файл занят, таймаут).
    # Это не «код плохой», поэтому формулировка другая — иначе Исполнитель будет
    # впустую переписывать рабочий файл.
    if checks["other_errors"]:
        return {
            "accepted": False,
            "feedback": (
                "Не удалось проверить синтаксис Python (проблема окружения, а не кода). "
                f"Устрани её и повтори проверку:\n{_join_errors(checks['other_errors'])}"
            ),
            "review_tokens": 0,
        }

    # Файлов нет на диске: шаг объективно не выполнен, модель тоже не нужна.
    if checks["missing"]:
        listed = "\n".join(f"- {path}" for path in checks["missing"])
        return {
            "accepted": False,
            "feedback": (
                "Шаг не принят: указанные исполнителем файлы не найдены на диске:\n"
                f"{listed}\n\n"
                "Создай или сохрани эти файлы по указанным путям, затем повтори шаг."
            ),
            "review_tokens": 0,
        }

    # Только если Исполнитель вообще не передал путей: результаты компиляции и
    # точные ошибки выше должны сохранять приоритет и диагностическую точность.
    file_required = bool(re.search(
        r"(?:созда|записа|сохрани|измени|обнови|добави|реализу|напиши|файл|модул|скрипт|код)",
        goal.lower(),
    ))
    if file_required and not _normalize_file_paths(modified_files):
        return {
            "accepted": False,
            "feedback": (
                "Шаг требует файлового результата, но Исполнитель не передал ни одного "
                "пути к файлу. Используй write_file и передай путь для проверки."
            ),
            "review_tokens": 0,
        }

    messages = _build_review_messages(goal, report, checks["report"])
    response, error = _call_local_model(messages)
    if error is not None:
        # Модель недоступна: принимаем безопасный вариант — шаг НЕ принимается,
        # но в feedback лежит понятная человеку причина и что с ней делать.
        return {"accepted": False, "feedback": error, "review_tokens": 0}

    answer = _extract_answer_text(response)
    review_tokens = team_config.extract_used_tokens(response, messages)

    if not answer:
        return {
            "accepted": False,
            "feedback": (
                f"{MODEL_ERROR_PREFIX}модель вернула пустой ответ без текста. "
                "Обычно так бывает, если модель ещё не загрузила weights или ответ "
                "обрезался нулевой длиной генерации. Повтори шаг."
            ),
            "review_tokens": review_tokens,
        }

    verdict = _parse_verdict(answer)
    if verdict is None:
        # Формат вердикта нарушен: считать шаг принятым было бы небезопасно,
        # поэтому считаем не принятым и просим переформатировать ответ.
        return {
            "accepted": False,
            "feedback": (
                "Ревьюер не написал первой строкой ни «ПРИНЯТО», ни «ДОРАБОТАТЬ», "
                "поэтому шаг считается НЕ принятым (безопасный режим). "
                "Полный ответ ревьюера:\n\n" + answer
            ),
            "review_tokens": review_tokens,
        }

    return {"accepted": verdict, "feedback": answer, "review_tokens": review_tokens}


# ---------------------------------------------------------------------------
# Точка входа: автономная демонстрация критерия готовности Шага 2
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


def _print_result(title: str, result: dict) -> bool:
    """Печатает один вердикт и проверяет форму словаря. Возвращает True, если форма верна."""
    print(title)
    if not isinstance(result, dict):
        print("  [ОШИБКА] review_step вернул не словарь.")
        return False

    missing_keys = [key for key in ("accepted", "feedback", "review_tokens") if key not in result]
    if missing_keys:
        print(f"  [ОШИБКА] в словаре нет ключей: {', '.join(missing_keys)}")
        return False

    print(f"  accepted      : {result['accepted']}")
    print(f"  review_tokens : {result['review_tokens']}")
    feedback = _as_text(result["feedback"])
    short = feedback if len(feedback) <= 500 else feedback[:500] + " ... (дальше обрезано для вывода)"
    print("  feedback      :")
    for line in short.splitlines():
        print(f"    | {line}")

    form_ok = isinstance(result["accepted"], bool) and isinstance(result["feedback"], str)
    if not form_ok:
        print("  [ОШИБКА] неверные типы: accepted должен быть bool, feedback — str.")
    return form_ok


def _self_check() -> bool:
    """Автономная демонстрация Шага 2 на валидном и заведомо сломанном коде.

    Это не автоматический тест (pytest/unittest не используются), а ручной запуск
    `python isolated_reviewer.py`: создаёт временные файлы в папке временных файлов
    Windows, проверяет четыре случая и удаляет их. Файлы проекта не затрагиваются.
    """
    print("=" * 62)
    print("isolated_reviewer.py — ШАГ 2: самопроверка изолированного Ревьюера")
    print("=" * 62)
    print(f"MODEL_NAME      : {team_config.MODEL_NAME}")
    print(f"OPENAI_BASE_URL : {team_config.OPENAI_BASE_URL}")
    print("-" * 62)

    good_source = (
        "# -*- coding: utf-8 -*-\n"
        '"""Демонстрационный модуль: складывает два числа."""\n'
        "\n\n"
        "def add_numbers(first, second):\n"
        '    """Возвращает сумму двух чисел."""\n'
        "    return first + second\n"
    )
    broken_source = "def broken(:\n    return 1\n"

    temp_dir = None
    form_ok_all = True
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="team_loop_review_"))
        good_file = temp_dir / "good_step.py"
        good_file.write_text(good_source, encoding="utf-8")
        broken_file = temp_dir / "broken_step.py"
        broken_file.write_text(broken_source, encoding="utf-8")
        absent_file = temp_dir / "absent_file.py"

        print("СЛУЧАЙ 1. Пустой список файлов (проверять нечего):")
        print(f"  {run_objective_checks([])}")
        print(f"  {run_objective_checks(None)}")

        print("\nСЛУЧАЙ 2. Заведомо сломанный код (модель НЕ вызывается):")
        print(f"  {run_objective_checks([str(broken_file)])}")
        broken_result = review_step(
            "Создать модуль broken_step.py с корректным синтаксисом Python.",
            "Я создал файл broken_step.py, вот его содержимое:\n" + broken_source,
            [str(broken_file)],
        )
        form_ok_all &= _print_result("  Вердикт по сломанному файлу:", broken_result)
        expected_broken = (
            broken_result.get("accepted") is False
            and "Ошибка компиляции Python" in _as_text(broken_result.get("feedback"))
        )
        print(f"  Ожидалось accepted=False с текстом об ошибке компиляции: "
              f"[{'OK' if expected_broken else 'ОШИБКА'}]")
        form_ok_all &= expected_broken

        print("\nСЛУЧАЙ 3. Файла нет на диске (модель НЕ вызывается):")
        absent_result = review_step(
            "Создать модуль absent_file.py.",
            "Я создал файл absent_file.py.",
            [str(absent_file)],
        )
        form_ok_all &= _print_result("  Вердикт по отсутствующему файлу:", absent_result)
        expected_absent = absent_result.get("accepted") is False
        print(f"  Ожидалось accepted=False: [{'OK' if expected_absent else 'ОШИБКА'}]")
        form_ok_all &= expected_absent

        print("\nСЛУЧАЙ 4. Валидный код (вызывается локальная модель, может быть долгим):")
        good_result = review_step(
            "Создать модуль good_step.py с функцией add_numbers(first, second), "
            "которая возвращает сумму двух чисел.",
            "Я создал файл good_step.py, вот его содержимое:\n" + good_source,
            [str(good_file)],
        )
        form_ok_all &= _print_result("  Вердикт по валидному файлу:", good_result)
        feedback = _as_text(good_result.get("feedback"))
        if feedback.startswith(MODEL_ERROR_PREFIX):
            print("  [ВНИМАНИЕ] вердикт модели не получен: сервер модели недоступен.")
            print("  Запусти локальный сервер модели, укажи OPENAI_BASE_URL и MODEL_NAME")
            print("  в файле .env (шаблон — .env.example) и повтори запуск.")
        else:
            model_ok = isinstance(good_result.get("accepted"), bool)
            print(f"  Модель ответила, словарь корректной формы: "
                  f"[{'OK' if model_ok else 'ОШИБКА'}]")
            form_ok_all &= model_ok

    except OSError as exc:
        print(f"[ОШИБКА] не удалось подготовить временные файлы для самопроверки: {exc}")
        return False
    except Exception as exc:  # последняя страховка точки входа
        print(f"[ОШИБКА] самопроверка завершилась непредвиденной ошибкой: "
              f"{type(exc).__name__}: {exc}")
        return False
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n" + "-" * 62)
    print("ИТОГ: " + ("все проверки формы ответа пройдены."
                      if form_ok_all else "есть замечания — см. строки [ОШИБКА] выше."))
    return form_ok_all


if __name__ == "__main__":
    # В точке входа включаем UTF-8 для stdout/stderr согласно правилу 2 PLAN.md.
    _ensure_console_utf8()
    sys.exit(0 if _self_check() else 1)
