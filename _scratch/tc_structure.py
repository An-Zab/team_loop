"""Структурная проверка team_config.py перед коммитом."""
import ast
import collections
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "team_config.py").resolve()
src = path.read_text(encoding="utf-8")
tree = ast.parse(src)

print("--- верхний уровень по порядку ---")
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        print(f"{node.lineno:4} {type(node).__name__:12} {node.name}")
    elif isinstance(node, ast.Assign):
        names = ",".join(getattr(t, "id", "?") for t in node.targets)
        print(f"{node.lineno:4} {'Assign':12} {names}")
    elif isinstance(node, ast.AnnAssign):
        print(f"{node.lineno:4} {'AnnAssign':12} {getattr(node.target, 'id', '?')}")
    elif isinstance(node, ast.If):
        print(f"{node.lineno:4} {'If':12} {ast.unparse(node.test)[:60]}")
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        print(f"{node.lineno:4} {'Import':12} {ast.unparse(node)[:60]}")

print("--- дубликаты функций ---")
counter = collections.Counter(n.name for n in tree.body if isinstance(n, ast.FunctionDef))
print([k for k, v in counter.items() if v > 1] or "нет")

print("--- устаревшие ссылки / способ вывода ---")
for token in ("_strip_quotes", "_parse_env_file", "print(", "sys.stderr", "sys.stdout", "_warn("):
    print(f"  {token:16} {src.count(token)}")

print("--- все имена верхнего уровня, используемые до определения? (проверка компилятором) ---")
compile(src, str(path), "exec")
print("  compile(): OK")
