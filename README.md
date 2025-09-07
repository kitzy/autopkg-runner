# autopkg-runner

This repository contains a GitHub Actions workflow and helper scripts to run [AutoPkg](https://autopkg.github.io/autopkg/) recipes, upload packages to [Fleet](https://fleetdm.com/), and open pull requests against a separate GitOps repository.

AutoPkg is invoked differently depending on the environment:

- Locally, call the `autopkg` CLI directly (for example when installed via Homebrew).
- In CI, the workflow uses `python autopkg/Code/autopkg` from the setup action and exposes it via the `AUTOPKG_CMD` environment variable so scripts behave the same.

Run AutoPkg as an unprivileged user. Use [`scripts/run_autopkg.sh`](scripts/run_autopkg.sh) to process recipe lists from an overrides repository. For example, after cloning the overrides repository:

```bash
git clone https://github.com/<owner>/<overrides-repo>.git overrides
sudo -u autopkg ./scripts/run_autopkg.sh
```

## Required secrets

Set these secrets in the repository settings so the workflow can access external services:

- `FLEET_URL` – Fleet base URL.
- `FLEET_API_TOKEN` – API token with global write access.
- `GITOPS_REPO` – GitHub `<owner>/<repo>` for the GitOps repository.
- `GITOPS_DEFAULT_BRANCH` – Default branch name of the GitOps repo (e.g. `main`).
- `OVERRIDES_REPO` – GitHub `<owner>/<repo>` containing recipe lists and overrides.
- `OVERRIDES_REF` – Branch or tag in the overrides repo to check out.
- `GITOPS_PUSH_TOKEN` – Fine-grained PAT with Contents and Pull requests write access to the GitOps repo.

## Configuration

- `config/recipe-map.yml` – Optional per-recipe overrides for Fleet team IDs and self-service flags.
- The overrides repo must include `recipe-lists/darwin-prod.txt` with one AutoPkg recipe per line and any local overrides under `overrides/`.

## One-time setup

1. Ensure the GitOps and overrides repositories exist and the PAT has write permissions.
2. Populate `config/recipe-map.yml` as needed.
3. Add the secrets listed above to this repository.

The workflow defined in `.github/workflows/autopkg.yml` runs on a schedule at 06:00 UTC and can also be triggered manually. It builds the listed recipes, uploads the resulting packages to Fleet, and opens auto-merging pull requests in the GitOps repository.
