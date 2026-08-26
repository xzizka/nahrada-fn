# ansible

Deploys the `fulltext-poc` stack (see
[`../fulltext-poc/README.md`](../fulltext-poc/README.md)) to a remote Linux
host as systemd-managed Podman containers (Podman Quadlets), built from the
same `services/Dockerfile` and configuration the project's own
`docker-compose.yml` uses locally.

This is a **different runtime path from local dev**, not a replacement for
it: `fulltext-poc/docker-compose.yml` + `Makefile` (via `podman-compose`)
remain the quick way to run the stack on a dev box (`make up`, `make hello`,
...). This Ansible role instead generates one systemd unit per container
(`/etc/containers/systemd/ftpoc/*.container`) so the stack starts on boot,
restarts on failure, and is inspectable with plain `systemctl`/`journalctl` -
appropriate for a host that's meant to keep running unattended. The two paths
use the same image and the same `fulltext-poc/postgres`, `opensearch`, `tika`
config files, but do not share a network or ports - don't run both on the
same host at once (see Notes).

Podman itself is daemonless and runs rootful here (via Ansible's
`become: true`), the same trust model as a rootful Docker daemon - no service
to enable, no docker-group-equivalent to manage.

## How the dependency ordering works

`docker-compose.yml` uses `depends_on: condition: service_healthy` to make
sure e.g. `worker` doesn't start until `opensearch` and `tika` are actually
healthy, not just started. Quadlet has no direct equivalent of that compose
key, so each `.container` unit sets `Notify=healthy`: systemd considers the
unit "started" only once the container's own `HealthCmd` first succeeds.
Combined with plain `Requires=`/`After=` (in `ingest-api.container`,
`worker.container`, `scanner.container`, `search-api.container`, pointing at
their infra dependencies), this gives the same ordering guarantee - `systemctl
enable --now` on all nine services in one transaction lets systemd's own
dependency graph resolve correct startup order, exactly as compose's
`depends_on` graph did.

## Prerequisites

- Target host: Debian/Ubuntu or RHEL/Rocky/AlmaLinux, reachable over SSH with
  a sudo-capable user, at least ~6 GB RAM free and ~2 GB free disk (see the
  fulltext-poc README for why).
- Control node: Ansible >= 2.15, `rsync` installed locally.

```
cd ansible
ansible-galaxy collection install -r requirements.yml
```

## Setup

1. Inventory - copy the example and point it at your target host:
   ```
   cp inventory/hosts.example.ini inventory/hosts.ini
   $EDITOR inventory/hosts.ini
   ```
   If you're running this playbook directly on the machine it should deploy
   to (no separate control node), point it at itself instead of over SSH:
   ```ini
   [ftpoc]
   localhost ansible_connection=local
   ```
   `become: true` still applies for a local connection - if the user running
   `ansible-playbook` needs a sudo password, add `--ask-become-pass`.

2. Secrets - copy the example vault file, fill in real values, then encrypt it
   (it is gitignored either way, but encrypting means it's also safe to commit
   later if you want it version-controlled):
   ```
   cp group_vars/all/vault.yml.example group_vars/all/vault.yml
   $EDITOR group_vars/all/vault.yml
   ansible-vault encrypt group_vars/all/vault.yml
   ```
   Generated secrets must avoid characters that break `FTPOC_POSTGRES_DSN`'s
   `postgresql://user:pass@host/db` form - the template applies Jinja's
   `urlencode` to the user and password when building it, so any character is
   actually safe, but a plain alphanumeric-plus-`-_.` password is easier to
   read back out of a `journalctl`/`systemctl cat` dump if you ever need to.

3. Non-secret settings (`BIND_ADDR`, `PUBLIC_HOST`, deploy path, ...) live in
   `group_vars/all/vars.yml` - see the fulltext-poc README's "Accessing this
   from outside the host" section before changing `ftpoc_bind_addr` from the
   `127.0.0.1` default. If you use a Tailscale address, `tailscaled` must
   already be up and connected on the target before you run the playbook -
   and see the LXC/userspace-networking gotcha below before assuming that
   means the literal Tailscale IP.

## Run

```
ansible-playbook playbook.yml --ask-vault-pass
```

This builds the image (`podman build`), renders and starts all nine
`.container` units, and runs `bootstrap` (S3 bucket + OpenSearch index
template) as a one-off `podman run`. Safe to re-run - re-rendering identical
quadlet files is a no-op, `systemctl` treats an already-running unit as
already satisfied, and bootstrap itself is idempotent.

