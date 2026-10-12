#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
preview_python="${PREVIEW_PYTHON:-$project_root/.venv-preview/bin/python}"
if [ ! -x "$preview_python" ] && [ -x "$project_root/../.venv-preview/bin/python" ]; then
  preview_python="$project_root/../.venv-preview/bin/python"
fi
if [ ! -x "$preview_python" ]; then
  echo 'Create .venv-preview with Python 3.12+ and install requirements.txt first.' >&2
  exit 1
fi
cd "$project_root"
export MPLCONFIGDIR="${MPLCONFIGDIR:-${TMPDIR:-/tmp}/maksonchik-matplotlib}"
"$preview_python" manage.py migrate --settings=maksonchik.settings_preview --noinput
exec "$preview_python" manage.py runserver 127.0.0.1:8000 --settings=maksonchik.settings_preview --noreload
