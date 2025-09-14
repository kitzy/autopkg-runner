# -*- coding: utf-8 -*-
#
# FleetGitOpsUploader AutoPkg Processor
#
# Uploads a package to Fleet and updates a Fleet GitOps repo with software YAML,
# commits on a new branch, and opens a PR.
#
# Requires: PyYAML and git CLI available.
#

from __future__ import annotations

import os
import re
import json
import shutil
import hashlib
import io
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML")

import urllib.request
import urllib.parse
import urllib.error

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
            "required": False,
            "description": (
                "GitHub repo in 'owner/repo' form for PR creation. "
                "If omitted, derived from git_repo_url."
            ),
        },
        "github_token": {
            "required": False,
            "default": "",
            "description": "GitHub token. If empty, will use FLEET_GITOPS_GITHUB_TOKEN env.",
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
        github_repo = self.env.get("github_repo") or self._derive_github_repo(git_repo_url)
        if not github_repo:
            raise ProcessorError("github_repo not provided and could not derive from git_repo_url")
        github_token = self.env.get("github_token") or os.environ.get("FLEET_GITOPS_GITHUB_TOKEN", "")
        if not github_token:
            raise ProcessorError(
                "GitHub token not provided (github_token or FLEET_GITOPS_GITHUB_TOKEN env)."
            )
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
            software_title,
            version,
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
            clone_url = git_repo_url
            if github_token and git_repo_url.startswith("https://"):
                parsed = urllib.parse.urlparse(git_repo_url)
                if "@" not in parsed.netloc:
                    netloc = f"{urllib.parse.quote(github_token, safe='')}@{parsed.netloc}"
                    clone_url = urllib.parse.urlunparse(parsed._replace(netloc=netloc))
            self._git(["clone", "--origin", "origin", "--branch", git_base_branch, clone_url, str(repo_dir)])

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
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise ProcessorError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _git_safe_commit(self, message: str, cwd=None):
        # commit only if staged changes exist
        status = self._git(["status", "--porcelain"], cwd=cwd)
        if status:
            self._git(["commit", "-m", message], cwd=cwd)

    @staticmethod
    def _pr_body(
        software_title: str,
        version: str,
        slug: str,
        title_id: int,
        installer_id: int,
    ) -> str:
        """Compose a concise markdown summary for the PR body.

        Examples
        --------
        >>> FleetGitOpsUploader._pr_body("Firefox", "1.2.3", "Mozilla/firefox", 42, 99)
        '### Firefox 1.2.3\n\n- Fleet title ID: `42`\n- Fleet installer ID: `99`\n- Software slug: `Mozilla/firefox`\n- [Changelog](https://github.com/Mozilla/firefox/releases/tag/1.2.3)'
        """

        lines = [
            f"### {software_title} {version}",
            "",
            f"- Fleet title ID: `{title_id}`",
            f"- Fleet installer ID: `{installer_id}`",
        ]

        if slug:
            lines.append(f"- Software slug: `{slug}`")
            if "/" in slug:
                changelog = f"https://github.com/{slug}/releases/tag/{version}"
                lines.append(f"- [Changelog]({changelog})")

        return "\n".join(lines)

    def _fleet_upload_package(
        self,
        base_url,
        token,
        pkg_path: Path,
        software_title: str,
        version: str,
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
        # API rules: only one of include/exclude
        if labels_include_any and labels_exclude_any:
            raise ProcessorError("Only one of labels_include_any or labels_exclude_any may be specified.")

        boundary = "----FleetUploadBoundary" + hashlib.sha1(os.urandom(16)).hexdigest()
        body = io.BytesIO()

        def write_field(name: str, value: str):
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.write(str(value).encode())
            body.write(b"\r\n")

        def write_file(name: str, filename: str, path: Path):
            body.write(f"--{boundary}\r\n".encode())
            body.write(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            body.write(b"Content-Type: application/octet-stream\r\n\r\n")
            with open(path, "rb") as f:
                shutil.copyfileobj(f, body)
            body.write(b"\r\n")

        write_field("team_id", str(team_id))
        write_field("self_service", json.dumps(bool(self_service)).lower())
        if install_script:
            write_field("install_script", install_script)
        if uninstall_script:
            write_field("uninstall_script", uninstall_script)
        if pre_install_query:
            write_field("pre_install_query", pre_install_query)
        if post_install_script:
            write_field("post_install_script", post_install_script)
        if automatic_install:
            write_field("automatic_install", "true")

        for label in labels_include_any:
            write_field("labels_include_any", label)
        for label in labels_exclude_any:
            write_field("labels_exclude_any", label)

        write_file("software", pkg_path.name, pkg_path)
        body.write(f"--{boundary}--\r\n".encode())

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = urllib.request.Request(url, data=body.getvalue(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                resp_body = resp.read()
                status = resp.getcode()
        except urllib.error.HTTPError as e:
            if e.code == 409:
                body = e.read() or b"{}"
                data = json.loads(body)
                pkg = data.get("software_package", {})
                title_id = pkg.get("title_id")
                installer_id = pkg.get("installer_id")
                hash_sha256 = pkg.get("hash_sha256")
                if not hash_sha256:
                    sha256 = hashlib.sha256()
                    with open(pkg_path, "rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            sha256.update(chunk)
                    hash_sha256 = sha256.hexdigest()
                return {
                    "software_package": {
                        "title_id": title_id,
                        "installer_id": installer_id,
                        "hash_sha256": hash_sha256,
                        "version": version,
                    }
                }
            raise ProcessorError(f"Fleet upload failed: {e.code} {e.read().decode()}")
        if status != 200:
            raise ProcessorError(f"Fleet upload failed: {status} {resp_body.decode()}")
        return json.loads(resp_body or b"{}")

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

        # Write the package fields directly without a top-level wrapper.
        self._write_yaml(pkg_yaml_path, pkg_block)

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
        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "maintainer_can_modify": True,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(api, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.getcode()
                resp_body = resp.read().decode()
        except urllib.error.HTTPError as e:
            status = e.getcode()
            resp_body = e.read().decode()
        if status not in (201, 422):
            raise ProcessorError(f"PR creation failed: {status} {resp_body}")
        pr = json.loads(resp_body or "{}")
        pr_url = pr.get("html_url") or self._find_existing_pr_url(github_repo, github_token, head, base)

        if labels and pr_url and "number" in pr:
            issue_api = f"https://api.github.com/repos/{github_repo}/issues/{pr['number']}/labels"
            issue_data = json.dumps({"labels": labels}).encode()
            issue_req = urllib.request.Request(issue_api, data=issue_data, headers=headers, method="POST")
            try:
                urllib.request.urlopen(issue_req, timeout=30)
            except urllib.error.HTTPError:
                pass

        return pr_url or ""

    def _find_existing_pr_url(self, repo: str, token: str, head: str, base: str) -> str:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        q = f"repo:{repo} is:pr is:open head:{head} base:{base}"
        url = f"https://api.github.com/search/issues?q={urllib.parse.quote(q)}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError:
            return ""
        if data.get("items"):
            return data["items"][0]["html_url"]
        return ""


