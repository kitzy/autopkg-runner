import types
import sys
from pathlib import Path

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub autopkglib before importing the module under test
autopkglib = types.ModuleType("autopkglib")

class Processor:
    def __init__(self):
        self.env = {}

class ProcessorError(Exception):
    pass

autopkglib.Processor = Processor
autopkglib.ProcessorError = ProcessorError
sys.modules.setdefault("autopkglib", autopkglib)

from overrides.FleetImporter import FleetImporter


def test_pr_body_basic():
    body = FleetImporter._pr_body("Firefox", "1.2.3", "firefox", 42, 99)
    assert "### Firefox 1.2.3" in body
    assert "Fleet title ID: `42`" in body
    assert "Fleet installer ID: `99`" in body
    assert "Software slug: `firefox`" in body


def test_pr_body_changelog():
    body = FleetImporter._pr_body("Firefox", "1.2.3", "Mozilla/firefox", 42, 99)
    assert "[Changelog](https://github.com/Mozilla/firefox/releases/tag/1.2.3)" in body

