"""
Lightweight local shim of the FleetImporter AutoPkg processor, exposing only
the bits we unit-test here (the _pr_body helper). This avoids depending on
the upstream repo at test time while aligning with the new naming.
"""
from typing import Optional

try:
    # AutoPkg processors derive from autopkglib.Processor; tests stub this.
    from autopkglib import Processor  # type: ignore
except Exception:  # pragma: no cover - tests stub autopkglib
    class Processor:  # fallback to allow import if stub not in place
        def __init__(self) -> None:
            self.env = {}


class FleetImporter(Processor):
    """Minimal surface to support unit tests for PR body composition."""

    @staticmethod
    def _pr_body(
        name: str,
        version: str,
        software_slug: str,
        title_id: int,
        installer_id: int,
        changelog_url: Optional[str] = None,
    ) -> str:
        """
        Compose a markdown PR body.

        Args:
            name: Human-readable software name
            version: Version string
            software_slug: Either a simple slug (e.g. "firefox") or
                an "owner/repo" GitHub slug for changelog linking
            title_id: Fleet software title id
            installer_id: Fleet installer id
            changelog_url: Optional explicit changelog URL; if not provided and
                software_slug looks like "owner/repo", link to GitHub releases tag
        """
        lines = [f"### {name} {version}", ""]

        # Changelog link logic
        url = changelog_url
        if not url and "/" in software_slug:
            owner, repo = software_slug.split("/", 1)
            url = f"https://github.com/{owner}/{repo}/releases/tag/{version}"
        if url:
            lines.append(f"[Changelog]({url})")
            lines.append("")

        # Metadata section
        lines.append(f"Fleet title ID: `{title_id}`")
        lines.append(f"Fleet installer ID: `{installer_id}`")
        lines.append(f"Software slug: `{software_slug}`")

        return "\n".join(lines) + "\n"
