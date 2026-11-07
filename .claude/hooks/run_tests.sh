#!/usr/bin/env bash
# Runs the offline tests after a Python file changes, and reports failures back to Claude.
# The API key is removed so that a test can never spend money. Set CLAIMS_PYTHON to use a virtualenv outside .venv.
file=$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))')
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac

cd "$CLAUDE_PROJECT_DIR" || exit 0
python_bin="${CLAIMS_PYTHON:-.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
  echo "Tests not run: no Python at $python_bin. Create .venv or set CLAIMS_PYTHON." >&2
  exit 0
fi

if ! output=$(env -u ANTHROPIC_API_KEY PYTHONDONTWRITEBYTECODE=1 "$python_bin" -m pytest -q --no-header -p no:cacheprovider --tb=short 2>&1); then
  printf 'Offline tests failed after editing %s:\n%s\n' "$file" "$(printf '%s\n' "$output" | tail -40)" >&2
  exit 2
fi
