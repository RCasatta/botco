# NixOS service for the orchestrator. On the host:
#
#   services.botco = {
#     enable = true;
#     settings = {                      # same shape as config.example.toml
#       accounts.writer.zuliprc = config.age.secrets.botco-writer-zuliprc.path;
#       ...
#     };
#   };
#
# Secrets (zuliprc files, x.env) should be owned by the botco user.
#
# `labDir` makes a lab notebook (markdown experiment reports) readable by the
# agents: it is bind-mounted read-only at /run/botco-lab inside the service,
# so the service user needs no access to the directories around it.
#
# `sessions.enable` lets accounts with a session engine run: botco starts
# each session as a transient systemd unit (botco-session-*) through
# systemd-run, and a polkit rule allows the botco user to start and stop
# those units, and only those. Put the session command (pi) in `path`.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.botco;
  labMount = "/run/botco-lab";
  defaults = { state_dir = "/var/lib/botco"; }
    // lib.optionalAttrs (cfg.labDir != null) { sources.lab = { dir = labMount; host_dir = cfg.labDir; }; };
  configFile = (pkgs.formats.toml { }).generate "botco.toml" (lib.recursiveUpdate defaults cfg.settings);
in
{
  options.services.botco = {
    enable = lib.mkEnableOption "the bot company orchestrator";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
    };
    labDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/alice/inference";
      description = "Directory with the lab notebook, shown read-only to the service.";
    };
    sessions.enable = lib.mkEnableOption "session engines (pi in a systemd sandbox)";
    path = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      description = "Packages on the service's PATH, e.g. the pi coding agent for sessions.";
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

    security.polkit.enable = lib.mkIf cfg.sessions.enable true;
    security.polkit.extraConfig = lib.mkIf cfg.sessions.enable ''
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "botco") {
          var unit = action.lookup("unit") || "";
          if (unit.indexOf("botco-session-") == 0) {
            return polkit.Result.YES;
          }
        }
      });
    '';

    systemd.services.botco = {
      description = "Bot company orchestrator";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      path = cfg.path ++ lib.optional cfg.sessions.enable config.systemd.package;
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
        BindReadOnlyPaths = lib.optional (cfg.labDir != null) "${cfg.labDir}:${labMount}";
      };
    };
  };
}
