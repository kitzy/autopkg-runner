# FleetGitOpsUploader AutoPkg Processor

Upload a freshly built installer to Fleet using the Software API, then create or update the corresponding GitOps YAML in your Fleet GitOps repo and open a pull request. This processor is designed for CI use in GitHub Actions and can also be run locally.

---

## Features

- Uploads a `.pkg` to Fleet for a specific team
- Creates a feature branch named `<software-title>-<version>`
- Writes or updates a per-title software YAML file in `lib/macos/software`
- Ensures the team YAML references that software file in `software.packages`
- Commits, pushes, and opens a GitHub pull request
- Idempotent where practical and fails loudly on API errors

---

## Requirements

- Python 3.9+
- AutoPkg
- `requests` and `PyYAML` Python packages
- `git` CLI on PATH
- GitHub token with `repo` scope if pushing to a different repo or if you need elevated permissions
- Fleet server and API token with rights to upload software for the target team

---

## Installation

1. Place `FleetGitOpsUploader.py` in an AutoPkg processor repo, for example:
   ```
   ~/Library/AutoPkg/RecipeRepos/com.github.yourorg.processors/FleetGitOpsUploader.py
   ```
2. Add your processor repo to AutoPkg search paths, for example:
   ```bash
   autopkg repo-add https://github.com/yourorg/autopkg-processors.git
   # or set AUTOPKG_RECIPE_SEARCH_DIRS to include the folder
   ```
3. Ensure dependencies are available:
   ```bash
   pip install --upgrade pip
   pip install requests PyYAML
   ```

---

## Inputs

All inputs can be provided as AutoPkg variables in your recipe or via `-k` overrides.

| Name | Required | Type | Description |
|------|----------|------|-------------|
| `pkg_path` | Yes | str | Path to the built `.pkg` file. |
| `software_title` | Yes | str | Human readable title, for example `Firefox`. |
| `version` | Yes | str | Version string used in YAML and branch name. |
| `platform` | No | str | Defaults to `darwin`. Accepts `darwin`, `windows`, `linux`, `ios`, `ipados`. |
| `fleet_api_base` | Yes | str | Fleet base URL, for example `https://fleet.example.com`. |
| `fleet_api_token` | Yes | str | Fleet API token. |
| `team_id` | Yes | int | Fleet Team ID for the upload. |
| `self_service` | No | bool | Make available in self service. Default `false`. |
| `automatic_install` | No | bool | On macOS, create automatic install policy. Default `false`. |
| `labels_include_any` | No | list[str] | Labels required for targeting. Only one of include or exclude may be set. |
| `labels_exclude_any` | No | list[str] | Labels to exclude from targeting. |
| `install_script` | No | str | Optional install script contents. |
| `uninstall_script` | No | str | Optional uninstall script contents. |
| `pre_install_query` | No | str | Optional osquery condition. |
| `post_install_script` | No | str | Optional post install script contents. |
| `git_repo_url` | Yes | str | HTTPS URL of your Fleet GitOps repo. |
| `git_base_branch` | No | str | Base branch to branch from and open PR to. Default `main`. |
| `git_author_name` | No | str | Commit author name. Default `autopkg-bot`. |
| `git_author_email` | No | str | Commit author email. Default `autopkg-bot@example.com`. |
| `team_yaml_path` | Yes | str | Path to team YAML in repo, for example `teams/workstations.yml`. |
| `software_dir` | No | str | Directory for per title YAML. Default `lib/macos/software`. |
| `package_yaml_suffix` | No | str | Suffix for per title YAML. Default `.package.yml`. |
| `team_yaml_package_path_prefix` | No | str | Path prefix used inside team YAML. Default `../lib/macos/software/`. |
| `github_repo` | Yes | str | `owner/repo` for PR creation. |
| `github_token` | No | str | GitHub token. If empty, uses `FLEET_GITOPS_GITHUB_TOKEN` env. When set, the processor rewrites the repo URL with the token so `git clone` and `git push` authenticate without prompts. |
| `pr_labels` | No | list[str] | Labels to set on the PR. |
| `software_slug` | No | str | Override slug used for file and branch names. Defaults to normalized `software_title`. |
| `branch_prefix` | No | str | Optional prefix for branch names, for example `autopkg`. |

---

## Outputs

