#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
preview_python=${WHOUPDATE_PREVIEW_PYTHON:-"$project_root/.venv-preview/bin/python"}
if [ ! -x "$preview_python" ] && [ -x "$project_root/../.venv-preview/bin/python" ]; then
  preview_python="$project_root/../.venv-preview/bin/python"
fi
if [ ! -x "$preview_python" ]; then
  echo 'Create .venv-preview, install requirements.txt, or set WHOUPDATE_PREVIEW_PYTHON.' >&2
  exit 1
fi
cd "$project_root"
exec "$preview_python" manage.py runserver 127.0.0.1:8001 --settings=maksonchik.settings_whoupdate_preview --noreload
