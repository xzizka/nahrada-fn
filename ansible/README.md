# ansible

Deploys the `fulltext-poc` docker-compose stack (see
[`../fulltext-poc/README.md`](../fulltext-poc/README.md)) to a remote Linux
host: installs Podman + podman-compose, sets the `vm.max_map_count`
OpenSearch requires, copies the project there, and brings the stack up.

Podman is daemonless and runs rootful here (via Ansible's `become: true`),
the same trust model as the rootful Docker daemon it replaces - no service to
enable, no docker-group-equivalent to manage.

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

3. Non-secret settings (`BIND_ADDR`, `PUBLIC_HOST`, deploy path, ...) live in
   `group_vars/all/vars.yml` - see the fulltext-poc README's "Accessing this
   from outside the host" section before changing `ftpoc_bind_addr` from the
   `127.0.0.1` default. If you use a Tailscale address, `tailscaled` must
   already be up and connected on the target before you run the playbook.

## Run

```
ansible-playbook playbook.yml --ask-vault-pass
```

This brings the stack up and runs `bootstrap` (S3 bucket + OpenSearch index
template). It's safe to re-run - `podman-compose up -d --build` and the
bootstrap step are both idempotent.

To also run the smoke tests (`make probe` and `make hello` on the target,
which uploads a generated test PDF into the real bucket/index - opt-in, not
run by default):

```
ansible-playbook playbook.yml --ask-vault-pass -e ftpoc_run_smoke_tests=true
```

## Notes

- The project is synced with `rsync` (via `ansible.posix.synchronize`) from
  this checkout, not cloned from a git remote - there's no separate hosting
  step required.
- Re-running the playbook after editing local files re-syncs and re-deploys;
  `--delete` is used on the rsync, so files removed locally are removed on the
  target too (the target's `.env` and `.git` are excluded from the sync and
  left alone).
- `make reset` (deletes all data volumes) is destructive and interactive by
  design - it is deliberately not wired into this playbook. Run it by hand on
  the target if you need it.
