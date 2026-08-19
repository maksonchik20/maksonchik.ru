from .site_info import SCHEMA, SITE


def site(request):
    return {"site": SITE, "schema": SCHEMA}
