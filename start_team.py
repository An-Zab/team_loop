# Путь: F:\Projects\team_loop\start_team.py
# Назначение: интерактивный запуск команды team_loop. Проверяет, загружена ли
#             локальная модель, подставляет её имя и размер контекста в .env,
#             сам убирает с дороги handoff.md при новой задаче и запускает цикл.
# Кодировка исходника: UTF-8 (в Python 3 она включена по умолчанию).
"""
start_team.py — кнопка «Запустить» для всей команды team_loop.

Зачем он нужен. На машине две локальные модели: они по-разному называются и у
них разное контекстное окно (например 262 144 и 131 072 токенов). Раньше при
переходе с модели на модель приходилось руками править `.env` и помнить, что
`handoff.md` от старой задачи перехватит новую. Здесь это делает программа.

Шаги (сверху вниз, ровно по порядку):

  1. Открывает PowerShell (это делает `start_team.bat`) и печатает баннер.
  2. Спрашивает у сервера модели `GET /v1/models`:
       * модель загружена  -> печатает «Модель загружена», её имя и контекст;
       * модель не загружена (сервер не отвечает либо ни одна модель не в
         статусе loaded) -> просит загрузить модель и ждёт Y (повторить шаг 2)
         или N (выход).
  3. Подставляет в `.env` и в окружение процесса `MODEL_NAME` и
     `MAX_CONTEXT_TOKENS` — ровно то, что сегодня ответил сервер. Порог Handoff
     отдельно не настраивается: `team_config.py` всегда берёт от контекста 60 %.
  4. Спрашивает: «Работаем с новой задачей [Y] или продолжаем старую [N]?»
       * [Y] новая задача  -> удаляет `handoff.md` и `handoff.bak`, дальше
         обычное общение с чистым контекстом истории;
       * [N] старая задача -> оставляет `handoff.md` и `handoff.bak` в покое.
  5. Просит текст: для новой задачи — формулировку задачи, для старой — промпт
    («Продолжи выполнение задачи» по умолчанию, достаточно нажать Enter).
  6. Запускает `run_strata_team.py` в этом же окне и передаёт ему код возврата.

Правило чистого чата живёт не здесь, а в цикле (ШАГ 5): по завершении работы
роли при заполнении 60 % и более контекста цикл сам обновляет `handoff.md` и
останавливается. Тогда нужно просто снова запустить этот файл и ответить [N].

Совместимость с Windows (правило 2 PLAN.md): pathlib.Path, явный encoding="utf-8"
при чтении и записи `.env`, UTF-8 в консоли включается до первого вывода.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

__all__ = ["main"]

# Сколько секунд ждём ответ от /v1/models. Модель уже загружена или нет — сервер
# отвечает на этот запрос быстро; висящий дольше пяти секунд сервер означает,
# что поднимать модель никто не собирается.
MODELS_TIMEOUT_SECONDS = 5

# Файл настроек, который правит шаг 3. Относительный путь — от папки проекта.
ENV_FILE_NAME = ".env"

# Текст промпта по умолчанию, когда человек продолжает старую задачу и жмёт Enter.
DEFAULT_RESUME_PROMPT = "Продолжи выполнение задачи из handoff.md"

# Верхняя граница контекстного окна: защита от мусора в ответе сервера.
_MAX_REASONABLE_CONTEXT = 1048576

# Ответы «да» и «нет» на обеих раскладках. Отдельная буква «н» в «да» НЕ
# входит: на ЙЦУКЕН под ней клавиша Y, но по-русски с неё же начинается «нет».
# Рискованно угадывать: ошибка стоит удалённого handoff.md, поэтому «н» — это
# всегда «нет», а «да» по-русски пишут целиком («да») или на клавише Y латиницей.
_YES = {"y", "yes", "д", "да", "+"}
_NO = {"n", "no", "н", "нет", "т", "-"}

# Шаблон строки .env с нужным ключом (значение подставляется в {key}).
_ENV_KEY_TEMPLATE = r"^(\s*)(?:export\s+)?{key}\s*="


# ---------------------------------------------------------------------------
# Шаг 2: что сегодня стоит на сервере модели
# ---------------------------------------------------------------------------

def models_endpoint(base_url: str) -> str:
    """Превращает OPENAI_BASE_URL в адрес метода /models.

    OPENAI_BASE_URL обычно заканчивается на /v1 (модули команды сами дописывают
    /chat/completions). Хвостовой слэш и отсутствующий /v1 не ломают адрес.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/models"


