# UCORE host services

This directory contains the systemd user services, timers, WebUI drop-in and
Podman Quadlets used by the UCORE catalog host. The scan starts the LiteLLM
import and the Oracle and Cloudflare publication jobs. A successful import
starts the post-import service, which acknowledges the receipt and refreshes
the client catalogs in `fedora45-ai-safrano9999-ucore`.

Run the following as the host service user from the permanent
`~/litellm-free/update` checkout:

```sh
./services/link-host.sh
```

The script links the unit and Quadlet definitions from this checkout and
reloads the systemd user manager. Existing timer enablement is preserved.
On a fresh host, enable the scheduled jobs after provisioning the runtime:

```sh
systemctl --user enable --now litellm-free-scan.timer litellm-free-caddy-log-export.timer litellm-free-recent-visitors.timer
```

The units use the host's existing scanner and WebUI, Cloudflare and Oracle
scripts, environment files, Caddy, Podman network and GoAccess volume. The
standalone importer in [deploy](../deploy/) has its own timer. On the UCORE
host, the scan triggers the import.
