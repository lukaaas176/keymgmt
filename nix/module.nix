{ config, lib, pkgs, ... }:

let
  cfg = config.services.keymgmt;

  pythonEnv = cfg.python.withPackages (ps:
    (with ps; [ django pdfplumber pillow gunicorn ]) ++ (cfg.extraPythonPackages ps));

  baseEnv = {
    DJANGO_SETTINGS_MODULE = "keymgmt.settings";
    PYTHONPATH = "${cfg.package}";
    PYTHONDONTWRITEBYTECODE = "1";
    KEYMGMT_DEBUG = "0";
    KEYMGMT_REQUIRE_LOGIN = if cfg.requireLogin then "1" else "0";
    KEYMGMT_DB_PATH = "/var/lib/${cfg.stateDir}/db.sqlite3";
    KEYMGMT_SECRET_KEY_FILE = "${cfg.secretKeyFile}";
    KEYMGMT_ALLOWED_HOSTS = lib.concatStringsSep ","
      (cfg.allowedHosts ++ lib.optional (cfg.domain != null) cfg.domain);
    KEYMGMT_CSRF_TRUSTED_ORIGINS =
      lib.optionalString (cfg.domain != null) "https://${cfg.domain}";
    KEYMGMT_HSTS_SECONDS = toString cfg.hstsSeconds;
    KEYMGMT_ADMIN_USERNAME = cfg.adminUsername;
  } // lib.optionalAttrs (cfg.adminPasswordFile != null) {
    KEYMGMT_ADMIN_PASSWORD_FILE = "${cfg.adminPasswordFile}";
  };

  preStart = pkgs.writeShellScript "keymgmt-pre-start" ''
    set -eu
    ${pythonEnv}/bin/python ${cfg.package}/manage.py migrate --noinput
    ${pythonEnv}/bin/python ${cfg.package}/manage.py ensure_user
  '';

  backupDir =
    if cfg.backup.directory != null then cfg.backup.directory
    else "/var/lib/${cfg.stateDir}/backups";

  # Online (WAL-safe) SQLite backup, then prune to the newest N.
  backupScript = pkgs.writeShellScript "keymgmt-backup" ''
    set -eu
    db="/var/lib/${cfg.stateDir}/db.sqlite3"
    dir="${backupDir}"
    dest="$dir/keymgmt-$(date +%Y-%m-%d_%H%M%S).sqlite3"
    sqlite3 "$db" ".backup '$dest'"
    echo "wrote $dest"
    ls -1t "$dir"/keymgmt-*.sqlite3 2>/dev/null \
      | tail -n +${toString (cfg.backup.keep + 1)} \
      | while read -r old; do rm -f "$old"; echo "pruned $old"; done
  '';
