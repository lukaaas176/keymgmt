from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload, name="upload"),
    path("data/", views.data_page, name="data_page"),
    path("data/export/", views.data_export, name="data_export"),
    path("data/import/", views.data_import, name="data_import"),
    path("transponders/", views.transponder_list, name="transponder_list"),
    path("transponders/new/", views.transponder_create, name="transponder_create"),
    path(
        "transponders/<str:serial>/",
        views.transponder_detail,
        name="transponder_detail",
    ),
    path(
        "transponders/<str:serial>/edit/",
        views.transponder_edit,
        name="transponder_edit",
    ),
    path(
        "transponders/<str:serial>/delete/",
        views.transponder_delete,
        name="transponder_delete",
    ),
    path("locks/", views.lock_list, name="lock_list"),
    path("locks/new/", views.lock_create, name="lock_create"),
    path("locks/<str:serial>/", views.lock_detail, name="lock_detail"),
    path("locks/<str:serial>/edit/", views.lock_edit, name="lock_edit"),
    path("locks/<str:serial>/delete/", views.lock_delete, name="lock_delete"),
    path("overlap/", views.overlap, name="overlap"),
    path("individual/", views.individual_access, name="individual_access"),
    path("export.pdf", views.export_pdf, name="export_pdf"),
    # Soll editing
    path("soll/", views.soll_matrix, name="soll_matrix"),
    path("soll/toggle/", views.soll_toggle, name="soll_toggle"),
    path("soll/group-assign/", views.soll_group_assign, name="soll_group_assign"),
    path(
        "transponders/<str:serial>/soll/",
        views.transponder_soll_action,
        name="transponder_soll_action",
    ),
    path("groups/", views.group_list, name="group_list"),
    path("groups/create/", views.group_create, name="group_create"),
    path("groups/access-report/", views.access_report, name="access_report"),
    path(
        "groups/access-report.pdf",
        views.access_report_pdf,
        name="access_report_pdf",
    ),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    path("groups/<int:pk>/rename/", views.group_rename, name="group_rename"),
    path("groups/<int:pk>/metadata/", views.group_metadata, name="group_metadata"),
    path("groups/<int:pk>/delete/", views.group_delete, name="group_delete"),
]