def _model_status(entry: dict) -> str:
    """Статус модели из ответа сервера: строка, {"value": ...} или пустая строка."""
    status = entry.get("status")
    if isinstance(status, str):
        return status.strip().lower()
    if isinstance(status, dict):
        value = status.get("value")
        if isinstance(value, str):
            return value.strip().lower()
    return ""


def _model_context(entry: dict) -> int:
    """Размер контекстного окна модели: meta.n_ctx, n_ctx или 0."""
    for source in (entry.get("meta"), entry):
        if not isinstance(source, dict):
            continue
        value = source.get("n_ctx")
        if isinstance(value, bool):
            continue
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < tokens <= _MAX_REASONABLE_CONTEXT:
            return tokens
    return 0


def query_loaded_model(base_url: str, api_key: str = "EMPTY",
                       timeout: int = MODELS_TIMEOUT_SECONDS) -> "tuple[str, int] | None":
    """Возвращает пару (имя модели, контекст) для загруженной модели или None.

    «Загружена» — это запись со статусом loaded. Сервер, который не отвечает,
    тоже означает «не загружена»: крутить цикл ожидания с таймаутом в 5 секунд
    дешевле, чем гонять запросы к модели, которой нет.
    """
    endpoint = models_endpoint(base_url)
    if not endpoint:
        return None

    request = urllib.request.Request(endpoint, method="GET")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None

    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        status = _model_status(entry)
        # Статуса может и не быть — тогда сервер не различает загруженные модели,
        # и единственная запись в списке и есть то, что сейчас стоит в памяти.
        if status not in ("", "loaded"):
            continue
        return model_id.strip(), _model_context(entry)
    return None


# ---------------------------------------------------------------------------
# Шаг 3: запись MODEL_NAME и MAX_CONTEXT_TOKENS в .env
# ---------------------------------------------------------------------------

def read_env_text(env_path: Path) -> "str | None":
    """Читает .env как текст или возвращает None, если файл не читается/не UTF-8.

    UTF-16 пробуем ТОЛЬКО при наличии BOM: произвольный UTF-8 файл почти всегда
    «успешно» декодируется как UTF-16 и превращается в кашу, которая потом
    молча записалась бы обратно в .env. Тот же порядок, что в team_config.py.
    """
    try:
        data = env_path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError:
        return None

    utf16_boms = (b"\xff\xfe", b"\xfe\xff")
    encodings = ("utf-16", "utf-8-sig") if data.startswith(utf16_boms) else ("utf-8-sig",)
    for encoding in encodings:
        try:
            return data.decode(encoding).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return None


def apply_env_settings(text: str, settings: "dict[str, str]",
                       newline: str = "\n") -> str:
    """Обновляет нужные ключи в тексте .env, не трогая комментарии и порядок.

    Существующая строка `KEY=...` заменяется на новое значение (хвостовой
    комментарий сохраняется). Ключ, закомментированный строкой вида `# KEY=...`,
    остаётся комментарием: вместо него значение дописывается в конец файла.
    """
    remaining = dict(settings)
    lines = text.splitlines()
    result: list[str] = []
    patterns = {
        key: re.compile(_ENV_KEY_TEMPLATE.format(key=re.escape(key)))
        for key in remaining
    }

    for line in lines:
        key_found = None
        for key, pattern in patterns.items():
            if pattern.match(line):
                key_found = key
                break
        if key_found is None:
            result.append(line)
            continue

        value = str(remaining.pop(key_found))
        comment = re.search(r"\s+#.*$", line)
        tail = comment.group(0) if comment else ""
        result.append(f"{key_found}={value}{tail}")

    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.append("# Подставлено start_team.py из ответа сервера модели:")
        for key, value in remaining.items():
            result.append(f"{key}={value}")

    return newline.join(result) + newline


def write_env_settings(env_path: Path, settings: "dict[str, str]") -> bool:
    """Пишет настройки в .env с сохранением кодировки UTF-8 и переносов строк."""
    text = read_env_text(env_path)
    if text is None:
        return False

    try:
        raw = env_path.read_bytes()
    except OSError:
        raw = b""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    updated = apply_env_settings(text, settings, newline)

    try:
        env_path.write_bytes(updated.encode("utf-8"))
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Шаг 4: handoff.md и handoff.bak
# ---------------------------------------------------------------------------

def handoff_paths() -> "tuple[Path, Path]":
    """Пути handoff.md и handoff.bak так, как их видят Шаги 3-5."""
    paths: list[Path] = []
    try:
        import team_config
        names = ("HANDOFF_FILE", "HANDOFF_BACKUP")
        fallbacks = ("handoff.md", "handoff.bak")
        for index, name in enumerate(names):
            raw = str(getattr(team_config, name, "") or "").strip()
            path = Path(raw) if raw else _MODULE_DIR / fallbacks[index]
            paths.append(path if path.is_absolute() else _MODULE_DIR / path)
    except Exception:
        paths = [_MODULE_DIR / "handoff.md", _MODULE_DIR / "handoff.bak"]
    return (paths[0], paths[1])


