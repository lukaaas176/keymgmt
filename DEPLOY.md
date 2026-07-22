# Deployment & Security

This app is a Django/SQLite service. It runs under **gunicorn**, keeps its data
in a single SQLite file, and shells out to **typst** (PDF export) and optionally
**tesseract** (image import). It is meant to sit **behind a reverse proxy** that
terminates TLS — the included NixOS module binds gunicorn to loopback only.

## Security model

1. **The whole UI requires a login.** `LoginRequiredMiddleware` gates every page
   (`KEYMGMT_REQUIRE_LOGIN=1`, the default in production). Only
   `/accounts/login/` is public. There is no Django admin exposed.
2. **No secrets in the image.** `SECRET_KEY`, hosts, and the login password come
   from the environment / secret files at runtime. The app **refuses to start**
   with `DEBUG=0` unless a real `SECRET_KEY` is set.
3. **Proxy-aware hardening.** Secure + HttpOnly + SameSite cookies, CSRF trusted
   origins, `X-Frame-Options: DENY`, no content-type sniffing, and
   `SECURE_PROXY_SSL_HEADER` so Django trusts your proxy's TLS.
4. **Systemd hardening.** Dedicated user, `ProtectSystem=strict`, `ProtectHome`,
   `PrivateTmp`, a syscall allow-list, and a writable path limited to the DB dir.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `KEYMGMT_SECRET_KEY` / `KEYMGMT_SECRET_KEY_FILE` | — | Django secret key (required when `DEBUG=0`). |
| `KEYMGMT_DEBUG` | `0` | `1` enables Django debug (local dev only). |
| `KEYMGMT_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated Host allow-list. |
| `KEYMGMT_CSRF_TRUSTED_ORIGINS` | — | e.g. `https://schliessmatrix.example.org` (needed for the AJAX editors behind TLS). |
| `KEYMGMT_DB_PATH` | `<repo>/db.sqlite3` | SQLite file path. |
| `KEYMGMT_REQUIRE_LOGIN` | `1` | `0` only behind a trusted auth proxy. |
| `KEYMGMT_ADMIN_USERNAME` / `KEYMGMT_ADMIN_PASSWORD_FILE` | — | Provision the initial login user (idempotent, via `manage.py ensure_user`). |
| `KEYMGMT_HSTS_SECONDS` | `0` | If > 0, Django also emits HSTS (usually leave it to nginx). |
| `KEYMGMT_LANGUAGE_CODE` / `KEYMGMT_TIME_ZONE` | `de` / `Europe/Berlin` | Locale. |

---

## Deploy on NixOS (flake)

The repo is a flake exposing `nixosModules.default`. Reference it from your host
flake and enable the service.

```nix
# flake.nix of your NixOS host
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    keymgmt.url = "github:lukaaas176/keymgmt";
    keymgmt.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, keymgmt, ... }: {
    nixosConfigurations.homeserver = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        keymgmt.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

```nix
# configuration.nix — the example that goes with it
{ config, ... }:
{
  services.keymgmt = {
    enable = true;
    domain = "schliessmatrix.example.org";   # -> ALLOWED_HOSTS + CSRF origin
    address = "127.0.0.1";                    # loopback; nginx proxies to it
    port = 8022;

    # Secrets owned by the `keymgmt` user (see agenix/sops note below).
    secretKeyFile = config.age.secrets.keymgmt-secret.path;
    adminUsername = "lukas";
    adminPasswordFile = config.age.secrets.keymgmt-admin.path;
  };
}
```

On rebuild the service runs migrations, provisions the login user, and starts
gunicorn on `127.0.0.1:8022`. The SQLite DB lives at `/var/lib/keymgmt/db.sqlite3`.

### Secrets

Use whatever you already run. Two examples for the secret key:

```bash
# generate a key
python -c 'import secrets; print(secrets.token_urlsafe(64))'
```

* **agenix** — `age.secrets.keymgmt-secret = { file = ./secrets/keymgmt-secret.age; owner = "keymgmt"; };`
* **sops-nix** — `sops.secrets."keymgmt/secret" = { owner = "keymgmt"; };` and point `secretKeyFile` at `config.sops.secrets."keymgmt/secret".path`.

> Do **not** pass a plain `./file` as `secretKeyFile` — Nix would copy it into
> the world-readable store. Always use a secret manager (or a file under
> `/var/lib` you create out of band) owned by the `keymgmt` user.

---

## nginx reverse proxy

You already run nginx. Add a vhost that terminates TLS and forwards to gunicorn.
The two must-haves are `X-Forwarded-Proto` (so Django knows it's HTTPS) and a
raised `client_max_body_size` (matrix PDFs are large).

```nginx
server {
    listen 443 ssl http2;
    server_name schliessmatrix.example.org;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    # HSTS (owned here, not in Django)
    add_header Strict-Transport-Security "max-age=63072000" always;

    client_max_body_size 25m;      # PDF uploads

    location / {
        proxy_pass         http://127.0.0.1:8022;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;   # required
        proxy_read_timeout 120s;                        # PDF export/typst
    }
}

server {                    # redirect http -> https
    listen 80;
    server_name schliessmatrix.example.org;
    return 301 https://$host$request_uri;
}
```

### Optional: a second auth layer (defense in depth)

The app already requires a login. If you also want the site invisible to the
public internet, add HTTP Basic Auth in nginx, or route it through your existing
SSO (Authelia/authentik) with `auth_request`. If you front it with a *trusted*
SSO that authenticates every request, you may set
`services.keymgmt.requireLogin = false` to avoid a double login — otherwise keep
the Django gate on.

---

## Backups

Everything is in one SQLite file. The module runs an **automated online backup**
(WAL-safe `.backup`, then prune) on a systemd timer — enabled by default:

```nix
services.keymgmt.backup = {
  enable = true;                 # default
  interval = "daily";            # any systemd OnCalendar, e.g. "*-*-* 03:00:00"
  keep = 14;                     # retain the newest 14 backups
  # directory = "/var/lib/keymgmt/backups";   # default; override if you like
};
```

Inspect or run it on demand:

```bash
systemctl list-timers keymgmt-backup
systemctl start keymgmt-backup      # one-off backup now
ls -lh /var/lib/keymgmt/backups
```

Point your off-host backup (restic/borg/…) at that directory. A manual one-off,
if ever needed:

```bash
sqlite3 /var/lib/keymgmt/db.sqlite3 ".backup '/tmp/keymgmt-$(date +%F).sqlite3'"
```

---

## Local development

Production defaults are secure, so for local work enable debug explicitly:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py runserver
```

Tests run with the gate and secret guard relaxed automatically:

```bash
uv run python manage.py test access
```