| Name | Description |
|------|-------------|
| `fleet_title_id` | Fleet software title ID returned by the upload API. |
| `fleet_installer_id` | Fleet installer ID returned by the upload API. |
| `git_branch` | Created branch name. |
| `pull_request_url` | URL of the created or found pull request. |

---

## What It Writes

### Per title software YAML

Created or updated at `<repo>/<software_dir>/<software_slug><package_yaml_suffix>`, for example `lib/macos/software/firefox.package.yml`.

Structure:

```yaml
package:
  name: "Firefox"
  version: "129.0.2"
  platform: "darwin"
  hash_sha256: "abc123..."         # if provided by Fleet response
  self_service: true               # optional
  automatic_install: false         # optional macOS only
  labels_include_any:              # optional, only one of include or exclude
    - "Workstations"
  pre_install_query:               # optional
    query: "SELECT 1;"
  install_script:                  # optional
    contents: |
      #!/bin/bash
      echo "custom install"
  uninstall_script:                # optional
    contents: |
      #!/bin/bash
      echo "custom uninstall"
  post_install_script:             # optional
    contents: |
      #!/bin/bash
      echo "post"
```

This file represents a custom package uploaded to Fleet. The GitOps runner applies targeting and behavior from this file.

### Team YAML update

Ensures the team YAML includes a reference under `software.packages`. For example in `teams/workstations.yml`:

```yaml
software:
  packages:
    - path: ../lib/macos/software/firefox.package.yml
```

If the entry already exists, it does not duplicate it.

---

## Example AutoPkg Recipe Integration

Add the processor after your build step. Example excerpt:

```xml
<dict>
  <key>Process</key>
  <array>
    <!-- your existing build processors here -->
    <dict>
      <key>Processor</key>
      <string>com.github.yourorg.processors/FleetGitOpsUploader</string>
      <key>Arguments</key>
      <dict>
        <key>pkg_path</key><string>%pathname%</string>
        <key>software_title</key><string>%NAME%</string>
        <key>version</key><string>%version%</string>

        <key>fleet_api_base</key><string>https://fleet.example.com</string>
        <key>fleet_api_token</key><string>%FLEET_API_TOKEN%</string>
        <key>team_id</key><string>1</string>

        <key>self_service</key><true/>
        <key>labels_include_any</key>
        <array><string>Workstations</string></array>

        <key>git_repo_url</key><string>https://github.com/kitzy/fleet-gitops.git</string>
        <key>git_base_branch</key><string>main</string>
        <key>team_yaml_path</key><string>teams/workstations.yml</string>
        <key>software_dir</key><string>lib/macos/software</string>
        <key>team_yaml_package_path_prefix</key><string>../lib/macos/software/</string>

        <key>github_repo</key><string>kitzy/fleet-gitops</string>
        <key>github_token</key><string>%FLEET_GITOPS_GITHUB_TOKEN%</string>

        <key>branch_prefix</key><string>autopkg</string>
      </dict>
    </dict>
  </array>
</dict>
```

---

## GitHub Actions Example

This is a minimal job that installs dependencies, runs your recipes, and relies on injected secrets. Adapt paths and recipe names as needed.

```yaml
name: AutoPkg Fleet Upload

on:
  workflow_dispatch:
  schedule:
    - cron: "0 5 * * *"

jobs:
  run-autopkg:
    runs-on: macos-latest
    permissions:
      contents: write
      pull-requests: write

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Install Python deps
        run: |
          python3 -m pip install --upgrade pip
          python3 -m pip install requests PyYAML

      - name: Install AutoPkg
        run: |
          brew install autopkg || true
          autopkg version

      - name: Configure AutoPkg repos
        run: |
          autopkg repo-add https://github.com/autopkg/recipes.git || true
          autopkg repo-add https://github.com/yourorg/autopkg-processors.git || true

        - name: Run recipe
          env:
            FLEET_API_TOKEN: ${{ secrets.FLEET_API_TOKEN }}
            FLEET_GITOPS_GITHUB_TOKEN: ${{ secrets.PAT_GITHUB }}
          run: |
            autopkg run MyApp.autopkg.recipe \
              -k FLEET_API_TOKEN="$FLEET_API_TOKEN" \
              -k FLEET_GITOPS_GITHUB_TOKEN="$FLEET_GITOPS_GITHUB_TOKEN"
```

