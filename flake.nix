{
  description = "Schließmatrix (keymgmt) — SimonsVoss transponder access overview (Django)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};
    in
    {
      # The application source as a store path.
      packages = forAll (system: {
        default = (pkgsFor system).callPackage ./nix/package.nix { };
        keymgmt = self.packages.${system}.default;
      });

      # NixOS module. Import it and set `services.keymgmt.enable = true`.
      # `services.keymgmt.package` defaults to this flake's package.
      nixosModules.default = { pkgs, lib, ... }: {
        imports = [ ./nix/module.nix ];
        services.keymgmt.package = lib.mkDefault
          self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };

      # Dev shell: `nix develop` gives Python + typst + tesseract + uv.
      devShells = forAll (system:
        let pkgs = pkgsFor system;
        in {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: with ps; [
                django
                pdfplumber
                pillow
                gunicorn
              ]))
              pkgs.typst
              pkgs.tesseract
              pkgs.uv
            ];
          };
        });
    };
}
