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
use the same images and the same `fulltext-poc/postgres`, `tika`
config files, but do not share a network or ports - don't run both on the
same host at once (see Notes).

Podman itself is daemonless and runs rootful here (via Ansible's
`become: true`), the same trust model as a rootful Docker daemon - no service
to enable, no docker-group-equivalent to manage.

## How the dependency ordering works

`docker-compose.yml` uses `depends_on: condition: service_healthy` to make
sure e.g. `worker` doesn't start until `postgres` and `tika` are actually
healthy, not just started. Quadlet has no direct equivalent of that compose
key, so each `.container` unit sets `Notify=healthy`: systemd considers the
unit "started" only once the container's own `HealthCmd` first succeeds.
Combined with plain `Requires=`/`After=` (in `ingest-api.container`,
`worker.container`, `scanner.container`, `search-api.container`, pointing at
their infra dependencies), this gives the same ordering guarantee - `systemctl
enable --now` on all eight services in one transaction lets systemd's own
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

This builds both images (`podman build` - the app image and the custom
Postgres image that bundles the Czech hunspell dictionary, see the
fulltext-poc README's "PostgreSQL full-text search" section), renders and
starts all eight `.container` units, and runs `bootstrap` (creates the S3
bucket - the search schema itself is created by `postgres/*.sql` the first
time Postgres's data volume initializes) as a one-off `podman run`. Safe to
re-run - re-rendering identical quadlet files is a no-op and bootstrap
itself is idempotent. `postgres`/`ingest-api`/`worker`/`scanner`/`search-api`
are explicitly `state: restarted` on every run rather than just `started`,
since their images get rebuilt every run under the same tag and Podman
containers pin to the image ID at creation time, not the tag - `started`
alone would silently keep the pre-rebuild container running. `rustfs`/
`tika`/`redis` (stock images, never rebuilt) stay `started` and are
untouched if already up.

Smoke tests are opt-in (`ftpoc_run_smoke_tests=true`):
```
ansible-playbook playbook.yml --ask-vault-pass -e ftpoc_run_smoke_tests=true
```
This runs the Phase 1.5 S3-compatibility probe and the Phase 5 end-to-end
hello-world test from the fulltext-poc README, but **not** via `make
probe`/`make hello` - those drive `podman-compose`, which would try to stand
up its own network/containers on the same ports this role's systemd units
already own. Instead it's `fulltext-poc/scripts/smoke-test-podman.sh` (a
podman-native port of `smoke-test.sh`: plain `podman run`/`exec`/`logs`
against the same image, the `ftpoc` network, and the running `postgres`/
`worker` containers, reading `ftpoc-secrets.env`/`ftpoc-app.env` instead of a
compose-style `.env`) plus an inline probe task mirroring the Makefile's
`probe` target - except that task's final "is the presigned URL actually
reachable" check runs `delegate_to: localhost` (from the control node, not
the target) for the reason in the gotchas section below. `smoke-test-podman.sh`'s
own equivalent check (Assert E, `download_url`) has no such delegation - it
runs entirely on the target, so on a host whose tailscaled is in
`--tun=userspace-networking` mode, Assert E can fail even though the stack is
genuinely fine, because the target can't curl its own public tailnet hostname
in that mode (see gotchas below). Treat an Assert-E-only failure on such a
host as inconclusive, not as evidence of a real problem, unless you also
check reachability from a separate peer.

Uploads a generated test PDF into the real bucket/index, so
it's still opt-in, not run on every deploy. Needs `jq` on the target (the
`podman` role installs it).

**Assert D is only meaningful on a fresh run.** It re-uploads the same
`hello-smlouva.pdf` and expects a "Sidecar cache hit" log line from `worker`
proving Tika was skipped the second time. `hello-smlouva.pdf`'s content (and
so its SHA-256) never changes, so on a *repeated* run against the same
still-populated `postgres` data, `ingest-api` already recognizes
the hash as fully indexed and doesn't re-enqueue a worker job at all - there's
nothing left for the worker to log a cache hit about, and this half of Assert
D fails even though nothing is actually wrong (confirmed by checking `podman
logs worker` directly: the cache-hit line is there from whichever run first
saw this exact file). Re-running smoke tests against a target that already
has hello-world data from a prior run will reproduce this; it's not a
Podman-specific issue, just a property of the test relying on the document
being new.

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
- **The same userspace-networking mode breaks self-curl of the public
  hostname.** The inbound proxy-to-127.0.0.1 trick above only helps
  connections arriving *from* the tailnet. A process on the target trying to
  curl its own `ftpoc_public_host` (e.g. inside `smoke-test-podman.sh`, or
  the S3 probe's presigned-URL check) has no real interface to route an
  *outbound* connection to its own Tailscale IP through, and fails to
  connect - not a sign the stack is broken, just that this one host can't
  reach itself that way. The probe task in this role works around it by
  curling the presigned URL `delegate_to: localhost` (the control node,
  genuinely a separate peer) instead of on the target.

## Notes

- The project is synced with `rsync` (via `ansible.posix.synchronize`) from
  this checkout, not cloned from a git remote - there's no separate hosting
  step required.
- Re-running the playbook after editing local files re-syncs and re-deploys;
  `--delete` is used on the rsync, so files removed locally are removed on
  the target too.
- Secrets never appear as literal `Environment=` values in a `.container`
  file (and so never show up in `systemctl cat`, `ps aux`, or the systemd
  journal for that unit): `rustfs`/`postgres` get them via
  `EnvironmentFile=.../ftpoc-secrets.env`, the four app containers via
  `EnvironmentFile=.../ftpoc-app.env`, both root-only-readable (`0600`).
- `make reset` (deletes all data volumes) has no equivalent wired into this
  playbook by design - it's destructive and interactive. To tear down the
  quadlet deployment by hand on the target: `systemctl disable --now` the
  eight `.container` units, then `rm -rf /etc/containers/systemd/ftpoc` and
  `systemctl daemon-reload`; separately remove the `rustfs-data`/
  `postgres-data` podman volumes if you want the data gone too.
