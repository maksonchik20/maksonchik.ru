import json
from pathlib import Path
from django.conf import settings


def config():
    override = getattr(settings, "OWNER_PANEL_CONFIG", None)
    if override is not None:
        return override
    path = Path("/srv/maksonchik/owner-panel.json")
    if not path.is_file():
        return {}
    return json.loads(path.read_text())
