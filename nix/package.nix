{ lib, stdenvNoCC }:

# The application *source* as a store path (manage.py at $out/manage.py). The
# Python environment and runtime tools (typst, tesseract) are assembled in the
# NixOS module, so the same source works with whatever nixpkgs/Python the host
# provides. Data, backups and the local DB are filtered out.
stdenvNoCC.mkDerivation {
  pname = "keymgmt";
  version = "0.1.0";

  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let base = baseNameOf (toString path);
      in
      base != ".git"
      && base != ".venv"
      && base != "result"
      && base != "__pycache__"
      && base != "scratchpad"
      && base != "to_check"
      && !(lib.hasSuffix ".sqlite3" base)
      && !(lib.hasInfix ".sqlite3." base) # db.sqlite3.bak-*, -wal, -shm
      && !(lib.hasSuffix ".pyc" base)
      && !(lib.hasSuffix ".pdf" base)
      && !(lib.hasSuffix ".csv" base);
  };

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cp -r . $out/
    runHook postInstall
  '';

  meta = {
    description = "Schließmatrix — SimonsVoss transponder access overview (Django)";
    platforms = lib.platforms.linux;
  };
}