Smoke tests (`ftpoc_run_smoke_tests=true`) are **not supported** on this path
- `make probe`/`make hello` drive `podman-compose`, which would try to stand
up its own network/containers on the same ports this role's systemd units
already own. Setting that variable makes the playbook fail with an explicit
message rather than silently conflict. Verify manually instead, e.g.:
```
curl http://127.0.0.1:8081/healthz   # ingest-api
curl http://127.0.0.1:8080/healthz   # search-api
systemctl status 'rustfs.service' 'opensearch.service' 'tika.service' \
  'redis.service' 'postgres.service' 'ingest-api.service' 'worker.service' \
  'scanner.service' 'search-api.service'
```

## Known environment gotchas (found deploying into an LXC guest)

These aren't specific to any one host, but showed up together deploying into
an unprivileged Proxmox LXC container, so they're worth checking as a group
on any container-based (as opposed to full-VM) target:

- **No `/dev/net/tun`.** Podman's build-time networking (`podman build`,
  used for `RUN apt-get update` etc.) always goes through `pasta` for its
  outbound-only sandbox, regardless of whether the outer `podman` invocation
  is rootful - and pasta needs `/dev/net/tun`. The `ftpoc` role's image build
  task uses `podman build --network=host` specifically to avoid this (reuses
  the host netns directly, no tap device needed). The *runtime* containers
  are unaffected - Podman's normal rootful bridge networking (netavark) never
  touches `/dev/net/tun`.
- **`vm.max_map_count` may already be host-managed and read-only.** The
  `podman` role reads the current value first and only tries to raise it if
  it's actually below 262144 - inside an LXC guest this sysctl commonly
  belongs to the host and a write attempt fails with "permission denied"
  even as root, even when the host already set it high enough.
- **`RLIMIT_MEMLOCK` may be capped low and non-negotiable.** An LXC guest can
  cap this well below what `bootstrap.memory_lock: true` needs to lock a
  multi-GB JVM heap (8MB in the environment this was built against - check
  with `ulimit -l` inside the guest). `opensearch.container` leaves memory
  locking off for this reason; raise the container's memlock ceiling at the
  LXC/Proxmox host level first if you need it back.
- **Tailscale may run in `--tun=userspace-networking` mode** (same
  `/dev/net/tun` absence) - `systemctl status tailscaled` shows this. There
  is then no real interface carrying the Tailscale IP, so Podman can't bind
  to it directly; the `ftpoc` role's BIND_ADDR-presence check will correctly
  fail if you point `ftpoc_bind_addr` at it. In this mode tailscaled itself
  proxies inbound tailnet connections straight to `127.0.0.1:<same port>`
  (confirmed by watching its logs while connecting over SSH), so leaving
  `ftpoc_bind_addr` at the `127.0.0.1` default already gets tailnet-only
  reachability - binding to the literal interface address is only necessary
  on hosts where tailscaled has a real TUN device.

## Notes

- The project is synced with `rsync` (via `ansible.posix.synchronize`) from
  this checkout, not cloned from a git remote - there's no separate hosting
  step required.
- Re-running the playbook after editing local files re-syncs and re-deploys;
  `--delete` is used on the rsync, so files removed locally are removed on
  the target too.
- Secrets never appear as literal `Environment=` values in a `.container`
  file (and so never show up in `systemctl cat`, `ps aux`, or the systemd
  journal for that unit): `rustfs`/`opensearch`/`postgres` get them via
  `EnvironmentFile=.../ftpoc-secrets.env`, the four app containers via
  `EnvironmentFile=.../ftpoc-app.env`, both root-only-readable (`0600`). The
  opensearch healthcheck references `$OPENSEARCH_INITIAL_ADMIN_PASSWORD` from
  that file rather than embedding the password a second time.
- `docker-compose.yml`'s `opensearch-dashboards` (behind `--profile
  dashboards`, dev-only convenience) has no quadlet equivalent here - it's
  not part of this deployment path.
- `make reset` (deletes all data volumes) has no equivalent wired into this
  playbook by design - it's destructive and interactive. To tear down the
  quadlet deployment by hand on the target: `systemctl disable --now` the
  nine `.container` units, then `rm -rf /etc/containers/systemd/ftpoc` and
  `systemctl daemon-reload`; separately remove the `rustfs-data`/
  `opensearch-data`/`postgres-data` podman volumes if you want the data gone
  too.
