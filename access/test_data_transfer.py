import json
from copy import deepcopy
from datetime import UTC, date, datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.datastructures import MultiValueDict

from . import data_transfer, views
from .models import Group, Lock, Transponder


class DataTransferExportTests(TestCase):
    def setUp(self):
        self.lock_a = Lock.objects.create(
            serial="DC-1",
            door_name="Eingang",
            room_number="0.01",
            location="MUC.1.EG",
            area="Allgemein",
        )
        self.lock_b = Lock.objects.create(serial="DC-2", door_name="Lager")
        self.base = Group.objects.create(
            name="AStA Allgemein", export_code="A", is_implicit=True
        )
        self.store = Group.objects.create(name="AStA Lager", export_code="L")
        self.base.doors.add(self.lock_a)
        self.store.doors.add(self.lock_b)
        self.tp = Transponder.objects.create(
            serial="T-1",
            asta_number=7,
            person_name="Muster, Mia",
            locking_system="Anlage 1",
            printed_on=date(2026, 7, 20),
            source_file="matrix.pdf",
        )
        self.tp.locks.add(self.lock_a)
        self.tp.planned_locks.add(self.lock_b)
        self.tp.removed_locks.add(self.lock_b)
        self.tp.desired_locks.add(self.lock_a, self.lock_b)
        self.tp.groups.add(self.base, self.store)

    def test_build_backup_contains_all_domain_fields_and_relationships(self):
        exported_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

        backup = data_transfer.build_backup(exported_at=exported_at)

        self.assertEqual(backup["format"], "schliessmatrix-backup")
        self.assertEqual(backup["version"], 1)
        self.assertEqual(backup["exported_at"], "2026-07-28T12:00:00Z")
        self.assertEqual(
            backup["locks"],
            [
                {
                    "serial": "DC-1",
                    "door_name": "Eingang",
                    "room_number": "0.01",
                    "location": "MUC.1.EG",
                    "area": "Allgemein",
                },
                {
                    "serial": "DC-2",
                    "door_name": "Lager",
                    "room_number": "",
                    "location": "",
                    "area": "",
                },
            ],
        )
        self.assertEqual(
            backup["groups"],
            [
                {
                    "name": "AStA Allgemein",
                    "export_code": "A",
                    "is_implicit": True,
                    "doors": ["DC-1"],
                },
                {
                    "name": "AStA Lager",
                    "export_code": "L",
                    "is_implicit": False,
                    "doors": ["DC-2"],
                },
            ],
        )
        row = backup["transponders"][0]
        self.assertEqual(
            {
                key: row[key]
                for key in (
                    "serial",
                    "asta_number",
                    "person_name",
                    "locking_system",
                    "printed_on",
                    "source_file",
                )
            },
            {
                "serial": "T-1",
                "asta_number": 7,
                "person_name": "Muster, Mia",
                "locking_system": "Anlage 1",
                "printed_on": "2026-07-20",
                "source_file": "matrix.pdf",
            },
        )
        self.assertTrue(row["imported_at"].endswith("Z"))
        self.assertEqual(row["active_locks"], ["DC-1"])
        self.assertEqual(row["planned_locks"], ["DC-2"])
        self.assertEqual(row["removed_locks"], ["DC-2"])
        self.assertEqual(row["desired_locks"], ["DC-1", "DC-2"])
        self.assertEqual(row["groups"], ["A", "L"])

    def test_encode_backup_is_utf8_json_and_excludes_auth_data(self):
        get_user_model().objects.create_user(
            username="not-in-backup", password="secret-password"
        )

        encoded = data_transfer.encode_backup(
            exported_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        )
        decoded = json.loads(encoded.decode("utf-8"))

        self.assertEqual(decoded["format"], "schliessmatrix-backup")
        self.assertNotIn("not-in-backup", encoded.decode("utf-8"))
        self.assertEqual(
            set(decoded),
            {"format", "version", "exported_at", "locks", "groups", "transponders"},
        )

    def test_export_query_count_does_not_grow_with_records(self):
        with CaptureQueriesContext(connection) as initial_queries:
            data_transfer.build_backup()

        for index, code in enumerate("BCDEF"):
            lock = Lock.objects.create(serial=f"EXTRA-{index}")
            group = Group.objects.create(name=f"Extra {index}", export_code=code)
            group.doors.add(lock)
            transponder = Transponder.objects.create(serial=f"EXTRA-{index}")
            transponder.locks.add(lock)
            transponder.groups.add(group)

        with CaptureQueriesContext(connection) as expanded_queries:
            data_transfer.build_backup()

        self.assertEqual(len(expanded_queries), len(initial_queries))


