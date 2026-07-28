import re
import unicodedata

from django.db import migrations, models
from django.db.models.functions import Lower


def backfill_group_metadata(apps, schema_editor):
    Group = apps.get_model("access", "Group")
    groups = list(Group.objects.order_by(Lower("name"), "pk"))
    implicit_candidates = [
        group for group in groups if group.name.casefold() == "asta allgemein"
    ]
    exact = [group for group in implicit_candidates if group.name == "AStA Allgemein"]
    implicit = (exact or implicit_candidates or [None])[0]
    implicit_pk = implicit.pk if implicit is not None else None
    used = set()
    for group in groups:
        semantic = re.sub(r"^AStA\s+", "", group.name.strip(), flags=re.IGNORECASE)
        ascii_name = (
            unicodedata.normalize("NFKD", semantic)
            .encode("ascii", "ignore")
            .decode("ascii")
            .upper()
        )
        stem = "".join(char for char in ascii_name if char.isalnum())
        code = next(
            (
                stem[:length]
                for length in range(1, min(4, len(stem)) + 1)
                if stem[:length] not in used
            ),
            None,
        )
        if code is None:
            raise RuntimeError(
                f"Cannot derive a unique export code for group {group.name!r}"
            )
        group.export_code = code
        group.is_implicit = group.pk == implicit_pk
        group.save(update_fields=["export_code", "is_implicit"])
        used.add(code)


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0006_transponder_removed_locks"),
    ]

    operations = [
        migrations.AddField(
            model_name="group",
            name="export_code",
            field=models.CharField(max_length=4, null=True),
        ),
        migrations.AddField(
            model_name="group",
            name="is_implicit",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_group_metadata, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="group",
            name="export_code",
            field=models.CharField(max_length=4),
        ),
        migrations.AddConstraint(
            model_name="group",
            constraint=models.CheckConstraint(
                condition=models.Q(export_code__regex=r"^[A-Z0-9]{1,4}\Z"),
                name="access_group_export_code_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="group",
            constraint=models.UniqueConstraint(
                Lower("export_code"),
                name="access_group_export_code_ci_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="group",
            constraint=models.UniqueConstraint(
                fields=("is_implicit",),
                condition=models.Q(is_implicit=True),
                name="access_group_single_implicit",
            ),
        ),
    ]
