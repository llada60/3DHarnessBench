"""Exercise dependency checkout behavior against real local Git repositories."""

import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.blender_mcp.source import ensure_source


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.upstream = self.repo / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "-b", "main")
        for name, text in {
            "mcp/pyproject.toml": '[project]\nname = "blender-mcp"\nversion = "1.0.0"\n',
            "mcp/blmcp/__init__.py": "# original server\n",
            "addon/blender_mcp_addon/__init__.py": "# original addon\n",
        }.items():
            path = self.upstream / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.upstream, "add", ".")
        git(self.upstream, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", "fixture")
        git(self.upstream, "tag", "v1.0.0")
        self.commit = git(self.upstream, "rev-parse", "HEAD")
        self.cfg = {"official": {
            "source_url": self.upstream.as_uri(),
            "source_ref": "v1.0.0", "source_commit": self.commit,
            "cache_dir": "cache",
        }}

    def test_checkout_is_unmodified_detached_and_reused(self):
        source = ensure_source(self.repo, self.cfg)
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.commit)
        self.assertEqual(git(source, "status", "--porcelain"), "")
        self.assertEqual(git(source, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(ensure_source(self.repo, self.cfg), source)
        for name in git(self.upstream, "ls-files").splitlines():
            self.assertEqual((source / name).read_bytes(), (self.upstream / name).read_bytes())

    def test_modified_cache_is_rejected_without_overwriting(self):
        source = ensure_source(self.repo, self.cfg)
        target = source / "mcp/blmcp/__init__.py"
        target.write_text("# locally patched\n")
        with self.assertRaisesRegex(RuntimeError, "local changes"):
            ensure_source(self.repo, self.cfg)
        self.assertEqual(target.read_text(), "# locally patched\n")

    def test_wrong_commit_is_rejected_and_staging_is_cleaned(self):
        self.cfg["official"]["source_commit"] = "0" * 40
        with self.assertRaisesRegex(RuntimeError, "source_commit"):
            ensure_source(self.repo, self.cfg)
        self.assertFalse(any(p.is_dir() for p in (self.repo / "cache").iterdir()))

    def test_commit_pin_is_required(self):
        del self.cfg["official"]["source_commit"]
        with self.assertRaisesRegex(ValueError, "40-character"):
            ensure_source(self.repo, self.cfg)
        self.assertFalse((self.repo / "cache").exists())

    def test_local_source_takes_precedence_and_preserves_edits(self):
        target = self.upstream / "mcp/blmcp/__init__.py"
        target.write_text("# development fork\n")
        source = ensure_source(self.repo, {"official": {"source_path": "upstream"}})
        self.assertEqual(source, self.upstream)
        self.assertEqual(target.read_text(), "# development fork\n")
        self.assertFalse((self.repo / "cache").exists())

    def test_invalid_local_source_has_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, "missing mcp/pyproject.toml"):
            ensure_source(self.repo, {"official": {"source_path": "missing"}})

    def test_forks_with_same_tag_and_commit_have_separate_caches(self):
        first = ensure_source(self.repo, self.cfg)
        fork = self.repo / "fork"
        git(self.repo, "clone", str(self.upstream), str(fork))
        self.cfg["official"]["source_url"] = fork.as_uri()
        second = ensure_source(self.repo, self.cfg)
        self.assertNotEqual(first, second)
        self.assertEqual(git(second, "remote", "get-url", "origin"), fork.as_uri())

    def test_parallel_callers_share_one_complete_checkout(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            sources = list(pool.map(lambda _: ensure_source(self.repo, self.cfg), range(4)))
        self.assertEqual(len(set(sources)), 1)
        self.assertEqual(git(sources[0], "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
