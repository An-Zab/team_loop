"""Точечная отладка двух сценариев: UTF-16 без dotenv и относительный HANDOFF_FILE."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "team_config.py").resolve()

DRIVER = '''
import json, os, sys
if os.environ.get("TC_NO_DOTENV") == "1":
    sys.modules["dotenv"] = None
import team_config as tc
print("JSON:" + json.dumps({
    "MODEL_NAME": tc.MODEL_NAME,
    "HANDOFF_FILE": str(tc.HANDOFF_FILE),
    "HANDOFF_BACKUP": str(tc.HANDOFF_BACKUP),
    "module_dir": str(tc._MODULE_DIR),
    "env_HAS_handoff": "HANDOFF_FILE" in os.environ,
    "env_HANDOFF_FILE": os.environ.get("HANDOFF_FILE"),
    "dotenv_in_modules": sys.modules.get("dotenv", "absent") == None,
}, ensure_ascii=False))
'''


def go(label, blob, no_dotenv=False):
    tmp = Path(tempfile.mkdtemp(prefix="tc_dbg_"))
    shutil.copy(SRC, tmp / "team_config.py")
    (tmp / "driver.py").write_text(DRIVER, encoding="utf-8")
    (tmp / ".env").write_bytes(blob)
    env = dict(os.environ)
    for var in ("MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY", "MAX_CONTEXT_TOKENS",
                "HANDOFF_TOKEN_LIMIT", "HANDOFF_FREE_RATIO", "MAX_STEP_ATTEMPTS",
                "HANDOFF_FILE", "HANDOFF_BACKUP", "TOKEN_ESTIMATE_DIVISOR", "PYTHONIOENCODING"):
        env.pop(var, None)
    if no_dotenv:
        env["TC_NO_DOTENV"] = "1"
    proc = subprocess.run([sys.executable, "-B", "driver.py"], cwd=tmp, env=env,
                          capture_output=True)
    print("=" * 72)
    print(f"{label}  rc={proc.returncode}  tmp={tmp}")
    print("STDOUT:", proc.stdout.decode("utf-8", "replace").strip())
    print("STDERR:", proc.stderr.decode("utf-8", "replace").strip() or "(пусто)")
    shutil.rmtree(tmp, ignore_errors=True)


go("UTF-16 + dotenv", ("\ufeffMODEL_NAME=ps51-model\n").encode("utf-16"))
go("UTF-16 БЕЗ dotenv", ("\ufeffMODEL_NAME=ps51-model\n").encode("utf-16"), no_dotenv=True)
go("HANDOFF_FILE=state/handoff.md", b"HANDOFF_FILE=state/handoff.md\nHANDOFF_BACKUP=state/handoff.bak\n")
