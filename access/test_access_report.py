import shutil
import unittest
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from . import pdf_export
from .models import Group, Lock, Transponder


class AccessReportDataTests(TestCase):
    def setUp(self):
        self.door_a = Lock.objects.create(
            serial="DOOR-A", door_name="Allgemeiner Eingang", location="MUC.A.EG"
        )
        self.door_l = Lock.objects.create(
            serial="DOOR-L", door_name="Lagertür", location="MUC.L.UG"
        )
        self.shared = Lock.objects.create(
            serial="DOOR-S", door_name="Gemeinsame Tür", location="MUC.S.EG"
        )
        self.individual = Lock.objects.create(
            serial="INDIVIDUAL", door_name="Einzeltür", location="MUC.I.OG"
        )
        self.base = Group.objects.create(
            name="AStA Allgemein", export_code="A", is_implicit=True
        )
        self.store = Group.objects.create(name="AStA Lager", export_code="L")
        self.base.doors.set([self.door_a, self.shared])
        self.store.doors.set([self.door_l, self.shared])

        self.base_tp = Transponder.objects.create(serial="000BASE", person_name="Basis")
        self.base_tp.groups.add(self.base)
        self.base_tp.desired_locks.set([self.door_a, self.shared])

        self.combined_tp = Transponder.objects.create(
            serial="010A0SC", person_name="Kombiniert"
        )
        self.combined_tp.groups.set([self.base, self.store])
        self.combined_tp.desired_locks.set(
            [self.door_a, self.door_l, self.shared, self.individual]
        )

        self.ungrouped_tp = Transponder.objects.create(
            serial="0UNGR", person_name="Ohne Gruppe"
        )
        self.ungrouped_tp.desired_locks.add(self.individual)

    def test_groups_by_exact_combination_and_uses_only_group_door_union(self):
        report = pdf_export.build_access_report_data(today=date(2026, 7, 28))

        self.assertEqual(report["mode"], "access_report")
        self.assertEqual(report["generated"], "2026-07-28")
        self.assertEqual(
            [(section["key"], section["title"]) for section in report["sections"]],
            [("A", "AStA A"), ("A+L", "AStA L"), ("", "Ohne Gruppe")],
        )
        combined = report["sections"][1]
        self.assertEqual(combined["serials"], ["010A0SC"])
        self.assertEqual(
            [
                lock["serial"]
                for location in combined["locations"]
                for lock in location["locks"]
            ],
            ["DOOR-A", "DOOR-L", "DOOR-S"],
        )
        self.assertNotIn(
            "INDIVIDUAL",
            {
                lock["serial"]
                for location in combined["locations"]
                for lock in location["locks"]
            },
        )
        self.assertEqual(report["sections"][-1]["serials"], ["0UNGR"])
        self.assertEqual(report["sections"][-1]["locations"], [])

    def test_same_display_label_keeps_exact_memberships_separate(self):
        only_store = Transponder.objects.create(serial="020STORE")
        only_store.groups.add(self.store)

        report = pdf_export.build_access_report_data()
        collisions = [
            (
                section["key"],
                section["title"],
                {
                    lock["serial"]
                    for location in section["locations"]
                    for lock in location["locks"]
                },
            )
            for section in report["sections"]
            if section["base_title"] == "AStA L"
        ]

        self.assertEqual(
            collisions,
            [
                ("A+L", "AStA L (A+L)", {"DOOR-A", "DOOR-L", "DOOR-S"}),
                ("L", "AStA L (L)", {"DOOR-L", "DOOR-S"}),
            ],
        )

    def test_locations_labels_and_serials_are_deterministically_ordered(self):
        area_lock = Lock.objects.create(
            serial="AREA", door_name="alpha", room_number="101", area="area fallback"
        )
        unknown_lock = Lock.objects.create(serial="UNKNOWN", door_name="Beta")
        same_location = Lock.objects.create(
            serial="LOWER", door_name="allgemeiner eingang", location="MUC.A.EG"
        )
        self.base.doors.add(area_lock, unknown_lock, same_location)
        second = Transponder.objects.create(serial="000base-lower")
        second.groups.add(self.base)

        section = pdf_export.build_access_report_data()["sections"][0]

        self.assertEqual(
            [location["name"] for location in section["locations"]],
            ["area fallback", "MUC.A.EG", "MUC.S.EG", "Ohne Standort"],
        )
        self.assertEqual(
            [lock["label"] for lock in section["locations"][0]["locks"]],
            ["alpha (101)"],
        )
        self.assertEqual(
            [lock["serial"] for lock in section["locations"][1]["locks"]],
            ["DOOR-A", "LOWER"],
        )
        self.assertEqual(section["serials"], ["000BASE", "000base-lower"])

    def test_individual_appendix_uses_desired_minus_inherited(self):
        report = pdf_export.build_access_report_data()

        self.assertEqual(
            [(item["serial"], item["title"]) for item in report["individuals"]],
            [
                ("010A0SC", "010A0SC · Kombiniert"),
                ("0UNGR", "0UNGR · Ohne Gruppe"),
            ],
        )
        self.assertEqual(
            [
                lock["serial"]
                for location in report["individuals"][0]["locations"]
                for lock in location["locks"]
            ],
            ["INDIVIDUAL"],
        )
        self.assertEqual(
            [
                lock["serial"]
                for location in report["individuals"][1]["locations"]
                for lock in location["locks"]
            ],
            ["INDIVIDUAL"],
        )
        self.assertNotIn("000BASE", [item["serial"] for item in report["individuals"]])

    def test_query_count_does_not_grow_with_report_size(self):
        with CaptureQueriesContext(connection) as initial_queries:
            pdf_export.build_access_report_data()

        for index, code in enumerate("BCDEF"):
            lock = Lock.objects.create(serial=f"EXTRA-{index}")
            group = Group.objects.create(name=f"Extra {index}", export_code=code)
            group.doors.add(lock)
            transponder = Transponder.objects.create(serial=f"EXTRA-{index}")
            transponder.groups.add(group)
            transponder.desired_locks.add(lock)

        with CaptureQueriesContext(connection) as expanded_queries:
            pdf_export.build_access_report_data()

        self.assertEqual(len(expanded_queries), len(initial_queries))


