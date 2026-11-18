"""Проверка team_config.py: кодировки .env, валидация, пути handoff, stderr.

Запуск:  python F:\\Projects\\_scratch\\tc_check.py [<path-to-team_config.py>] [label]

Репозиторий не трогается: все .env-фикстуры пишутся в tempfile.mkdtemp()
и удаляются. Окружение чистится от конфигурационных переменных, чтобы
тесты были воспроизводимы на любой машине.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "team_config.py").resolve()
LABEL = sys.argv[2] if len(sys.argv) > 2 else "HEAD"

CONFIG_VARS = (
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_NAME", "MAX_CONTEXT_TOKENS",
    "HANDOFF_FREE_RATIO", "HANDOFF_TOKEN_LIMIT", "MAX_STEP_ATTEMPTS",
    "HANDOFF_FILE", "HANDOFF_BACKUP", "TOKEN_ESTIMATE_DIVISOR",
)

DRIVER = '''
import json, os, sys
if os.environ.get("TC_NO_DOTENV") == "1":
    sys.modules["dotenv"] = None          # «from dotenv import ...» -> ImportError
import team_config as tc
limit = tc.HANDOFF_TOKEN_LIMIT
msgs = [{"role": "user", "content": "x" * 300}, ("assistant", "y" * 300)]
out = {
    "MODEL_NAME": tc.MODEL_NAME,
    "OPENAI_BASE_URL": tc.OPENAI_BASE_URL,
    "MAX_CONTEXT_TOKENS": tc.MAX_CONTEXT_TOKENS,
    "HANDOFF_FREE_RATIO": tc.HANDOFF_FREE_RATIO,
    "HANDOFF_TOKEN_LIMIT": limit,
    "MAX_STEP_ATTEMPTS": tc.MAX_STEP_ATTEMPTS,
    "HANDOFF_FILE": str(tc.HANDOFF_FILE),
    "HANDOFF_BACKUP": str(tc.HANDOFF_BACKUP),
    "fallback_600": tc.extract_used_tokens(None, msgs),
    "crit_below": tc.is_context_critical(limit - 1),
    "crit_at": tc.is_context_critical(limit),
    "divisor": getattr(tc, "TOKEN_ESTIMATE_DIVISOR", None),
    "weird_keys": [k for k in os.environ if "\\ufeff" in k or "\\x00" in k],
}
print("JSON:" + json.dumps(out, ensure_ascii=False))
'''


def run(name, env_bytes=None, *, no_dotenv=False, io_encoding=None, cwd_outside=False,
        extra_env=None, checks=()):
    tmp = Path(tempfile.mkdtemp(prefix="tcfg_"))
    other = Path(tempfile.mkdtemp(prefix="tcfg_cwd_"))
    shutil.copy(SRC, tmp / "team_config.py")
    (tmp / "driver.py").write_text(DRIVER, encoding="utf-8")
    if env_bytes is not None:
        (tmp / ".env").write_bytes(env_bytes)

    env = dict(os.environ)
    for var in CONFIG_VARS + ("TC_NO_DOTENV", "PYTHONIOENCODING"):
        env.pop(var, None)
    if no_dotenv:
        env["TC_NO_DOTENV"] = "1"
    if io_encoding:
        env["PYTHONIOENCODING"] = io_encoding
    env.update(extra_env or {})

    proc = subprocess.run(
        [sys.executable, "-B", str(tmp / "driver.py")],
        cwd=(other if cwd_outside else tmp), env=env, capture_output=True,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    data = {}
    for line in out.splitlines():
        if line.startswith("JSON:"):
            data = json.loads(line[5:])
    res = {
        "name": name, "rc": proc.returncode, "json": data, "stderr": err,
        "traceback": "Traceback" in err, "module_dir": str(tmp),
    }
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(other, ignore_errors=True)

    problems = []
    if res["rc"] != 0:
        problems.append(f"rc={res['rc']}")
    if res["traceback"]:
        problems.append("Traceback в stderr")
    for description, check in checks:
        try:
            ok = check(res)
        except Exception as exc:
            ok = False
            description = f"{description} (исключение {exc!r})"
        if not ok:
            problems.append(description)
    res["problems"] = problems
    return res


U16 = ("MODEL_NAME=ps51-model\nMAX_CONTEXT_TOKENS=8192\n").encode("utf-16")
# UTF-8+BOM, пересохранённый в UTF-16: второй BOM декодер уже не снимает.
U16_DOUBLE_BOM = "\ufeffMODEL_NAME=twice-model\n".encode("utf-16")


def limit_of(r):
    return r["json"].get("HANDOFF_TOKEN_LIMIT")


def slash(value):
    """Нормализует путь для сравнений: прямые слеши и нижний регистр."""
    return str(value).replace("\\", "/").lower()


def main():
    scenarios = [
        run("no_env: без .env работают дефолты ТЗ", checks=(
            ("MODEL_NAME=qwen-local", lambda r: r["json"].get("MODEL_NAME") == "qwen-local"),
            ("MAX=32768", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 32768),
            ("limit=24576", lambda r: limit_of(r) == 24576),
            ("оценка 600 симв. -> 200", lambda r: r["json"].get("fallback_600") == 200),
            ("crit: limit-1 False, limit True",
             lambda r: r["json"].get("crit_below") is False and r["json"].get("crit_at") is True),
            ("HANDOFF_FILE в каталоге модуля",
             lambda r: r["json"].get("HANDOFF_FILE", "").lower().startswith(r["module_dir"].lower())),
        )),
        run("utf8_plain: обычный UTF-8 подхватывается",
            b"MODEL_NAME=latin-model\nMAX_CONTEXT_TOKENS=8192\n", checks=(
            ("MODEL_NAME=latin-model", lambda r: r["json"].get("MODEL_NAME") == "latin-model"),
            ("limit=6144", lambda r: limit_of(r) == 6144),
        )),
        run("utf16le_bom: .env из PowerShell 5.1 (`> .env`)", U16, checks=(
            ("MODEL_NAME=ps51-model", lambda r: r["json"].get("MODEL_NAME") == "ps51-model"),
            ("MAX=8192", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 8192),
            ("нет ключей с BOM/NUL", lambda r: not r["json"].get("weird_keys")),
        )),
        run("utf16_double_bom: UTF-8+BOM, пересохранённый в UTF-16", U16_DOUBLE_BOM, checks=(
            ("MODEL_NAME=twice-model", lambda r: r["json"].get("MODEL_NAME") == "twice-model"),
            ("нет ключей с BOM/NUL", lambda r: not r["json"].get("weird_keys")),
        )),
        run("utf16_double_bom БЕЗ python-dotenv", U16_DOUBLE_BOM, no_dotenv=True, checks=(
            ("MODEL_NAME=twice-model", lambda r: r["json"].get("MODEL_NAME") == "twice-model"),
            ("нет ключей с BOM/NUL", lambda r: not r["json"].get("weird_keys")),
        )),
        run("utf8_bom: UTF-8 с BOM (Блокнот «Сохранить как… UTF-8»)",
            b"\xef\xbb\xbfMODEL_NAME=bom-model\n", checks=(
            ("MODEL_NAME=bom-model", lambda r: r["json"].get("MODEL_NAME") == "bom-model"),
            ("нет ключей с BOM/NUL", lambda r: not r["json"].get("weird_keys")),
        )),
        run("cp1251: .env в CP1251 -> предупреждение + дефолты",
            "MODEL_NAME=модель\n# комментарий по-русски\n".encode("cp1251"), checks=(
            ("MODEL_NAME=qwen-local (дефолт)", lambda r: r["json"].get("MODEL_NAME") == "qwen-local"),
            ("нет мусорных ключей", lambda r: not r["json"].get("weird_keys")),
        )),
        run("utf16_no_bom: UTF-16LE без BOM -> отказ, а не мусор",
            "MODEL_NAME=nul-model\n".encode("utf-16-le"), checks=(
            ("MODEL_NAME=qwen-local (дефолт)", lambda r: r["json"].get("MODEL_NAME") == "qwen-local"),
        )),
        run("utf16le_bom + python-dotenv (установлен)", U16, checks=(
            ("MODEL_NAME=ps51-model", lambda r: r["json"].get("MODEL_NAME") == "ps51-model"),
        )),
        run("utf16le_bom БЕЗ python-dotenv", U16, no_dotenv=True, checks=(
            ("MODEL_NAME=ps51-model", lambda r: r["json"].get("MODEL_NAME") == "ps51-model"),
            ("MAX=8192", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 8192),
            ("нет ключей с BOM/NUL", lambda r: not r["json"].get("weird_keys")),
        )),
        run("utf8_bom БЕЗ python-dotenv", b"\xef\xbb\xbfMODEL_NAME=bom-model\n", no_dotenv=True, checks=(
            ("MODEL_NAME=bom-model", lambda r: r["json"].get("MODEL_NAME") == "bom-model"),
        )),
        run("cp1251 БЕЗ python-dotenv", "MODEL_NAME=модель\n".encode("cp1251"), no_dotenv=True, checks=(
            ("MODEL_NAME=qwen-local (дефолт)", lambda r: r["json"].get("MODEL_NAME") == "qwen-local"),
        )),
        run("limit_zero: HANDOFF_TOKEN_LIMIT=0 -> диапазон 1..MAX",
            b"MAX_CONTEXT_TOKENS=8192\nHANDOFF_TOKEN_LIMIT=0\n", checks=(
            ("1 <= limit <= 8192", lambda r: 1 <= (limit_of(r) or 0) <= 8192),
        )),
        run("limit_huge: HANDOFF_TOKEN_LIMIT=999999 -> диапазон",
            b"MAX_CONTEXT_TOKENS=8192\nHANDOFF_TOKEN_LIMIT=999999\n", checks=(
            ("1 <= limit <= 8192", lambda r: 1 <= (limit_of(r) or 0) <= 8192),
        )),
        run("limit_negative: HANDOFF_TOKEN_LIMIT=-5 -> диапазон",
            b"MAX_CONTEXT_TOKENS=8192\nHANDOFF_TOKEN_LIMIT=-5\n", checks=(
            ("1 <= limit <= 8192", lambda r: 1 <= (limit_of(r) or 0) <= 8192),
        )),
        run("ctx_zero: MAX_CONTEXT_TOKENS=0 (регрессия деления на ноль)",
            b"MAX_CONTEXT_TOKENS=0\n", checks=(
            ("MAX=32768", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 32768),
            ("limit=24576", lambda r: limit_of(r) == 24576),
        )),
        run("ctx_garbage: MAX_CONTEXT_TOKENS=abc", b"MAX_CONTEXT_TOKENS=abc\n", checks=(
            ("MAX=32768", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 32768),
        )),
        run("ratio_bad: HANDOFF_FREE_RATIO=1.5", b"HANDOFF_FREE_RATIO=1.5\n", checks=(
            ("ratio=0.25", lambda r: r["json"].get("HANDOFF_FREE_RATIO") == 0.25),
        )),
        run("paths_cwd_outside: запуск из другого cwd", b"MODEL_NAME=other-cwd-model\n",
            cwd_outside=True, checks=(
            ("MODEL_NAME=other-cwd-model (.env найден у модуля)",
             lambda r: r["json"].get("MODEL_NAME") == "other-cwd-model"),
            ("HANDOFF_FILE у модуля, а не в cwd",
             lambda r: r["json"].get("HANDOFF_FILE", "").lower().startswith(r["module_dir"].lower())),
            ("HANDOFF_BACKUP у модуля",
             lambda r: r["json"].get("HANDOFF_BACKUP", "").lower().startswith(r["module_dir"].lower())),
        )),
        run("handoff_env_relative: HANDOFF_FILE=state/handoff.md из .env",
            b"HANDOFF_FILE=state/handoff.md\nHANDOFF_BACKUP=state/handoff.bak\n", checks=(
            ("HANDOFF_FILE под каталогом модуля",
             lambda r: slash(r["json"].get("HANDOFF_FILE", "")).startswith(slash(r["module_dir"]))
             and slash(r["json"].get("HANDOFF_FILE", "")).endswith("/state/handoff.md")),
            ("HANDOFF_BACKUP под каталогом модуля",
             lambda r: slash(r["json"].get("HANDOFF_BACKUP", "")).endswith("/state/handoff.bak")),
        )),
        run("handoff_env_absolute: абсолютный HANDOFF_FILE из .env не переписывается",
            b"HANDOFF_FILE=F:/tmp/state/handoff.md\n", checks=(
            ("HANDOFF_FILE=F:/tmp/state/handoff.md",
             lambda r: slash(r["json"].get("HANDOFF_FILE", "")) == "f:/tmp/state/handoff.md"),
        )),
        run("stderr_cp437: предупреждение в перенаправленный stderr (cp437)",
            b"MAX_CONTEXT_TOKENS=abc\n", io_encoding="cp437", checks=(
            ("MAX=32768", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 32768),
        )),
        run("stderr_cp1251_cyr: кириллица в предупреждении, cp1251",
            "MAX_CONTEXT_TOKENS=абв\n".encode("cp1251"), io_encoding="cp1251", checks=(
            ("MAX=32768", lambda r: r["json"].get("MAX_CONTEXT_TOKENS") == 32768),
        )),
        run("env_wins: переменная окружения важнее .env", b"MODEL_NAME=from-file\n",
            extra_env={"MODEL_NAME": "from-env"}, checks=(
            ("MODEL_NAME=from-env", lambda r: r["json"].get("MODEL_NAME") == "from-env"),
        )),
        run("export_и_кавычки: export KEY=\"value\" # comment",
            'export MODEL_NAME="exp-model" # пояснение\n'.encode("utf-8"), checks=(
            ("MODEL_NAME=exp-model", lambda r: r["json"].get("MODEL_NAME") == "exp-model"),
        )),
        run("divisor: TOKEN_ESTIMATE_DIVISOR=2 (кириллица)", b"TOKEN_ESTIMATE_DIVISOR=2\n", checks=(
            ("оценка 600 симв. -> 300", lambda r: r["json"].get("fallback_600") == 300),
        )),
        run("divisor_bad: TOKEN_ESTIMATE_DIVISOR=0 -> дефолт 3", b"TOKEN_ESTIMATE_DIVISOR=0\n", checks=(
            ("оценка 600 симв. -> 200", lambda r: r["json"].get("fallback_600") == 200),
        )),
    ]

    print(f"=== {LABEL}: {SRC} ===")
    failed = 0
    for res in scenarios:
        status = "PASS" if not res["problems"] else "FAIL"
        if res["problems"]:
            failed += 1
        first_err = res["stderr"].strip().splitlines()[0][:120] if res["stderr"].strip() else ""
        print(f"[{status}] {res['name']}")
        if res["problems"]:
            print(f"        проблемы: {'; '.join(res['problems'])}")
            print(f"        stderr: {first_err or '(пусто)'}")
    print(f"--- всего сценариев: {len(scenarios)}, с проблемами: {failed} ---")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
