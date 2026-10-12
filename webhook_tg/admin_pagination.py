"""Count-free primary-key navigation for append-heavy admin journals."""

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.options import IncorrectLookupParameters
from django.contrib.admin.views.main import ChangeList

BEFORE_PARAM = "before_id"
AFTER_PARAM = "after_id"
CURSOR_PARAMS = (BEFORE_PARAM, AFTER_PARAM)


class KeysetChangeList(ChangeList):
    def get_filters_params(self, params=None):
        paramToValue = super().get_filters_params(params)
        for name in CURSOR_PARAMS:
            paramToValue.pop(name, None)
        return paramToValue

    def get_ordering(self, request, queryset):
        # A stable indexed order; arbitrary column sorts need their own cursor
        # and index and must not silently fall back to scanning a huge journal.
        return ["-pk"]

    def get_query_string(self, new_params=None, remove=None):
        paramToValue = new_params or {}
        remove_params = list(remove or []) + ["p", "all", "o"]
        # Filter/search links start at the newest matching page. Only explicit
        # navigation links carry a cursor into the next request.
        remove_params.extend(CURSOR_PARAMS)
        return super().get_query_string(paramToValue, remove_params)

    @staticmethod
    def _cursor(request, name):
        raw = request.GET.get(name)
        if raw in (None, ""):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise IncorrectLookupParameters("Invalid page cursor")
        if not 0 < value <= 9223372036854775807:
            raise IncorrectLookupParameters("Invalid page cursor")
        return value

    def get_results(self, request):
        before = self._cursor(request, BEFORE_PARAM)
        after = self._cursor(request, AFTER_PARAM)
        if before is not None and after is not None:
            raise IncorrectLookupParameters("Use only one page cursor")
        self.cursor_first_url = self.get_query_string()
        self.cursor_older_url = None
        self.cursor_newer_url = None
        self.cursor_is_first = before is None and after is None
        self.search_hidden_fields = [
            (name, value)
            for name, values in request.GET.lists()
            if name not in (*CURSOR_PARAMS, "q", "p", "all", "o")
            for value in values
        ]

        if self.model_admin.skip_journal_results(request):
            # AJAX chat has already fetched its own bounded message page.
            rows = []
            has_more = False
        else:
            queryset = self.queryset
            if before is not None:
                queryset = queryset.filter(pk__lt=before)
            elif after is not None:
                queryset = queryset.filter(pk__gt=after).order_by("pk")
            rows = list(queryset[:self.list_per_page + 1])
            has_more = len(rows) > self.list_per_page
            rows = rows[:self.list_per_page]
            if after is not None:
                rows.reverse()
            if rows:
                if (after is not None and has_more) or before is not None:
                    self.cursor_newer_url = self.get_query_string({AFTER_PARAM: rows[0].pk})
                if (after is None and has_more) or after is not None:
                    self.cursor_older_url = self.get_query_string({BEFORE_PARAM: rows[-1].pk})

        # Django uses this for the visible-page action checkbox counter. The
        # custom search/pagination templates never label it as a global total.
        self.result_count = len(rows)
        self.full_result_count = None
        self.show_full_result_count = False
        self.show_admin_actions = bool(rows)
        self.result_list = rows
        self.can_show_all = False
        self.show_all = False
        self.multi_page = bool(self.cursor_older_url or self.cursor_newer_url)
        self.paginator = None


class KeysetAdminMixin:
    change_list_template = "admin/webhook_tg/keyset_change_list.html"
    show_full_result_count = False
    sortable_by = ()
    # Facet counters are exact COUNTs as well; disable their opt-in query flag.
    show_facets = admin.ShowFacets.NEVER

    def get_changelist(self, request, **optionToValue):
        return KeysetChangeList

    def skip_journal_results(self, request):
        return False

    def response_action(self, request, queryset, *args, **optionToValue):
        if forms.BooleanField(required=False).clean(request.POST.get("select_across")):
            self.message_user(
                request,
                "Выберите конкретные записи на странице. Действия сразу над всем журналом отключены.",
                messages.WARNING,
            )
            return None
        return super().response_action(request, queryset, *args, **optionToValue)
