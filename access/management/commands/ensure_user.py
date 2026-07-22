"""Create or update the login user from the environment (idempotent).

Used by the NixOS service to provision the initial account on every start:

    KEYMGMT_ADMIN_USERNAME=admin \
    KEYMGMT_ADMIN_PASSWORD_FILE=/run/secrets/keymgmt-admin \
    python manage.py ensure_user

Reads the password from KEYMGMT_ADMIN_PASSWORD or, preferably, from the file at
KEYMGMT_ADMIN_PASSWORD_FILE. Does nothing (and does not fail) when no username
or password is configured, so the service can start before any is set.
"""

import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or update the login user from KEYMGMT_ADMIN_* env vars."

    def handle(self, *args, **opts):
        username = (os.environ.get("KEYMGMT_ADMIN_USERNAME") or "").strip()
        password = os.environ.get("KEYMGMT_ADMIN_PASSWORD")
        pw_file = os.environ.get("KEYMGMT_ADMIN_PASSWORD_FILE")
        if not password and pw_file and Path(pw_file).exists():
            password = Path(pw_file).read_text().strip()

        if not username or not password:
            self.stdout.write(
                "KEYMGMT_ADMIN_USERNAME / password not set — skipping user "
                "provisioning.")
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        user.set_password(password)
        user.is_active = True
        # is_staff/is_superuser are harmless (the Django admin is not exposed)
        # and let the account manage others via `manage.py` if ever needed.
        user.is_staff = True
        user.is_superuser = True
        user.save()
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} login user {username!r}."))