Notes:
- GitHub provides a `GITHUB_TOKEN` in Actions with repo scope. Use a PAT such as `${{ secrets.PAT_GITHUB }}` in `FLEET_GITOPS_GITHUB_TOKEN` if you open PRs across repos or orgs with stricter rules.
- Make sure the runner has access to push branches to the GitOps repo if it is a different repository than the Actions runner.

---

## Behavior and Idempotency

- If the team YAML already references the package file, the processor does not add a duplicate.
- If the per title YAML exists, it is updated in place.
- If there are no changes to commit, the job still pushes the branch and attempts to open a PR. If a PR already exists for the same `head` and `base`, the processor returns the existing PR URL.
- Only one of `labels_include_any` or `labels_exclude_any` may be set. The processor enforces this.

---

## Permissions

- Fleet API token must allow software uploads scoped to the target team.
- GitHub token must be able to push branches to the GitOps repo and open PRs.

---

## Troubleshooting

- **401 or 403 from Fleet**  
  Verify `fleet_api_base`, token value, and that the token has rights to upload for `team_id`.

- **400 from Fleet on upload**  
  Check that the file is a valid installer for the chosen platform. For `.pkg` you can omit custom scripts unless you need overrides.

- **Git failure on push**  
  Ensure the token used by Actions can push to the repo. Check branch protection rules and required status checks. If you block direct pushes to branches that do not exist yet, create an exception for the bot or switch to a service account PAT.

- **Labels error in GitOps sync**  
  Fleet GitOps requires that labels referenced in software YAML exist in your GitOps configuration. Add missing labels or remove them from the package YAML.

- **422 on PR creation**  
  This indicates a PR already exists. The processor searches for an open PR with the same head and base and returns that URL.

- **Nothing to commit**  
  If the YAML already matches the intended state, there will be no diff. This is normal. The processor still attempts to ensure a PR exists.

---

## Local Testing

You can run the processor outside Actions to validate behavior.

1. Create a temporary directory and clone your GitOps repo.
2. Export `FLEET_GITOPS_GITHUB_TOKEN` and `FLEET_API_TOKEN`.
3. Run your recipe with `autopkg run` and override variables with `-k` as needed.
4. Inspect the created branch and YAML changes before opening a PR.

---

## Security Notes

- Avoid echoing tokens in logs. The example Actions job relies on environment variables and never prints secrets.
- When a GitHub token is provided, the processor rewrites the Git repository URL with the token so that cloning and pushing use authenticated HTTPS URLs without prompts. `GIT_TERMINAL_PROMPT` is set to `0` to prevent interactive authentication.
- Consider scoping the GitHub token to the target repo only.
- Rotate the Fleet API token periodically.

---

## Conventions

- Branch name format: `<software-slug>-<version>` or `<branch_prefix>/<software-slug>-<version>` if `branch_prefix` is set.
- Per title YAML default directory: `lib/macos/software`. Override via `software_dir` if your repo uses a different layout.
- Team YAML reference uses a relative `path` entry. Adjust `team_yaml_package_path_prefix` if your repo structure differs.

---

## FAQ

**Q: Can this handle Windows or Linux packages?**  
Yes. Set `platform` accordingly and provide the appropriate installer. For non `.pkg` installers you will likely need `install_script` and `uninstall_script`.

**Q: Can it skip creating a PR and push directly to main?**  
Branch and PR is deliberate. If you want direct commits, you can set the base and head to the same branch and adjust the code. That is not recommended for audited changes.

**Q: What about labels or categories management?**  
This processor assumes labels already exist in GitOps. Managing label creation is out of scope to keep changes atomic and reviewable.

**Q: Can I change the YAML schema it writes?**  
Yes. Modify `_write_or_update_package_yaml` if your GitOps runner expects different keys or nesting. The defaults match a common custom package pattern.

---

## References

- AutoPkg Processors: https://github.com/autopkg/autopkg/wiki/Processors  
- Fleet GitOps YAML software docs: https://fleetdm.com/docs/configuration/yaml-files#software  
- Fleet example team YAML and per title examples:  
  - Team file pattern similar to `teams/workstations.yml`  
  - Per title example similar to `lib/macos/software/*.yml`

---

## License

MIT. Add your organization and year.