def delete_handoff_files(paths: "list[Path]") -> "tuple[list[Path], list[Path]]":
    """Удаляет файлы состояния. Возвращает (удалённые, те, что не получилось)."""
    deleted: list[Path] = []
    failed: list[Path] = []
    for path in paths:
        try:
            if not path.is_file():
                continue
            path.unlink()
            deleted.append(path)
        except OSError:
            failed.append(path)
    return deleted, failed


def has_usable_handoff(paths: "list[Path]") -> "Path | None":
    """Первый пригодный файл состояния или None, если продолжать не из чего."""
    try:
        import handoff_manager
    except ImportError:
        return paths[0] if paths and paths[0].is_file() else None
    for path in paths:
        try:
            if handoff_manager.read_handoff_if_usable(path) is not None:
                return path
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Ввод человека
# ---------------------------------------------------------------------------

def _ensure_console_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def _normalize_answer(raw: str) -> str:
    return (raw or "").strip().lower().rstrip(".!")


def ask_yes_no(prompt: str, default: "str | None" = None) -> bool:
    """Задаёт вопрос с вариантами [Y]/[N] и возвращает True на «да».

    Принимает обе раскладки (y/д/н и n/т), пустая строка — default, если он задан.
    """
    suffix = " [Y/N] " if default is None else (" [Y/n] " if default else " [y/N] ")
    while True:
        try:
            answer = _normalize_answer(input(prompt + suffix))
        except EOFError:
            return bool(default)
        if answer in _YES:
            return True
        if answer in _NO:
            return False
        if not answer and default is not None:
            return default
        print("  Напиши Y (да) или N (нет).")


def ask_text(prompt: str, default: str = "") -> str:
    """Просит строку текста; пустой ответ возвращает default."""
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return default
        if answer:
            return answer
        if default:
            return default
        print("  Нужен хотя бы один символ — иначе циклу нечего выполнять.")


# ---------------------------------------------------------------------------
# Шаги запуска
# ---------------------------------------------------------------------------

def step_check_model(base_url: str, api_key: str) -> "tuple[str, int] | None":
    """Шаг 2: ждёт загруженную модель. Возвращает None, если человек выбрал выход."""
    print("\n" + "-" * 62)
    print("[шаг 2] Проверяю локальный сервер модели")
    print("-" * 62)

    while True:
        loaded = query_loaded_model(base_url, api_key)
        if loaded is not None:
            model_name, context = loaded
            print(f"  Модель загружена : {model_name}")
            if context:
                print(f"  Контекст         : {context} токенов")
            else:
                print("  Контекст         : сервер не прислал n_ctx, "
                      "оставляю MAX_CONTEXT_TOKENS из .env")
            return model_name, context

        print(f"  Сервер {base_url} не отвечает,")
        print("  либо на нём сейчас нет модели в статусе loaded.")
        print("  Загрузите модель в Strata (или другой локальный сервер).")
        if not ask_yes_no("  Повторить проверку шага 2?", default=True):
            print("  Выход.")
            return None


def step_write_env(model_name: str, context: int) -> None:
    """Шаг 3: подставляет имя модели и её контекст в .env и в окружение."""
    print("\n" + "-" * 62)
    print("[шаг 3] Подставляю значения в .env")
    print("-" * 62)

    settings: dict[str, str] = {}
    if model_name:
        settings["MODEL_NAME"] = model_name
    if context > 0:
        settings["MAX_CONTEXT_TOKENS"] = str(context)

    env_path = _MODULE_DIR / ENV_FILE_NAME
    if not settings:
        print("  Нечего записывать — работаю на том, что уже лежит в .env.")
        return

    if write_env_settings(env_path, settings):
        for key, value in settings.items():
            os.environ[key] = value
            print(f"  {key:<18} : {value}")
        print(f"  Файл обновлён    : {env_path}")
    else:
        print(f"  [ВНИМАНИЕ] не удалось записать {env_path}.")
        print("  Поправь значения в .env руками и повтори запуск.")


def step_reload_config() -> None:
    """Перечитывает team_config, чтобы баннер цикла показывал новые значения."""
    try:
        import team_config
        team_config._load_dotenv()
        team_config.MODEL_NAME = os.environ.get("MODEL_NAME", team_config.MODEL_NAME)
        raw = os.environ.get("MAX_CONTEXT_TOKENS", "")
        if raw.strip().isdigit():
            team_config.MAX_CONTEXT_TOKENS = int(raw.strip())
        team_config.HANDOFF_TOKEN_LIMIT = team_config._handoff_limit_from_ratio()
    except Exception:
        # Цикл запускается отдельным процессом и сам прочитает обновлённый .env.
        pass


