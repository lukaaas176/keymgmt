from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path

urlpatterns = [
    # Login is the one page reachable without a session (LoginRequiredMiddleware
    # gates everything else). Logout is POST-only and needs a session.
    path(
        "accounts/login/",
        login_not_required(auth_views.LoginView.as_view()),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("access.urls")),
]