class AccessReportMarkdownTests(TestCase):
    def report_data(self):
        return {
            "title": "Zugangsübersicht nach Exportgruppe",
            "generated": "2026-07-28",
            "mode": "access_report",
            "sections": [
                {
                    "key": "A+L",
                    "base_title": "AStA L",
                    "title": "AStA L",
                    "locations": [
                        {
                            "name": "MUC.G43.EG",
                            "locks": [
                                {"serial": "D1", "label": "Haupteingang 008 West"}
                            ],
                        }
                    ],
                    "serials": ["010A0SC"],
                    "ungrouped": False,
                }
            ],
            "individuals": [
                {
                    "serial": "02UA77F",
                    "label": "Rossmeier, Justus",
                    "title": "02UA77F · Rossmeier, Justus",
                    "locations": [
                        {
                            "name": "MUC.G43.UG",
                            "locks": [{"serial": "D2", "label": "Raum -103 Lager"}],
                        }
                    ],
                }
            ],
        }

    def test_renders_canonical_markdown(self):
        expected = """# Zugangsübersicht nach Exportgruppe

## AStA L

### Türen
- **MUC.G43.EG**
  - Haupteingang 008 West

### Transponder
- 010A0SC

# Zusätzliche individuelle Türen

## 02UA77F · Rossmeier, Justus
- **MUC.G43.UG**
  - Raum -103 Lager
"""

        self.assertEqual(
            pdf_export.render_access_report_markdown(self.report_data()), expected
        )

    def test_escapes_database_values_without_changing_structure(self):
        data = self.report_data()
        data["sections"][0]["title"] = "#Group *x* [a] \\"
        data["sections"][0]["locations"][0]["name"] = "> Basement"
        data["sections"][0]["locations"][0]["locks"][0]["label"] = "- Door_`x`"
        data["sections"][0]["serials"] = ["+SERIAL"]
        data["individuals"][0]["title"] = "=TP [name]"

        markdown = pdf_export.render_access_report_markdown(data)

        self.assertIn("## \\#Group \\*x\\* \\[a\\] \\\\", markdown)
        self.assertIn(r"- **\> Basement**", markdown)
        self.assertIn(r"  - \- Door\_\`x\`", markdown)
        self.assertIn(r"- \+SERIAL", markdown)
        self.assertIn(r"## \=TP \[name\]", markdown)

    def test_renders_group_and_report_empty_states(self):
        data = self.report_data()
        data["sections"][0]["locations"] = []
        data["individuals"] = []

        markdown = pdf_export.render_access_report_markdown(data)

        self.assertIn("_Keine Gruppentüren._", markdown)
        self.assertIn("_Keine zusätzlichen individuellen Türen._", markdown)
        self.assertTrue(markdown.endswith("\n"))
        self.assertFalse(markdown.endswith("\n\n"))

        data["sections"] = []
        self.assertIn(
            "_Keine Exportgruppen._",
            pdf_export.render_access_report_markdown(data),
        )


