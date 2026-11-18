"""Показывает полные трейсбеки текущего team_config.py на битых .env."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "team_config.py").resolve()

CASES = {
    "UTF-16LE (PowerShell 5.1 `> .env`)": ("\ufeffMODEL_NAME=ps51-model\n").encode("utf-16"),
    "CP1251 (русская локаль)": "MODEL_NAME=модель\n".encode("cp1251"),
    "UTF-16LE без BOM": "MODEL_NAME=nul-model\n".encode("utf-16-le"),
}

for label, blob in CASES.items():
    tmp = Path(tempfile.mkdtemp(prefix="tc_tb_"))
    shutil.copy(SRC, tmp / "team_config.py")
    (tmp / ".env").write_bytes(blob)
    env = dict(os.environ)
    for var in ("MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY", "MAX_CONTEXT_TOKENS",
                "HANDOFF_TOKEN_LIMIT", "HANDOFF_FREE_RATIO", "MAX_STEP_ATTEMPTS",
                "HANDOFF_FILE", "HANDOFF_BACKUP", "PYTHONIOENCODING"):
        env.pop(var, None)
    proc = subprocess.run([sys.executable, "-B", "-c", "import team_config"],
                          cwd=tmp, env=env, capture_output=True)
    print("=" * 70)
    print(f"СЦЕНАРИЙ: {label}   (import team_config, rc={proc.returncode})")
    print("-" * 70)
    tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-4:]
    print("\n".join(tail) or "(stderr пуст)")
    shutil.rmtree(tmp, ignore_errors=True)