class DataTransferValidationTests(TestCase):
    def minimal_backup(self):
        return {
            "format": "schliessmatrix-backup",
            "version": 1,
            "exported_at": "2026-07-28T12:00:00Z",
            "locks": [
                {
                    "serial": "DC-1",
                    "door_name": "Eingang",
                    "room_number": "",
                    "location": "",
                    "area": "",
                }
            ],
            "groups": [
                {
                    "name": "Allgemein",
                    "export_code": "a",
                    "is_implicit": True,
                    "doors": ["DC-1"],
                }
            ],
            "transponders": [
                {
                    "serial": "T-1",
                    "asta_number": 1,
                    "person_name": "Mia",
                    "locking_system": "",
                    "printed_on": None,
                    "source_file": "",
                    "imported_at": "2026-07-28T10:00:00Z",
                    "active_locks": ["DC-1"],
                    "planned_locks": [],
                    "removed_locks": [],
                    "desired_locks": ["DC-1"],
                    "groups": ["a"],
                }
            ],
        }

    def encoded(self, backup=None):
        return json.dumps(backup or self.minimal_backup()).encode()

    def test_valid_backup_is_normalized_and_dates_are_parsed(self):
        backup = self.minimal_backup()
        backup["transponders"][0]["printed_on"] = "2026-07-20"

        parsed = data_transfer.parse_backup(self.encoded(backup))

        self.assertEqual(parsed["groups"][0]["export_code"], "A")
        self.assertEqual(parsed["groups"][0]["doors"], ["DC-1"])
        self.assertEqual(parsed["transponders"][0]["groups"], ["A"])
        self.assertEqual(parsed["transponders"][0]["printed_on"], date(2026, 7, 20))
        self.assertEqual(
            parsed["transponders"][0]["imported_at"],
            datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        )

    def test_rejects_invalid_encoding_json_shape_and_duplicate_keys(self):
        bad_content = [
            b"",
            b"\xff",
            b"{",
            b"[]",
            b'{"format":"schliessmatrix-backup","format":"other"}',
        ]
        for content in bad_content:
            with (
                self.subTest(content=content),
                self.assertRaises(data_transfer.BackupValidationError),
            ):
                data_transfer.parse_backup(content)

    def test_rejects_wrong_format_version_and_record_keys(self):
        cases = []
        for key, value in (("format", "other"), ("version", 2), ("version", 0)):
            backup = self.minimal_backup()
            backup[key] = value
            cases.append(backup)
        missing_root = self.minimal_backup()
        del missing_root["locks"]
        cases.append(missing_root)
        unknown_root = self.minimal_backup()
        unknown_root["extra"] = []
        cases.append(unknown_root)
        unknown_record = self.minimal_backup()
        unknown_record["locks"][0]["extra"] = "x"
        cases.append(unknown_record)

        for backup in cases:
            with (
                self.subTest(backup=backup),
                self.assertRaises(data_transfer.BackupValidationError),
            ):
                data_transfer.parse_backup(self.encoded(backup))

    def test_rejects_invalid_scalar_values_dates_and_limits(self):
        mutations = [
            ("locks", "serial", "X" * 33),
            ("locks", "door_name", "X" * 256),
            ("groups", "name", "X" * 129),
            ("groups", "is_implicit", "yes"),
            ("transponders", "asta_number", True),
            ("transponders", "asta_number", 2**31),
            ("transponders", "printed_on", "28.07.2026"),
            ("transponders", "imported_at", "2026-07-28T10:00:00"),
        ]
        for collection, field, value in mutations:
            backup = self.minimal_backup()
            backup[collection][0][field] = value
            with (
                self.subTest(collection=collection, field=field),
                self.assertRaises(data_transfer.BackupValidationError),
            ):
                data_transfer.parse_backup(self.encoded(backup))

    def test_rejects_duplicate_identities_and_relationship_values(self):
        cases = []
        duplicate_lock = self.minimal_backup()
        duplicate_lock["locks"].append(deepcopy(duplicate_lock["locks"][0]))
        cases.append(duplicate_lock)
        duplicate_group = self.minimal_backup()
        second_group = deepcopy(duplicate_group["groups"][0])
        second_group.update(name="ALLGEMEIN", export_code="A", is_implicit=False)
        duplicate_group["groups"].append(second_group)
        cases.append(duplicate_group)
        duplicate_relation = self.minimal_backup()
        duplicate_relation["groups"][0]["doors"] = ["DC-1", "DC-1"]
        cases.append(duplicate_relation)
        two_implicit = self.minimal_backup()
        second_group = deepcopy(two_implicit["groups"][0])
        second_group.update(name="Lager", export_code="L")
        two_implicit["groups"].append(second_group)
        cases.append(two_implicit)

        for backup in cases:
            with (
                self.subTest(backup=backup),
                self.assertRaises(data_transfer.BackupValidationError),
            ):
                data_transfer.parse_backup(self.encoded(backup))

    def test_external_references_are_only_allowed_for_merge_validation(self):
        backup = self.minimal_backup()
        backup["groups"][0]["doors"] = ["RETAINED-LOCK"]
        backup["transponders"][0]["groups"] = ["R"]

        with self.assertRaises(data_transfer.BackupValidationError):
            data_transfer.parse_backup(self.encoded(backup))

        parsed = data_transfer.parse_backup(
            self.encoded(backup), allow_external_references=True
        )
        self.assertEqual(parsed["groups"][0]["doors"], ["RETAINED-LOCK"])
        self.assertEqual(parsed["transponders"][0]["groups"], ["R"])

    def test_size_limit_is_checked_before_json_parsing(self):
        content = self.encoded()
        with mock.patch.object(data_transfer, "MAX_BACKUP_BYTES", len(content)):
            self.assertEqual(data_transfer.parse_backup(content)["version"], 1)
        with (
            mock.patch.object(data_transfer, "MAX_BACKUP_BYTES", len(content) - 1),
            self.assertRaisesMessage(data_transfer.BackupValidationError, "10 MiB"),
        ):
            data_transfer.parse_backup(content)


