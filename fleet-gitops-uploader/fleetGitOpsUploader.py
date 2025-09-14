# -*- coding: utf-8 -*-
#
# FleetGitOpsUploader AutoPkg Processor
#
# Uploads a package to Fleet and updates a Fleet GitOps repo with software YAML,
# commits on a new branch, and opens a PR.
#
# Requires: requests, PyYAML; git CLI available.
#
from __future__ import annotations

import os
import re
import json
import shutil
import hashlib
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML")

try:
    import requests
except ImportError:
    raise ImportError("requests is required. Install with: pip install requests")

from autopkglib import Processor, ProcessorError


class FleetGitOpsUploader(Processor):
    """Upload AutoPkg-built installer to Fleet and update GitOps YAML in a PR."""

    description = __doc__
    input_variables = {
        # --- Required basics ---
        "pkg_path": {
            "required": True,
            "description": "Path to the built .pkg from AutoPkg.",
        },
        "software_title": {
            "required": True,
            "description": "Human-readable software title, e.g., 'Firefox.app'.",
        },
        "version": {
            "required": True,
            "description": "Version string to use for branch naming and YAML.",
        },
        "platform": {
            "required": False,
            "default": "darwin",
            "description": "Platform hint for YAML (darwin|windows|linux|ios|ipados).",
        },

        # --- Fleet API ---
        "fleet_api_base": {
            "required": True,
            "description": "Fleet base URL, e.g., https://fleet.example.com",
        },
        "fleet_api_token": {
            "required": True,
            "description": "Fleet API token (Bearer).",
        },
        "team_id": {
            "required": True,
            "description": "Fleet team ID to attach the uploaded package to.",
        },

        # Optional Fleet install flags
        "self_service": {
            "required": False,
            "default": False,
            "description": "Whether the package is self-service.",
        },
        "automatic_install": {
            "required": False,
            "default": False,
            "description": "macOS-only: create automatic install policy when hosts lack software.",
        },
        "labels_include_any": {
            "required": False,
            "default": [],
            "description": "List of label names to include.",
        },
        "labels_exclude_any": {
            "required": False,
            "default": [],
            "description": "List of label names to exclude.",
        },
        "install_script": {
            "required": False,
            "default": "",
            "description": "Custom install script body (string).",
        },
        "uninstall_script": {
            "required": False,
            "default": "",
            "description": "Custom uninstall script body (string).",
        },
        "pre_install_query": {
            "required": False,
            "default": "",
            "description": "Pre-install osquery SQL condition.",
        },
        "post_install_script": {
            "required": False,
            "default": "",
            "description": "Post-install script body (string).",
        },

        # --- Git / GitHub ---
        "git_repo_url": {
            "required": True,
            "description": "Git URL of your Fleet GitOps repo (HTTPS).",
        },
        "git_base_branch": {
            "required": False,
            "default": "main",
            "description": "The base branch to branch from and target in PRs.",
        },
        "git_author_name": {
            "required": False,
            "default": "autopkg-bot",
            "description": "Commit author name.",
        },
        "git_author_email": {
            "required": False,
            "default": "autopkg-bot@example.com",
            "description": "Commit author email.",
        },

        # Pathing inside repo
        "team_yaml_path": {
            "required": True,
            "description": "Path to the team YAML (e.g., 'teams/workstations.yml').",
        },
        "software_dir": {
            "required": False,
            "default": "lib/macos/software",
            "description": "Directory for per-software YAML files relative to repo root.",
        },
        "package_yaml_suffix": {
            "required": False,
            "default": ".package.yml",
            "description": "Suffix for package YAML files.",
        },
        "team_yaml_package_path_prefix": {
            "required": False,
            "default": "../lib/macos/software/",
            "description": "Prefix used in team YAML when referencing package YAML paths.",
        },

        # GitHub PR
        "github_repo": {
            "required": True,
            "description": "GitHub repo in 'owner/repo' form for PR creation.",
        },
        "github_token": {
            "required": False,
            "default": "",
            "description": "GitHub token. If empty, will use GITHUB_TOKEN env.",
        },
        "pr_labels": {
            "required": False,
            "default": [],
            "description": "List of GitHub PR labels to apply.",
        },

        # Slug / naming
        "software_slug": {
            "required": False,
            "default": "",
            "description": "Optional file slug. Defaults to normalized software_title.",
        },
        "branch_prefix": {
            "required": False,
            "default": "",
            "description": "Optional prefix for branch names.",
        },
    }

    output_variables = {
        "fleet_title_id": {"description": "Created/updated Fleet software title ID."},
        "fleet_installer_id": {"description": "Installer ID in Fleet."},
        "git_branch": {"description": "The branch name created for the PR."},
        "pull_request_url": {"description": "The created PR URL."},
    }

    def _derive_github_repo(self, git_repo_url: str) -> str:
        """
        Derive 'owner/repo' from a git repo URL.
        Supports https://github.com/owner/repo(.git)? and git@github.com:owner/repo(.git)?
        Returns empty string if it can't be derived.
        """
        if not git_repo_url:
            return ""
        s = git_repo_url.strip()
        # SSH: git@github.com:owner/repo.git
        if s.startswith("git@"):
            try:
                path_part = s.split(":", 1)[1]
            except IndexError:
                return ""
            if path_part.endswith(".git"):
                path_part = path_part[:-4]
            return path_part.strip("/") if path_part.count("/") == 1 else ""
        # HTTPS: https://github.com/owner/repo or with .git
        if s.startswith("http://") or s.startswith("https://"):
            if "github.com/" not in s:
                return ""
            after_host = s.split("github.com/", 1)[1]
            if after_host.endswith(".git"):
                after_host = after_host[:-4]
            after_host = after_host.strip("/")
            return after_host if after_host.count("/") == 1 else ""
        # Fallback: already owner/repo
        if s.count("/") == 1 and ":" not in s and " " not in s:
            return s
        return ""

    def main(self):
        # Inputs
        pkg_path = Path(self.env["pkg_path"]).expanduser().resolve()
        if not pkg_path.is_file():
            raise ProcessorError(f"pkg_path not found: {pkg_path}")

        software_title = self.env["software_title"].strip()
        version = self.env["version"].strip()
        platform = self.env.get("platform", "darwin")

        fleet_api_base = self.env["fleet_api_base"].rstrip("/")
        fleet_token = self.env["fleet_api_token"]
        team_id = int(self.env["team_id"])

        # Fleet options
        self_service = bool(self.env.get("self_service", False))
        automatic_install = bool(self.env.get("automatic_install", False))
        labels_include_any = list(self.env.get("labels_include_any", []))
        labels_exclude_any = list(self.env.get("labels_exclude_any", []))
        install_script = self.env.get("install_script", "")
        uninstall_script = self.env.get("uninstall_script", "")
        pre_install_query = self.env.get("pre_install_query", "")
        post_install_script = self.env.get("post_install_script", "")

        # Git / GitHub
        git_repo_url = self.env["git_repo_url"]
        git_base_branch = self.env.get("git_base_branch", "main")
        author_name = self.env.get("git_author_name", "autopkg-bot")
        author_email = self.env.get("git_author_email", "autopkg-bot@example.com")
        team_yaml_path = self.env["team_yaml_path"]
        software_dir = self.env.get("software_dir", "lib/macos/software")
        package_yaml_suffix = self.env.get("package_yaml_suffix", ".package.yml")
        team_yaml_prefix = self.env.get("team_yaml_package_path_prefix", "../lib/macos/software/")
        github_repo = self.env["github_repo"]
        github_token = self.env.get("github_token") or os.environ.get("GITHUB_TOKEN", "")
        if not github_token:
            raise ProcessorError("GitHub token not provided (github_token or GITHUB_TOKEN env).")
        pr_labels = list(self.env.get("pr_labels", []))
        branch_prefix = self.env.get("branch_prefix", "").strip()

        # Slug
        software_slug = self.env.get("software_slug", "").strip() or self._slugify(software_title)

        # Upload to Fleet
        self.output("Uploading package to Fleet…")
        upload_info = self._fleet_upload_package(
            fleet_api_base,
            fleet_token,
            pkg_path,
            team_id,
            self_service,
            automatic_install,
            labels_include_any,
            labels_exclude_any,
            install_script,
            uninstall_script,
            pre_install_query,
            post_install_script,
        )

        title_id = upload_info["software_package"].get("title_id")
        installer_id = upload_info["software_package"].get("installer_id")
        hash_sha256 = upload_info["software_package"].get("hash_sha256")
        returned_version = upload_info["software_package"].get("version") or version

        # Prepare repo in a temp dir
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            self._git(["clone", "--origin", "origin", "--branch", git_base_branch, git_repo_url, str(repo_dir)])

            # Create branch
            branch_name = f"{software_slug}-{returned_version}"
            if branch_prefix:
                branch_name = f"{branch_prefix.rstrip('/')}/{branch_name}"
            self._git(["checkout", "-b", branch_name], cwd=repo_dir)

            # Ensure software YAML exists/updated
            sw_dir = repo_dir / software_dir
            sw_dir.mkdir(parents=True, exist_ok=True)
            pkg_yaml_path = sw_dir / f"{software_slug}{package_yaml_suffix}"

            self.output(f"Updating package YAML: {pkg_yaml_path}")
            self._write_or_update_package_yaml(
                pkg_yaml_path=pkg_yaml_path,
                software_title=software_title,
                version=returned_version,
                platform=platform,
                fleet_title_id=title_id,
                fleet_installer_id=installer_id,
                hash_sha256=hash_sha256,
                self_service=self_service,
                labels_include_any=labels_include_any,
                labels_exclude_any=labels_exclude_any,
                automatic_install=automatic_install,
                pre_install_query=pre_install_query,
                install_script=install_script,
                uninstall_script=uninstall_script,
                post_install_script=post_install_script,
            )

            # Ensure team YAML references the package YAML path
            team_yaml_abs = repo_dir / team_yaml_path
            if not team_yaml_abs.exists():
                raise ProcessorError(f"team_yaml_path not found: {team_yaml_abs}")

            self.output(f"Ensuring team YAML has software entry: {team_yaml_abs}")
            team_yaml_modified = self._ensure_team_yaml_has_package(
                team_yaml_abs,
                ref_path=f"{team_yaml_prefix}{pkg_yaml_path.name}",
            )

            # Commit if changed
            self._git(["config", "user.name", author_name], cwd=repo_dir)
            self._git(["config", "user.email", author_email], cwd=repo_dir)
            # Stage files
            self._git(["add", str(pkg_yaml_path)], cwd=repo_dir)
            if team_yaml_modified:
                self._git(["add", str(team_yaml_abs)], cwd=repo_dir)

            # If nothing to commit, we still push branch (idempotent PRs are okay)
            commit_msg = f"feat(software): {software_title} {returned_version} [{software_slug}]"
            self._git_safe_commit(commit_msg, cwd=repo_dir)

            # Push
            self._git(["push", "--set-upstream", "origin", branch_name], cwd=repo_dir)

        # Open PR
        pr_url = self._open_pull_request(
            github_repo=github_repo,
            github_token=github_token,
            head=branch_name,
            base=git_base_branch,
            title=f"{software_title} {returned_version}",
            body=self._pr_body(software_title, returned_version, software_slug, title_id, installer_id),
            labels=pr_labels,
        )

        # Outputs
        self.env["fleet_title_id"] = title_id
        self.env["fleet_installer_id"] = installer_id
        self.env["git_branch"] = branch_name
        self.env["pull_request_url"] = pr_url

        self.output(f"PR opened: {pr_url}")

    # ------------------- helpers -------------------

    def _slugify(self, text: str) -> str:
        # keep it boring; Git path and branch friendly
        s = text.lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s or "software"

    def _git(self, args, cwd=None):
        proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ProcessorError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _git_safe_commit(self, message: str, cwd=None):
        # commit only if staged changes exist
        status = self._git(["status", "--porcelain"], cwd=cwd)
        if status:
            self._git(["commit", "-m", message], cwd=cwd)

    def _fleet_upload_package(
        self,
        base_url,
        token,
        pkg_path: Path,
        team_id: int,
        self_service: bool,
        automatic_install: bool,
        labels_include_any: list[str],
        labels_exclude_any: list[str],
        install_script: str,
        uninstall_script: str,
        pre_install_query: str,
        post_install_script: str,
    ) -> dict:
        url = f"{base_url}/api/v1/fleet/software/package"
        headers = {"Authorization": f"Bearer {token}"}

        form = {
            "team_id": str(team_id),
            "self_service": json.dumps(bool(self_service)).lower(),
        }
        # API rules: only one of include/exclude
        if labels_include_any and labels_exclude_any:
            raise ProcessorError("Only one of labels_include_any or labels_exclude_any may be specified.")

        if labels_include_any:
            # Note: API accepts labels_include_any as array form field; send multiple entries
            # requests can send tuples for repeating fields
            pass
        if labels_exclude_any:
            pass

        files = [
            ("software", (pkg_path.name, open(pkg_path, "rb"), "application/octet-stream")),
        ]

        data = []
        for k, v in form.items():
            data.append((k, v))
        if install_script:
            data.append(("install_script", install_script))
        if uninstall_script:
            data.append(("uninstall_script", uninstall_script))
        if pre_install_query:
            data.append(("pre_install_query", pre_install_query))
        if post_install_script:
            data.append(("post_install_script", post_install_script))
        if automatic_install:
            data.append(("automatic_install", "true"))

        for label in labels_include_any:
            data.append(("labels_include_any", label))
        for label in labels_exclude_any:
            data.append(("labels_exclude_any", label))

        resp = requests.post(url, headers=headers, files=files, data=data, timeout=900)
        if resp.status_code != 200:
            raise ProcessorError(f"Fleet upload failed: {resp.status_code} {resp.text}")
        return resp.json()

    def _read_yaml(self, path: Path) -> dict:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _write_yaml(self, path: Path, data: dict):
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)

    def _write_or_update_package_yaml(
        self,
        pkg_yaml_path: Path,
        software_title: str,
        version: str,
        platform: str,
        fleet_title_id: int,
        fleet_installer_id: int,
        hash_sha256: str | None,
        self_service: bool,
        labels_include_any: list[str],
        labels_exclude_any: list[str],
        automatic_install: bool,
        pre_install_query: str,
        install_script: str,
        uninstall_script: str,
        post_install_script: str,
    ):
        """
        We store the package metadata in a YAML the GitOps worker can apply.
        Follows Fleet's GitOps 'packages' schema conventions and puts targeting
        keys (labels/categories/self_service) where Fleet expects them for custom packages.
        """
        data = self._read_yaml(pkg_yaml_path)

        # Compose content. If GitOps runner expects `path:`, you’ll reference this file
        # from team YAML. Inside the file we set the package fields Fleet understands.
        pkg_block = {
            "name": software_title,
            "version": str(version),
            "platform": platform,
        }

        # Include hash if Fleet returned one (helps dedupe per docs)
        if hash_sha256:
            pkg_block["hash_sha256"] = hash_sha256

        # Optional targeting and behavior
        if self_service:
            pkg_block["self_service"] = True
        if labels_include_any:
            pkg_block["labels_include_any"] = list(labels_include_any)
        if labels_exclude_any:
            pkg_block["labels_exclude_any"] = list(labels_exclude_any)
        if automatic_install and platform in ("darwin", "macos"):
            pkg_block["automatic_install"] = True
        if pre_install_query:
            pkg_block["pre_install_query"] = {"query": pre_install_query}
        if install_script:
            pkg_block["install_script"] = {"contents": install_script}
        if uninstall_script:
            pkg_block["uninstall_script"] = {"contents": uninstall_script}
        if post_install_script:
            pkg_block["post_install_script"] = {"contents": post_install_script}

        # We store under top-level `package` to mirror common patterns.
        data = {"package": pkg_block}

        self._write_yaml(pkg_yaml_path, data)

    def _ensure_team_yaml_has_package(self, team_yaml_path: Path, ref_path: str) -> bool:
        """Ensure team YAML includes software.packages with the given ref_path."""
        y = self._read_yaml(team_yaml_path)
        if "software" not in y or y["software"] is None:
            y["software"] = {}
        if "packages" not in y["software"] or y["software"]["packages"] is None:
            y["software"]["packages"] = []

        pkgs = y["software"]["packages"]

        # Normalize existing paths into comparable strings
        def pkg_ref(p):
            # allow either dict {"path": "..."} or raw string
            if isinstance(p, str):
                return p
            if isinstance(p, dict) and "path" in p:
                return p["path"]
            return ""

        existing = [pkg_ref(p) for p in pkgs]
        if ref_path not in existing:
            pkgs.append({"path": ref_path})
            self._write_yaml(team_yaml_path, y)
            return True
        return False

    def _open_pull_request(
        self,
        github_repo: str,
        github_token: str,
        head: str,
        base: str,
        title: str,
        body: str,
        labels: list[str],
    ) -> str:
        api = f"https://api.github.com/repos/{github_repo}/pulls"
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
        }
        payload = {"title": title, "head": head, "base": base, "body": body, "maintainer_can_modify": True}
        r = requests.post(api, headers=headers, json=payload, timeout=60)
        if r.status_code not in (201, 422):
            # 422 if PR already exists; try to discover URL
            raise ProcessorError(f"PR creation failed: {r.status_code} {r.text}")
        pr = r.json()
        pr_url = pr.get("html_url") or self._find_existing_pr_url(github_repo, github_token, head, base)

        if labels and pr_url and "number" in pr:
            issue_api = f"https://api.github.com/repos/{github_repo}/issues/{pr['number']}/labels"
            requests.post(issue_api, headers=headers, json={"labels": labels}, timeout=30)

        return pr_url or ""

    def _find_existing_pr_url(self, repo: str, token: str, head: str, base: str) -> str:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        q = f"repo:{repo} is:pr is:open head:{head} base:{base}"
        r = requests.get(f"https://api.github.com/search/issues?q={requests.utils.quote(q)}", headers=headers, timeout=30)
        if r.ok and r.json().get("items"):
            return r.json()["items"][0]["html_url"]
        return ""


