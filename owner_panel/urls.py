from django.urls import path
from . import views

app_name = "owner_panel"
urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="sw"),
    path("api/<str:project>/<str:section>/", views.data, name="data"),
    path("api/create-token/", views.create_token, name="create_token"),
    path("api/update-token/", views.update_token, name="update_token"),
    path("api/ghost-payout/", views.ghost_payout, name="ghost_payout"),
    path("api/push/", views.push_subscription, name="push"),
    path("api/push-test/", views.push_test, name="push_test"),
]