def step_new_task_or_resume() -> bool:
    """Шаг 4: новая задача [Y] или продолжение старой [N].

    Новая задача — это чистый контекст, поэтому старые файлы состояния мешают:
    Аналитик (ШАГ 4) подхватил бы handoff.md и продолжил прежний план вместо
    новой задачи. При продолжении старой задачи файлы состояния, наоборот, нужны.
    """
    print("\n" + "-" * 62)
    print("[шаг 4] Новая задача или продолжение старой")
    print("-" * 62)

    while True:
        new_task = ask_yes_no(
            "  Работаем с новой задачей [Y] или продолжаем старую [N]?"
        )
        primary, backup = handoff_paths()

        if new_task:
            deleted, failed = delete_handoff_files([primary, backup])
            for path in deleted:
                print(f"  Удалён файл состояния: {path}")
            for path in failed:
                print(f"  [ВНИМАНИЕ] не удалось удалить {path} — закрой его "
                      "в редакторе.")
            if not deleted and not failed:
                print("  Файлов состояния и так нет — контекст будет чистым.")
            return True

        usable = has_usable_handoff([primary, backup])
        if usable is not None:
            print(f"  Оставляю точку продолжения: {usable}")
            return False

        print(f"  Продолжать не из чего: {primary} и {backup}")
        print("  пустые, не читаются или содержат неполный шаблон.")
        if ask_yes_no("  Тогда начнём новую задачу?", default=True):
            delete_handoff_files([primary, backup])
            return True
        # Человек настаивает: пусть цикл сам объяснит это внятной ошибкой.
        return False


def step_build_command(new_task: bool) -> "tuple[list[str], str]":
    """Шаг 5: просит текст и собирает команду запуска цикла."""
    print("\n" + "-" * 62)
    print("[шаг 5] Что запускаем")
    print("-" * 62)

    if new_task:
        task = ask_text("  Новая задача одной строкой > ")
        print(f"  Запускаю цикл по новой задаче ({len(task)} символов).")
        return ([sys.executable, "run_strata_team.py", task], task)

    prompt = ask_text(
        f"  Промпт (Enter = «{DEFAULT_RESUME_PROMPT}») > ",
        DEFAULT_RESUME_PROMPT,
    )
    print("  Продолжаю работу из handoff.md.")
    return ([sys.executable, "run_strata_team.py", "--resume", prompt], prompt)


def step_run(command: "list[str]") -> int:
    """Шаг 6: запускает цикл в этом же окне и возвращает его код возврата."""
    print("\n" + "-" * 62)
    print("[шаг 6] Запускаю цикл команды")
    print("-" * 62)
    try:
        return subprocess.call(command, cwd=str(_MODULE_DIR))
    except OSError as exc:
        print(f"  [ОШИБКА] не удалось запустить цикл: {exc}")
        return 1


def print_footer(code: int) -> None:
    """Подсказка после завершения цикла."""
    print("\n" + "=" * 62)
    if code == 0:
        print("Цикл завершился кодом 0.")
        print("Если он остановился на правиле Handoff (60 % контекста) — запусти")
        print("этот файл снова и ответь [N]: работа продолжится в чистом чате.")
    else:
        print(f"Цикл завершился кодом {code}. Причина — в строках выше.")
    print("=" * 62)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> int:
    """Прогоняет шаги 2-6 и возвращает код возврата цикла."""
    _ensure_console_utf8()

    print("=" * 62)
    print("start_team.py — интерактивный запуск команды team_loop")
    print("=" * 62)
    print(f"Папка проекта : {_MODULE_DIR}")

    try:
        import team_config
        base_url = str(getattr(team_config, "OPENAI_BASE_URL", ""))
        api_key = str(getattr(team_config, "OPENAI_API_KEY", "EMPTY"))
    except ImportError:
        print("[ОШИБКА] не найден team_config.py рядом со start_team.py.")
        return 1

    loaded = step_check_model(base_url, api_key)
    if loaded is None:
        return 1
    model_name, context = loaded

    step_write_env(model_name, context)
    step_reload_config()

    new_task = step_new_task_or_resume()
    command, _text = step_build_command(new_task)
    code = step_run(command)
    print_footer(code)
    return code


if __name__ == "__main__":
    _ensure_console_utf8()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[ПРЕРВАНО] запуск отменён клавишей Ctrl+C.")
        sys.exit(130)
