{
  description = "Bot company: a team of local-LLM personas collaborating on Zulip";

  inputs = {
    # Same revision as ripper in ~/systems, so the deployed package builds
    # against the Python the host already has.
    nixpkgs.url = "github:NixOS/nixpkgs/4975466d324710c576dc11ad614684e6bd8cad8e";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      py = pkgs.python3;
      deps = ps: [ ps.zulip ps.requests ps.requests-oauthlib ];
    in
    {
      packages.${system}.default = py.pkgs.buildPythonApplication {
        pname = "botco";
        version = "0.1.0";
        pyproject = true;
        src = ./.;
        build-system = [ py.pkgs.setuptools ];
        dependencies = deps py.pkgs;
        nativeCheckInputs = [ py.pkgs.pytestCheckHook ];
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = [ (py.withPackages (ps: deps ps ++ [ ps.pytest ])) ];
      };

      nixosModules.default = import ./nixos-module.nix self;
    };
}