HAVE_TYPST = shutil.which("typst") is not None


class AccessReportPdfTests(TestCase):
    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_access_report_wrapper_renders_selectable_text_pdf(self):
        lock = Lock.objects.create(
            serial="PDF-LOCK", door_name="Tür mit Umlaut", location="MUC.PDF.EG"
        )
        group = Group.objects.create(name="AStA PDF", export_code="P")
        group.doors.add(lock)
        transponder = Transponder.objects.create(serial="010PDF", person_name="PDF")
        transponder.groups.add(group)
        transponder.desired_locks.add(lock)

        pdf = pdf_export.export_access_report_pdf(today=date(2026, 7, 28))

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_empty_access_report_renders_pdf(self):
        data = pdf_export.build_access_report_data(today=date(2026, 7, 28))

        pdf = pdf_export.render_pdf(data, "a4")

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)


class AccessReportViewTests(TestCase):
    def test_report_page_exposes_markdown_and_actions(self):
        response = self.client.get("/groups/access-report/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "access/access_report.html")
        self.assertContains(response, "# Zugangsübersicht nach Exportgruppe")
        self.assertContains(response, "Markdown kopieren")
        self.assertContains(response, "PDF herunterladen")
        self.assertContains(response, "readonly")

    def test_group_list_links_to_access_report(self):
        response = self.client.get("/groups/")

        self.assertContains(response, 'href="/groups/access-report/"', count=1)
        self.assertContains(
            response,
            '<a href="/groups/access-report/" class="btn-ghost">Zugangsübersicht</a>',
            html=True,
        )

    def test_pdf_endpoint_uses_safe_server_filename(self):
        with mock.patch.object(
            pdf_export, "export_access_report_pdf", return_value=b"%PDF report"
        ):
            response = self.client.get("/groups/access-report.pdf?filename=bad.pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertRegex(
            response["Content-Disposition"],
            r'^attachment; filename="zugangsuebersicht-\d{4}-\d{2}-\d{2}\.pdf"$',
        )
        self.assertNotIn("bad", response["Content-Disposition"])
        self.assertEqual(response.content, b"%PDF report")

    def test_pdf_failure_redirects_with_safe_message(self):
        with (
            mock.patch.object(
                pdf_export,
                "export_access_report_pdf",
                side_effect=RuntimeError("private /tmp/path"),
            ),
            self.assertLogs("access.views", level="ERROR") as logs,
        ):
            response = self.client.get("/groups/access-report.pdf", follow=True)

        self.assertRedirects(response, "/groups/access-report/")
        self.assertContains(
            response, "PDF-Export fehlgeschlagen. Bitte Typst-Installation prüfen."
        )
        self.assertNotContains(response, "private /tmp/path")
        self.assertIn("Access report PDF export failed", logs.output[0])

    def test_copy_action_has_success_and_manual_fallback_states(self):
        response = self.client.get("/groups/access-report/")

        self.assertContains(response, "navigator.clipboard.writeText")
        self.assertContains(response, "$refs.markdown.focus()")
        self.assertContains(response, "$refs.markdown.select()")
        self.assertContains(response, "Markdown kopiert.")
        self.assertContains(response, "bitte manuell kopieren")
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'x-ref="markdown"')
        self.assertContains(response, "flex-col")
        self.assertContains(response, "sm:flex-row")


GATE_MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
]


@override_settings(MIDDLEWARE=GATE_MIDDLEWARE)
class AccessReportAuthTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "report-user", password="safe-password"
        )

    def test_anonymous_user_is_redirected_from_report_routes(self):
        for url in ("/groups/access-report/", "/groups/access-report.pdf"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_normal_authenticated_user_can_view_and_download(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/groups/access-report/").status_code, 200)
        with mock.patch.object(
            pdf_export, "export_access_report_pdf", return_value=b"%PDF report"
        ):
            response = self.client.get("/groups/access-report.pdf")
        self.assertEqual(response.status_code, 200)
