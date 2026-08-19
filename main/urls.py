from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("favicon.ico", views.favicon_ico, name="favicon_ico"),
    path("favicon.svg", views.favicon_svg, name="favicon_svg"),
    path("favicon-120.png", views.favicon_png, name="favicon_png"),
    path("", views.index, name="home"),
    path("for-flowers/", views.ForFlowersRedirect.as_view(), name="for_flowers"),
    path(
        "services/site-for-flower-store/",
        RedirectView.as_view(url="/services/site-for-flower-shop/", permanent=True),
        name="flower_store_redirect",
    ),
    path("services/<slug>/", views.service_landing, name="service_landing"),
    path("blog/", views.blog_index, name="blog_index"),
    path("blog/<slug>/", views.blog_article, name="blog_article"),
]
