# Application release state

## State and commands

The deployment directory contains a generated `docker-compose.override.yml`.
It is JSON (valid YAML), with literal images for backend, worker, frontend and
admin, plus previous successful images in `x-vstore-release`. Current and previous
images are written in one atomic rename after file fsync; the directory is also
fsynced. Permissions are 0600. The file contains image references only, never
environment secrets. Keep it outside Git and preserve it across config updates.

All deployment writers share `.deploy.lock`. The workflows inherit descriptor
200; the manual wrapper locks the same file until a mutating command exits, with
a 300-second acquisition timeout. Read-only commands such as `logs -f` do not
hold the lock. Use:

```bash
bash scripts/compose.sh up -d --wait
bash scripts/compose.sh ps
bash scripts/compose.sh logs --tail=50 backend
bash scripts/compose.sh verify
bash scripts/compose.sh rollback FRONTEND_IMAGE
# Other rollback groups: BACKEND_IMAGE (backend and worker), ADMIN_IMAGE.
```

Plain `docker compose up` also reads the successful images automatically, including
in a new SSH session with stale `.env` or exported image variables. Use the wrapper
for manual operations because plain Compose does not acquire the shared lock.
Do not use `-f`, `COMPOSE_FILE`, another override, or another working directory to
bypass the generated state. `release_compose` deliberately uses only the base file
with the loaded images when testing a candidate or applying infrastructure.

Bootstrap requires healthy backend, frontend and admin containers, a running
worker, and unambiguous registry digests. Backend and worker must use the same
image. The verifier inspects the container image ID, then the image RepoDigests.
The existing digest must belong to the expected image. Bootstrap never replaces
an existing state file. All image groups must exist before the first app deploy.

Application CD publishes only after `up --wait`, the caller's HTTP gate and
verification of every running application image. Failed health checks restore
the current successful image; the unsuccessful candidate never enters history.
A repeated deployment of the same digest keeps the previous image. Intentional
rollback checks HTTP and container health and swaps current/previous for the
selected group only. Backend rollback does not downgrade database schema.

An interruption before publication leaves the successful state unchanged. Candidate
containers may remain until recovery; the next infrastructure/manual `up` restores
the recorded successful release. If app CD detects this drift first, it refuses
to deploy. Run the wrapper's `up -d --wait`, then `verify`, before retrying app CD.
An interruption after atomic publication retains a fully verified release.

## First adoption on an existing host

1. Pause app deployments while adopting. Confirm Python 3, Bash, util-linux flock,
   Docker and Compose v2 are present. Review the helper and workflow changes.
2. Make the new scripts available in the existing deployment clone. Before any
   stack convergence, run `bash scripts/compose.sh bootstrap`. This discovers
   the actual healthy running images without restarting services, pulling tags,
   reading secrets into the state, or printing the resolved Compose environment.
3. Run `bash scripts/compose.sh verify`. Check the image-only override and confirm
   it reflects the intended running release, including backend/worker parity.
4. Apply the reviewed infrastructure change first, then resume app deployments.
   Infra CD can also bootstrap automatically if the state is absent, before any
   build/up command. Adoption fails closed if running images cannot be verified.

Keep the host clone compatible with the updated reusable app workflow: its helper
scripts must be installed before that workflow runs. An old clone without helpers
fails rather than silently deploying with stale image defaults.

For a brand-new installation, configure secrets and review all three immutable
`*_IMAGE` references in `.env`, pull and start the base stack with `docker compose
up -d --wait`, then bootstrap. The template's `:latest` values are for local
development only. No previous target exists until the first successful upgrade.

## Recovery

Do not adopt an unhealthy or unintended running candidate. For a valid state,
recover interrupted deployment by applying the recorded images through the wrapper
and verifying them. No state editing or image-tag guessing is needed.

If the state is missing, bootstrap only after independently confirming the intended
healthy running release. If corrupt, restore a known-good saved override and run
`verify`; then converge using the wrapper if the live containers differ.

If no valid copy exists, an operator can re-adopt independently verified running
images under the shared lock. Pause deployment automation first. Preserve the
damaged file for investigation. This resets previous history, so retain the backup
for recovering earlier image references:

```bash
cd ~/vstore
exec 200>.deploy.lock
flock -w 300 200
mkdir -p backups
saved="backups/release-state-$(date +%Y%m%d_%H%M%S).json"
mv docker-compose.override.yml "$saved"
if ! python3 scripts/release-state.py --lock-fd 200 bootstrap; then
  mv "$saved" docker-compose.override.yml
fi
flock -u 200
exec 200>&-
```

No restore or schema downgrade is automatic. Check backend compatibility with the
current database schema before selecting a previous backend image.

## Verification before rollout

```bash
python3 -m unittest discover -s tests -p 'test_release_state.py' -v
python3 tests/smoke_release_state.py
docker compose --env-file .env.example config --quiet
bash tests/smoke-proxy.sh
```

The Docker smoke test creates a uniquely named disposable stack on loopback with
two registry-pinned Nginx images. It executes the actual app deployment script,
tests a new SSH-like session with stale pins, intentional rollback, failed HTTP
verification and interrupted-candidate recovery. Its Docker shim suppresses the
workflow's global image prune; cleanup removes only its own test project.
These checks do not deploy production or validate database migrations.
