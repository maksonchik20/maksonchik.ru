WHO_UPDATE_HOSTS = frozenset({"who-update.ru", "www.who-update.ru"})


class HostURLConfMiddleware:
    """Use a narrow URL surface for the standalone WhoUpdate domain."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":", 1)[0].lower()
        if host in WHO_UPDATE_HOSTS:
            request.urlconf = "maksonchik.who_update_urls"
        return self.get_response(request)
