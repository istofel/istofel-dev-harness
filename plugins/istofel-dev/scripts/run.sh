#!/bin/sh
# Lançador portátil: encontra Python 3.8+ (python3, python ou py) no Linux, macOS e Windows (Git Bash).
DIR=$(cd "$(dirname "$0")" && pwd)
for p in python3 python py; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    exec "$p" "$DIR/harness_core.py" "$@"
  fi
done
case "$1" in hook-*) echo '{"systemMessage": "istofel-harness: Python 3.8+ nao encontrado; hooks inativos."}' ;; *) echo "Python 3.8+ nao encontrado." >&2; exit 1 ;; esac
exit 0
