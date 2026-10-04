# NixOS service for the orchestrator. On the host:
#
#   services.botco = {
#     enable = true;
#     settings = {                      # same shape as config.example.toml
#       personas.writer.zuliprc = config.age.secrets.botco-writer-zuliprc.path;
#       ...
#     };
#   };
#
# Secrets (zuliprc files, x.env) should be owned by the botco user.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.botco;
  configFile = (pkgs.formats.toml { }).generate "botco.toml" ({ state_dir = "/var/lib/botco"; } // cfg.settings);
in
{
  options.services.botco = {
    enable = lib.mkEnableOption "the bot company orchestrator";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
    };
    settings = lib.mkOption {
      type = (pkgs.formats.toml { }).type;
      default = { };
      description = "Contents of the TOML configuration; see config.example.toml.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.botco = {
      isSystemUser = true;
      group = "botco";
    };
    users.groups.botco = { };

    systemd.services.botco = {
      description = "Bot company orchestrator";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      # The orchestrator waits for the inference server by itself, so it does
      # not need to be ordered after it.
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/botco --config ${configFile}";
        User = "botco";
        Group = "botco";
        StateDirectory = "botco";
        WorkingDirectory = "/var/lib/botco";
        Restart = "always";
        RestartSec = 30;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      };
    };
  };
}