in
{
  options.services.keymgmt = {
    enable = lib.mkEnableOption "the Schließmatrix (keymgmt) web app";

    package = lib.mkOption {
      type = lib.types.package;
      description = "Application source tree (contains manage.py). Defaults to this flake's package.";
    };

    python = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python3;
      defaultText = lib.literalExpression "pkgs.python3";
      description = "Python package set to build the app environment from (needs Django ≥ 6.0).";
    };

    extraPythonPackages = lib.mkOption {
      type = lib.types.functionTo (lib.types.listOf lib.types.package);
      default = _: [ ];
      defaultText = lib.literalExpression "ps: [ ]";
      description = "Extra Python packages to add to the app environment.";
    };

    runtimePackages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ pkgs.typst pkgs.tesseract ];
      defaultText = lib.literalExpression "[ pkgs.typst pkgs.tesseract ]";
      description = "Runtime binaries on PATH. typst is required for PDF export; tesseract only for importing images of a matrix.";
    };

    address = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address gunicorn binds to. Keep it on loopback and put nginx in front.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8022;
      description = "Port gunicorn binds to.";
    };

    workers = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = "Number of gunicorn worker processes.";
    };

    domain = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "schliessmatrix.example.org";
      description = ''
        Public hostname. Added to ALLOWED_HOSTS, and as https://DOMAIN to
        CSRF_TRUSTED_ORIGINS (required for the AJAX Soll editors behind TLS).
      '';
    };

    allowedHosts = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "localhost" "127.0.0.1" "[::1]" ];
      description = "Host headers to accept (the domain, if set, is added automatically).";
    };

    secretKeyFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to a file holding Django's SECRET_KEY, readable by the service
        user (e.g. an agenix/sops secret with owner = "keymgmt"). Do NOT use a
        plain path in the Nix store — it would be world-readable.
      '';
    };

    requireLogin = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Gate the whole UI behind Django login. Disable only behind a trusted auth proxy.";
    };

    adminUsername = lib.mkOption {
      type = lib.types.str;
      default = "admin";
      description = "Login user provisioned on start (together with adminPasswordFile).";
    };

    adminPasswordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        File with the initial login password, readable by the service user.
        The account is created/updated on every start — rotate by changing the
        file and restarting. When null, provision the user manually instead.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "keymgmt";
      description = "systemd StateDirectory name; the SQLite DB lives in /var/lib/<stateDir>.";
    };

    user = lib.mkOption { type = lib.types.str; default = "keymgmt"; };
    group = lib.mkOption { type = lib.types.str; default = "keymgmt"; };

    hstsSeconds = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
      description = "If > 0, Django also emits an HSTS header (usually leave HSTS to nginx).";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional extra systemd EnvironmentFile for further KEYMGMT_* overrides.";
    };

    backup = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Periodically back up the SQLite database via a systemd timer.";
      };
      interval = lib.mkOption {
        type = lib.types.str;
        default = "daily";
        example = "*-*-* 03:00:00";
        description = "systemd OnCalendar expression for the backup timer.";
      };
      keep = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14;
        description = "Number of most-recent backups to retain (older ones are pruned).";
      };
      directory = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        defaultText = lib.literalExpression "\"/var/lib/\${stateDir}/backups\"";
        description = "Where backups are written (defaults to a subdirectory of the state dir).";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    users.users = lib.mkIf (cfg.user == "keymgmt") {
      keymgmt = {
        isSystemUser = true;
        group = cfg.group;
        description = "keymgmt service user";
      };
    };
    users.groups = lib.mkIf (cfg.group == "keymgmt") { keymgmt = { }; };

    systemd.services.keymgmt = {
      description = "Schließmatrix (keymgmt) web app";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      path = cfg.runtimePackages ++ [ pythonEnv ];
      environment = baseEnv;

      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = "${cfg.package}";
        StateDirectory = cfg.stateDir;
        StateDirectoryMode = "0750";

        ExecStartPre = "${preStart}";
        ExecStart = lib.concatStringsSep " " [
          "${pythonEnv}/bin/gunicorn keymgmt.wsgi:application"
          "--bind ${cfg.address}:${toString cfg.port}"
          "--workers ${toString cfg.workers}"
          "--pythonpath ${cfg.package}"
          "--access-logfile - --error-logfile -"
        ];

        Restart = "on-failure";
        RestartSec = "5s";

        # Hardening.
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "/var/lib/${cfg.stateDir}" ];
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        LockPersonality = true;
        SystemCallFilter = [ "@system-service" ];
        SystemCallErrorNumber = "EPERM";
        UMask = "0077";
      } // lib.optionalAttrs (cfg.environmentFile != null) {
        EnvironmentFile = cfg.environmentFile;
      };
    };

    # --- Automated SQLite backups (systemd timer) ---------------------------
    systemd.tmpfiles.rules = lib.mkIf cfg.backup.enable [
      "d ${backupDir} 0750 ${cfg.user} ${cfg.group} - -"
    ];

    systemd.services.keymgmt-backup = lib.mkIf cfg.backup.enable {
      description = "keymgmt SQLite backup";
      path = [ pkgs.sqlite pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${backupScript}";

        # Hardening: read the DB, write only the backup tree.
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = lib.unique [ "/var/lib/${cfg.stateDir}" backupDir ];
        RestrictAddressFamilies = [ "AF_UNIX" ];
        LockPersonality = true;
        SystemCallFilter = [ "@system-service" ];
        SystemCallErrorNumber = "EPERM";
        UMask = "0077";
      };
    };

    systemd.timers.keymgmt-backup = lib.mkIf cfg.backup.enable {
      description = "keymgmt SQLite backup timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.backup.interval;
        Persistent = true;
        RandomizedDelaySec = "5m";
      };
    };
  };
}