class DataTransferReplaceTests(TestCase):
    def backup_content(self):
        backup = {
            "format": "schliessmatrix-backup",
            "version": 1,
            "exported_at": "2026-07-28T12:00:00Z",
            "locks": [
                {
                    "serial": "DC-1",
                    "door_name": "Eingang",
                    "room_number": "0.01",
                    "location": "MUC.1.EG",
                    "area": "Allgemein",
                },
                {
                    "serial": "DC-2",
                    "door_name": "Lager",
                    "room_number": "1.02",
                    "location": "MUC.1.OG",
                    "area": "Intern",
                },
            ],
            "groups": [
                {
                    "name": "AStA Allgemein",
                    "export_code": "A",
                    "is_implicit": True,
                    "doors": ["DC-1"],
                },
                {
                    "name": "AStA Lager",
                    "export_code": "L",
                    "is_implicit": False,
                    "doors": ["DC-2"],
                },
            ],
            "transponders": [
                {
                    "serial": "T-1",
                    "asta_number": 7,
                    "person_name": "Muster, Mia",
                    "locking_system": "Anlage 1",
                    "printed_on": "2026-07-20",
                    "source_file": "matrix.pdf",
                    "imported_at": "2026-07-21T08:30:00Z",
                    "active_locks": ["DC-1"],
                    "planned_locks": ["DC-2"],
                    "removed_locks": ["DC-2"],
                    "desired_locks": ["DC-1", "DC-2"],
                    "groups": ["A", "L"],
                }
            ],
        }
        return json.dumps(backup).encode()

    def seed_old_state(self):
        old_lock = Lock.objects.create(serial="OLD-LOCK", door_name="Alt")
        old_group = Group.objects.create(name="Altgruppe", export_code="O")
        old_group.doors.add(old_lock)
        old_tp = Transponder.objects.create(serial="OLD-TP", person_name="Alt")
        old_tp.locks.add(old_lock)
        old_tp.groups.add(old_group)

    def test_replace_restores_exact_domain_state_and_preserves_accounts(self):
        user = get_user_model().objects.create_user("keeper", password="safe-password")
        self.seed_old_state()

        result = data_transfer.restore_backup(
            self.backup_content(), mode="replace", replace_confirmed=True
        )

        self.assertEqual(
            result,
            data_transfer.ImportResult(
                mode="replace",
                created_locks=2,
                updated_locks=0,
                created_groups=2,
                updated_groups=0,
                created_transponders=1,
                updated_transponders=0,
            ),
        )
        self.assertFalse(Lock.objects.filter(serial="OLD-LOCK").exists())
        self.assertFalse(Group.objects.filter(export_code="O").exists())
        self.assertFalse(Transponder.objects.filter(serial="OLD-TP").exists())
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())

        lock = Lock.objects.get(serial="DC-1")
        self.assertEqual(
            (lock.door_name, lock.room_number, lock.location, lock.area),
            ("Eingang", "0.01", "MUC.1.EG", "Allgemein"),
        )
        group = Group.objects.get(export_code="A")
        self.assertTrue(group.is_implicit)
        self.assertEqual(set(group.doors.values_list("serial", flat=True)), {"DC-1"})
        tp = Transponder.objects.get(serial="T-1")
        self.assertEqual(tp.printed_on, date(2026, 7, 20))
        self.assertEqual(tp.imported_at, datetime(2026, 7, 21, 8, 30, tzinfo=UTC))
        self.assertEqual(set(tp.locks.values_list("serial", flat=True)), {"DC-1"})
        self.assertEqual(
            set(tp.planned_locks.values_list("serial", flat=True)), {"DC-2"}
        )
        self.assertEqual(
            set(tp.removed_locks.values_list("serial", flat=True)), {"DC-2"}
        )
        self.assertEqual(
            set(tp.desired_locks.values_list("serial", flat=True)), {"DC-1", "DC-2"}
        )
        self.assertEqual(
            set(tp.groups.values_list("export_code", flat=True)), {"A", "L"}
        )

    def test_replace_requires_confirmation_and_known_mode_before_mutation(self):
        self.seed_old_state()
        for kwargs in (
            {"mode": "replace"},
            {"mode": "unknown", "replace_confirmed": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(data_transfer.BackupValidationError):
                    data_transfer.restore_backup(self.backup_content(), **kwargs)
                self.assertTrue(Lock.objects.filter(serial="OLD-LOCK").exists())

    def test_replace_rolls_back_when_relationship_writes_fail(self):
        self.seed_old_state()

        with (
            mock.patch.object(
                data_transfer,
                "_set_relationships",
                side_effect=IntegrityError("forced relationship failure"),
            ),
            self.assertRaises(IntegrityError),
        ):
            data_transfer.restore_backup(
                self.backup_content(), mode="replace", replace_confirmed=True
            )

        self.assertEqual(
            set(Lock.objects.values_list("serial", flat=True)), {"OLD-LOCK"}
        )
        self.assertEqual(
            set(Transponder.objects.values_list("serial", flat=True)), {"OLD-TP"}
        )
        self.assertEqual(
            set(Group.objects.values_list("export_code", flat=True)), {"O"}
        )


class DataTransferMergeTests(TestCase):
    def setUp(self):
        self.matching_lock = Lock.objects.create(serial="DC-1", door_name="Alt")
        self.retained_lock = Lock.objects.create(serial="R-LOCK", door_name="Bleibt")
        self.unrelated_lock = Lock.objects.create(
            serial="U-LOCK", door_name="Unberührt"
        )
        self.matching_group = Group.objects.create(name="Alte Gruppe", export_code="A")
        self.retained_group = Group.objects.create(
            name="Behalten", export_code="R", is_implicit=True
        )
        self.unrelated_group = Group.objects.create(name="Unberührt", export_code="U")
        self.retained_group.doors.add(self.retained_lock)
        self.unrelated_group.doors.add(self.unrelated_lock)
        self.matching_tp = Transponder.objects.create(serial="T-1", person_name="Alt")
        self.matching_tp.locks.add(self.retained_lock)
        self.matching_tp.groups.add(self.retained_group)
        self.unrelated_tp = Transponder.objects.create(
            serial="U-TP", person_name="Unberührt"
        )
        self.unrelated_tp.locks.add(self.unrelated_lock)
        self.unrelated_tp.groups.add(self.unrelated_group)

    def merge_backup(self, *, implicit=True):
        return {
            "format": "schliessmatrix-backup",
            "version": 1,
            "exported_at": "2026-07-28T12:00:00Z",
            "locks": [
                {
                    "serial": "DC-1",
                    "door_name": "Neu",
                    "room_number": "1.01",
                    "location": "MUC",
                    "area": "Import",
                },
                {
                    "serial": "DC-2",
                    "door_name": "Neu angelegt",
                    "room_number": "",
                    "location": "",
                    "area": "",
                },
            ],
            "groups": [
                {
                    "name": "Importgruppe",
                    "export_code": "A",
                    "is_implicit": implicit,
                    "doors": ["DC-2", "R-LOCK"],
                }
            ],
            "transponders": [
                {
                    "serial": "T-1",
                    "asta_number": 9,
                    "person_name": "Neu",
                    "locking_system": "Anlage 2",
                    "printed_on": None,
                    "source_file": "backup.json",
                    "imported_at": "2026-07-22T09:00:00Z",
                    "active_locks": ["DC-1"],
                    "planned_locks": ["R-LOCK"],
                    "removed_locks": [],
                    "desired_locks": ["DC-1", "DC-2"],
                    "groups": ["A", "R"],
                },
                {
                    "serial": "T-2",
                    "asta_number": None,
                    "person_name": "Neu angelegt",
                    "locking_system": "",
                    "printed_on": None,
                    "source_file": "",
                    "imported_at": "2026-07-22T09:00:00Z",
                    "active_locks": ["DC-2"],
                    "planned_locks": [],
                    "removed_locks": [],
                    "desired_locks": ["DC-2"],
                    "groups": ["A"],
                },
            ],
        }

    def encoded(self, backup=None):
        return json.dumps(backup or self.merge_backup()).encode()

    def test_merge_upserts_imported_records_and_preserves_unrelated_state(self):
        result = data_transfer.restore_backup(self.encoded(), mode="merge")

        self.assertEqual(
            result,
            data_transfer.ImportResult(
                mode="merge",
                created_locks=1,
                updated_locks=1,
                created_groups=0,
                updated_groups=1,
                created_transponders=1,
                updated_transponders=1,
            ),
        )
        self.matching_lock.refresh_from_db()
        self.assertEqual(
            (self.matching_lock.door_name, self.matching_lock.area),
            ("Neu", "Import"),
        )
        self.matching_group.refresh_from_db()
        self.assertEqual(self.matching_group.name, "Importgruppe")
        self.assertTrue(self.matching_group.is_implicit)
        self.assertEqual(
            set(self.matching_group.doors.values_list("serial", flat=True)),
            {"DC-2", "R-LOCK"},
        )
        self.retained_group.refresh_from_db()
        self.assertFalse(self.retained_group.is_implicit)

        self.matching_tp.refresh_from_db()
        self.assertEqual(
            (self.matching_tp.person_name, self.matching_tp.asta_number), ("Neu", 9)
        )
        self.assertEqual(
            set(self.matching_tp.locks.values_list("serial", flat=True)), {"DC-1"}
        )
        self.assertEqual(
            set(self.matching_tp.planned_locks.values_list("serial", flat=True)),
            {"R-LOCK"},
        )
        self.assertEqual(
            set(self.matching_tp.groups.values_list("export_code", flat=True)),
            {"A", "R"},
        )

        self.unrelated_lock.refresh_from_db()
        self.assertEqual(self.unrelated_lock.door_name, "Unberührt")
        self.assertEqual(
            set(self.unrelated_group.doors.values_list("serial", flat=True)),
            {"U-LOCK"},
        )
        self.assertEqual(
            set(self.unrelated_tp.locks.values_list("serial", flat=True)),
            {"U-LOCK"},
        )

    def test_merge_without_imported_implicit_group_retains_existing_one(self):
        data_transfer.restore_backup(
            self.encoded(self.merge_backup(implicit=False)), mode="merge"
        )

        self.retained_group.refresh_from_db()
        self.matching_group.refresh_from_db()
        self.assertTrue(self.retained_group.is_implicit)
        self.assertFalse(self.matching_group.is_implicit)

    def test_imported_group_can_clear_its_own_implicit_flag(self):
        backup = self.merge_backup(implicit=False)
        backup["groups"] = [
            {
                "name": "Behalten",
                "export_code": "R",
                "is_implicit": False,
                "doors": ["R-LOCK"],
            }
        ]
        backup["transponders"] = []

        data_transfer.restore_backup(self.encoded(backup), mode="merge")

        self.retained_group.refresh_from_db()
        self.assertFalse(self.retained_group.is_implicit)

    def test_merge_rejects_group_identity_conflict_before_mutation(self):
        backup = self.merge_backup()
        backup["groups"][0]["name"] = "Unberührt"

        with self.assertRaises(data_transfer.BackupValidationError):
            data_transfer.restore_backup(self.encoded(backup), mode="merge")

        self.matching_lock.refresh_from_db()
        self.assertEqual(self.matching_lock.door_name, "Alt")
        self.assertEqual(self.matching_group.name, "Alte Gruppe")

    def test_merge_rejects_references_missing_from_file_and_database(self):
        backup = self.merge_backup()
        backup["groups"][0]["doors"] = ["MISSING"]

        with self.assertRaises(data_transfer.BackupValidationError):
            data_transfer.restore_backup(self.encoded(backup), mode="merge")

        self.assertFalse(Lock.objects.filter(serial="DC-2").exists())

    def test_merge_rolls_back_scalar_updates_when_relationship_writes_fail(self):
        with (
            mock.patch.object(
                data_transfer,
                "_set_relationships",
                side_effect=IntegrityError("forced relationship failure"),
            ),
            self.assertRaises(IntegrityError),
        ):
            data_transfer.restore_backup(self.encoded(), mode="merge")

        self.matching_lock.refresh_from_db()
        self.matching_group.refresh_from_db()
        self.matching_tp.refresh_from_db()
        self.assertEqual(self.matching_lock.door_name, "Alt")
        self.assertEqual(self.matching_group.name, "Alte Gruppe")
        self.assertEqual(self.matching_tp.person_name, "Alt")
        self.assertFalse(Lock.objects.filter(serial="DC-2").exists())
        self.assertFalse(Transponder.objects.filter(serial="T-2").exists())


def _web_backup(*, lock_serial="WEB-1"):
    return json.dumps(
        {
            "format": "schliessmatrix-backup",
            "version": 1,
            "exported_at": "2026-07-28T12:00:00Z",
            "locks": [
                {
                    "serial": lock_serial,
                    "door_name": "Webimport",
                    "room_number": "",
                    "location": "",
                    "area": "",
                }
            ],
            "groups": [],
            "transponders": [],
        }
    ).encode()


class DataTransferViewTests(TestCase):
    def upload(self, content=None, **data):
        payload = {
            "mode": "merge",
            "backup": SimpleUploadedFile(
                "backup.json", content or _web_backup(), "application/json"
            ),
            **data,
        }
        return self.client.post("/data/import/", payload, follow=True)

    def test_data_page_renders(self):
        response = self.client.get("/data/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "access/data_transfer.html")

    def test_export_download_has_private_safe_headers(self):
        response = self.client.get("/data/export/?filename=attacker.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertRegex(
            response["Content-Disposition"],
            r'^attachment; filename="schliessmatrix-backup-\d{4}-\d{2}-\d{2}\.json"$',
        )
        self.assertNotIn("attacker", response["Content-Disposition"])
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Pragma"], "no-cache")
        self.assertEqual(
            json.loads(response.content)["format"], "schliessmatrix-backup"
        )

    def test_merge_upload_imports_and_reports_counts(self):
        response = self.upload()

        self.assertRedirects(response, "/data/")
        self.assertTrue(Lock.objects.filter(serial="WEB-1").exists())
        self.assertContains(response, "1 Tür neu")
        self.assertContains(response, "Zusammenführen abgeschlossen")

    def test_import_rejects_missing_or_multiple_files(self):
        missing = self.client.post("/data/import/", {"mode": "merge"}, follow=True)
        multiple = self.client.post(
            "/data/import/",
            {
                "mode": "merge",
                "backup": [
                    SimpleUploadedFile("one.json", _web_backup()),
                    SimpleUploadedFile("two.json", _web_backup()),
                ],
            },
            follow=True,
        )

        self.assertContains(missing, "genau eine Sicherungsdatei")
        self.assertContains(multiple, "genau eine Sicherungsdatei")
        self.assertFalse(Lock.objects.filter(serial="WEB-1").exists())

    def test_import_surfaces_validation_errors_without_mutating(self):
        invalid_json = self.upload(b"not-json")
        bad_mode = self.upload(mode="unknown")

        self.assertContains(invalid_json, "ungültiges JSON")
        self.assertContains(bad_mode, "Unbekannter Importmodus")
        self.assertFalse(Lock.objects.filter(serial="WEB-1").exists())

    def test_replace_requires_confirmation_and_can_replace(self):
        Lock.objects.create(serial="OLD")

        rejected = self.upload(mode="replace")
        self.assertContains(rejected, "ausdrücklich bestätigt")
        self.assertTrue(Lock.objects.filter(serial="OLD").exists())

        accepted = self.upload(mode="replace", confirm_replace="on")
        self.assertContains(accepted, "Ersetzen abgeschlossen")
        self.assertFalse(Lock.objects.filter(serial="OLD").exists())
        self.assertTrue(Lock.objects.filter(serial="WEB-1").exists())

    def test_import_database_failure_uses_generic_message(self):
        with (
            mock.patch.object(
                data_transfer,
                "restore_backup",
                side_effect=DatabaseError("private detail"),
            ),
            self.assertLogs("access.views", level="ERROR") as logs,
        ):
            response = self.upload()

        self.assertContains(response, "Import fehlgeschlagen")
        self.assertNotContains(response, "private detail")
        self.assertIn("Domain data import failed", logs.output[0])

    def test_import_is_post_only(self):
        self.assertEqual(self.client.get("/data/import/").status_code, 405)

    def test_import_reads_only_enough_bytes_to_enforce_the_size_limit(self):
        class GuardedUpload:
            def read(self, size=None):
                if size is None or size > data_transfer.MAX_BACKUP_BYTES + 1:
                    raise AssertionError("upload read was not bounded")
                return b"{}"

        request = RequestFactory().post("/data/import/", {"mode": "merge"})
        _ = request.POST
        request._files = MultiValueDict({"backup": [GuardedUpload()]})
        request.session = {}
        request._messages = FallbackStorage(request)

        response = views.data_import(request)

        self.assertEqual(response.status_code, 302)


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
class DataTransferAuthTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "normal-user", password="safe-password"
        )

    def test_anonymous_user_is_redirected_from_all_data_routes(self):
        for method, url in (
            ("get", "/data/"),
            ("get", "/data/export/"),
            ("post", "/data/import/"),
        ):
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_normal_authenticated_user_can_import_and_export(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/data/").status_code, 200)
        self.assertEqual(self.client.get("/data/export/").status_code, 200)
        response = self.client.post(
            "/data/import/",
            {
                "mode": "merge",
                "backup": SimpleUploadedFile("backup.json", _web_backup()),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Lock.objects.filter(serial="WEB-1").exists())

    def test_import_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            "/data/import/",
            {
                "mode": "merge",
                "backup": SimpleUploadedFile("backup.json", _web_backup()),
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Lock.objects.filter(serial="WEB-1").exists())


class DataTransferTemplateTests(TestCase):
    def test_page_explains_export_scope_and_both_import_modes(self):
        response = self.client.get("/data/")

        self.assertContains(response, "Daten sichern")
        self.assertContains(response, "Daten importieren")
        self.assertContains(response, "Zusammenführen")
        self.assertContains(response, "Alle Daten ersetzen")
        self.assertContains(response, "Anmeldekonten werden nicht exportiert")
        self.assertContains(response, 'accept="application/json,.json"')
        self.assertContains(response, 'name="mode" value="merge"')
        self.assertContains(response, 'name="mode" value="replace"')
        self.assertContains(response, 'value="merge" checked x-model="mode"')

    def test_replace_mode_has_visible_warning_and_confirmation_contract(self):
        response = self.client.get("/data/")

        self.assertContains(response, "Bestehende Schließmatrix-Daten werden gelöscht")
        self.assertContains(response, 'name="confirm_replace" value="on"')
        self.assertContains(response, "mode === 'replace'")

    def test_desktop_and_mobile_navigation_link_to_data_page(self):
        response = self.client.get("/data/")

        self.assertContains(response, "data-page-link", count=2)
        self.assertContains(response, 'href="/data/"', count=2)
        self.assertContains(response, 'class="nav-link nav-active"', count=2)
