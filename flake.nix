{
  description = "Bot company: a team of local-LLM personas collaborating on Zulip";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    (flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        py = pkgs.python3;
        deps = ps: [ ps.zulip ps.requests ps.requests-oauthlib ];
      in
      {
        packages.default = py.pkgs.buildPythonApplication {
          pname = "botco";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ py.pkgs.setuptools ];
          dependencies = deps py.pkgs;
          nativeCheckInputs = [ py.pkgs.pytestCheckHook ];
        };

        devShells.default = pkgs.mkShell {
          packages = [ (py.withPackages (ps: deps ps ++ [ ps.pytest ])) ];
        };
      })) // {
      nixosModules.default = import ./nixos-module.nix self;
    };
}
