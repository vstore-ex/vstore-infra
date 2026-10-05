# Commit messages

Use Node.js 22.12 or newer and run `npm ci` after cloning. This installs the
commitlint development tools and enables the Husky `commit-msg` hook.
For the Python backend and infrastructure repository, npm is only needed
for these development tools; it is not a runtime dependency.

Use Conventional Commits:

```text
feat(auth): add Google sign-in
fix: correct cart totals
chore: update dependencies
ci: validate commit messages
```

Format: `type(optional-scope): description`. Allowed types:
`build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`,
`style`, `test`. Use a lowercase type and scope, an imperative description,
no final period, and a header of at most 100 characters. Descriptions may
be written in English or Russian. Breaking changes use `!` after the type
or scope and/or a `BREAKING CHANGE:` footer.

Check a message or commit range manually:

```sh
printf '%s\n' 'feat: add checkout' | npm run commitlint
npm run commitlint -- --from origin/main --to HEAD --verbose
```

GitHub Actions checks every commit in a pull request and its title. Use the
same format for the PR title so the default squash commit is valid. Standard
merge and revert messages are ignored when checking commit history; PR titles
are always checked. Existing history on `main` is not checked retroactively.

To enforce this check before merge, add `Commitlint / Commitlint` as a required status
check in the GitHub branch protection rule or ruleset for `main`.

## Reusing the CI workflow

The implementation lives in `vstore-infra/.github/workflows/commitlint.yml`
and exposes `workflow_call`. The infra repository calls it with a relative
path from `commitlint-check.yml`; other repositories call it with a reviewed
infra commit SHA:

```yaml
name: Commitlint
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, edited, ready_for_review]
permissions:
  contents: read
jobs:
  commitlint:
    name: Commitlint
    uses: vstore-ex/vstore-infra/.github/workflows/commitlint.yml@<infra-commit-sha>
```

Callers need `package.json`, `package-lock.json`, the `commitlint` npm script,
`commitlint.config.mjs`, and `.github/commitlint-title.config.mjs`. The shared
workflow installs dependencies and checks the caller's history and PR title.
No inputs or secrets are required. Access to reusable workflows from private
repositories must be enabled for this organization, as with the shared build
and deploy workflows.

When updating the shared implementation, update the pinned infra SHA in the
caller workflows. A published SHA works before infra merges into main, so
application PR checks do not depend on merge order. The required status check
is now named `Commitlint / Commitlint`.
