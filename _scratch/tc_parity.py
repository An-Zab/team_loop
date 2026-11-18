"""Паритет встроенного парсера и python-dotenv + устойчивость _warn к битому stderr."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "team_config.py").resolve()

PARITY = r'''
import io, json, os, sys
from pathlib import Path
import team_config as tc

TRICKY = (
    'OPENAI_BASE_URL=http://localhost:1234/v1\n'
    '# комментарий\n'
    '\n'
    'MODEL_NAME="Кавычки Модель"\n'
    "OPENAI_API_KEY='EMPTY'\n"
    'export MAX_CONTEXT_TOKENS=8192   # с комментарием после значения\n'
    'HANDOFF_FREE_RATIO = 0.3 \n'
    'EMPTY_VALUE=\n'
    'без_равно\n'
    '#MAX_STEP_ATTEMPTS=99\n'
    'HANDOFF_FILE=state/handoff.md\n'
    'HASH_IN_QUOTES="pass #word"\n'
    'HASH_BARE=pass #word\n'
    'HASH_TOUCHING=pass#word\n'
    'TRAILING_SPACES=value   \n'
    'URL=http://host:1234/v1#frag\n'
)

# Встроенный парсер.
own = tc._parse_env_text(tc._decode_env_text(TRICKY.encode("utf-8"), Path("x.env")))

# python-dotenv на том же тексте, в чистом окружении (если установлен).
try:
    from dotenv import load_dotenv
except ImportError:
    print("PARITY:" + json.dumps({"own": own, "via_dotenv": None, "equal": True,
                                  "note": "python-dotenv не установлен, пропуск сравнения"},
                                 ensure_ascii=False))
    sys.exit(0)
saved = dict(os.environ)
for key in own:
    os.environ.pop(key, None)
load_dotenv(stream=io.StringIO(TRICKY), override=False, interpolate=False)
via_dotenv = {k: os.environ.get(k) for k in own}
os.environ.clear()
os.environ.update(saved)

print("PARITY:" + json.dumps({"own": own, "via_dotenv": via_dotenv,
                              "equal": own == via_dotenv}, ensure_ascii=False))
'''

WARN_ROBUSTNESS = r'''
import io
import sys
import team_config as tc


class Broken:
    """stderr, который не умеет UTF-8 и падает даже на ASCII-повторе."""

    encoding = "cp437"

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        raise UnicodeEncodeError("cp437", data, 0, 1, "forced")

    def flush(self):
        pass


for stream in (Broken(), io.StringIO()):
    sys.stderr = stream
    tc._warn("Проверка кириллицы в предупреждении")
sys.stderr = sys.__stderr__
print("WARN_OK: _warn не бросил исключение на битом и на заглушённом stderr")
'''


def go(label, driver, env_bytes=None):
    tmp = Path(tempfile.mkdtemp(prefix="tc_par_"))
    shutil.copy(SRC, tmp / "team_config.py")
    (tmp / "driver.py").write_text(driver, encoding="utf-8")
    if env_bytes is not None:
        (tmp / ".env").write_bytes(env_bytes)
    env = dict(os.environ)
    for var in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_NAME", "MAX_CONTEXT_TOKENS",
                "HANDOFF_TOKEN_LIMIT", "HANDOFF_FREE_RATIO", "MAX_STEP_ATTEMPTS",
                "HANDOFF_FILE", "HANDOFF_BACKUP", "TOKEN_ESTIMATE_DIVISOR",
                "PYTHONIOENCODING"):
        env.pop(var, None)
    proc = subprocess.run([sys.executable, "-B", "driver.py"], cwd=tmp, env=env,
                          capture_output=True)
    print("=" * 72)
    print(f"{label}  rc={proc.returncode}")
    print(proc.stdout.decode("utf-8", "replace").strip() or "(stdout пуст)")
    err = proc.stderr.decode("utf-8", "replace").strip()
    if err:
        print("STDERR:", err)
    shutil.rmtree(tmp, ignore_errors=True)


go("Паритет встроенного парсера и python-dotenv", PARITY)
go("Устойчивость _warn (sys.stderr битый/заглушённый)", WARN_ROBUSTNESS)
