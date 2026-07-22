from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload, name="upload"),
    path("transponders/", views.transponder_list, name="transponder_list"),
    path("transponders/<str:serial>/", views.transponder_detail, name="transponder_detail"),
    path("locks/", views.lock_list, name="lock_list"),
    path("locks/<str:serial>/", views.lock_detail, name="lock_detail"),
    path("overlap/", views.overlap, name="overlap"),
    path("individual/", views.individual_access, name="individual_access"),
    path("export.pdf", views.export_pdf, name="export_pdf"),

    # Soll editing
    path("soll/", views.soll_matrix, name="soll_matrix"),
    path("soll/toggle/", views.soll_toggle, name="soll_toggle"),
    path("soll/group-assign/", views.soll_group_assign, name="soll_group_assign"),
    path("transponders/<str:serial>/soll/", views.transponder_soll_action,
         name="transponder_soll_action"),
    path("groups/", views.group_list, name="group_list"),
    path("groups/create/", views.group_create, name="group_create"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    path("groups/<int:pk>/rename/", views.group_rename, name="group_rename"),
    path("groups/<int:pk>/delete/", views.group_delete, name="group_delete"),
]
