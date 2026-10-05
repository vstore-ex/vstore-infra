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

To enforce this check before merge, add `Commitlint` as a required status
check in the GitHub branch protection rule or ruleset for `main`.
