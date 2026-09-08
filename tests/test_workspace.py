import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import project, repository

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.workspace import (
    File,
    changes,
    export,
    git,
    materialize,
    snapshot,
    valid_path,
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project(self.root)

    def test_refresh_reads_new_remote_commit_without_changing_local_branch(self):
        repo = repository(self.root / "source")
        remote = repository(self.root / "remote")
        (remote / "src/app.py").write_text("value = 7\n")
        git(remote, "add", "src/app.py")
        git(
            remote,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "Next",
        )
        original_sha, _ = export(self.project)

        def local_fetch(repo_path, *args):
            if "fetch" in args:
                self.assertIn("https://github.com/owner/project.git", args)
                args = tuple(
                    str(remote) if a == "https://github.com/owner/project.git" else a for a in args
                )
            return git(repo_path, *args)

        with patch("sdlc_dispatcher.workspace.git", side_effect=local_fetch):
            new_sha, files = export(
                replace(self.project, github_repository="owner/project"), refresh=True
            )
        self.assertNotEqual(new_sha, original_sha)
        self.assertEqual(files["src/app.py"].data, b"value = 7\n")
        self.assertEqual(export(self.project)[0], original_sha)
        self.assertEqual((repo / "src/app.py").read_text(), "value = 1\n")

    def test_export_uses_commit_and_omits_local_secrets_and_edits(self):
        repo = repository(self.root / "source")
        (repo / ".env").write_text("SECRET=private")
        (repo / "src/app.py").write_text("uncommitted")
        sha, files = export(self.project)
        self.assertEqual(len(sha), 40)
        self.assertEqual(set(files), {"src/app.py"})
        self.assertEqual(files["src/app.py"].data, b"value = 1\n")

    def test_symlink_output_rejected_without_following_it(self):
        root = self.root / "out"
        materialize(root, {"src/app.py": File(b"safe")})
        (root / "src/link").symlink_to("/etc/passwd")
        with self.assertRaises(DispatchError):
            snapshot(root, 10000)

    def test_hard_links_and_fifos_rejected(self):
        root = self.root / "out"
        materialize(root, {"src/app.py": File(b"safe")})
        os.link(root / "src/app.py", root / "src/link")
        with self.assertRaises(DispatchError):
            snapshot(root, 10000)
        (root / "src/link").unlink()
        os.mkfifo(root / "src/fifo")
        with self.assertRaises(DispatchError):
            snapshot(root, 10000)

    def test_protected_and_out_of_scope_changes_rejected(self):
        for name in [
            ".github/workflows/ci.yml",
            "pyproject.toml",
            "AGENTS.md",
            "other.txt",
            "src/.git/config",
            "src/.env",
            "tests/conftest.py",
        ]:
            with self.subTest(name=name), self.assertRaises(DispatchError):
                changes(self.project, {}, {name: File(b"unsafe")})

    def test_existing_tests_cannot_be_weakened(self):
        with self.assertRaises(DispatchError):
            changes(
                self.project,
                {"tests/test_app.py": File(b"assert False")},
                {"tests/test_app.py": File(b"assert True")},
            )

    def test_binary_or_secret_or_oversize_output_rejected(self):
        for data in [b"\xff", b"\0", b"-----BEGIN RSA PRIVATE KEY-----", b"x" * 300000]:
            with self.subTest(data=data[:20]), self.assertRaises(DispatchError):
                changes(self.project, {}, {"src/app.py": File(data)})
        with self.assertRaises(DispatchError):
            changes(
                self.project,
                {},
                {"src/app.py": File(b"some-private-value")},
                secrets=("some-private-value",),
            )

    def test_small_edit_in_large_file_fits_diff_budget(self):
        before = "".join(f"line_{i} = {i}\n" for i in range(20000))
        after = before.replace("line_10000 = 10000", "line_10000 = 10001")
        registration = replace(self.project, max_patch_bytes=1000)
        delta = changes(
            registration,
            {"src/app.py": File(before.encode())},
            {"src/app.py": File(after.encode())},
        )
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["after"], after)

    def test_large_rewrite_and_multibyte_changes_exceed_diff_budget(self):
        registration = replace(self.project, max_patch_bytes=1000)
        for before, after in [
            (b"a\n" * 2000, b"b\n" * 2000),
            (b"old\n", ("雪" * 400 + "\n").encode()),
        ]:
            with self.subTest(size=len(after)), self.assertRaises(DispatchError):
                changes(registration, {"src/app.py": File(before)}, {"src/app.py": File(after)})

    def test_add_edit_delete_supported(self):
        delta = changes(self.project, {"src/old.py": File(b"old")}, {"src/new.py": File(b"new")})
        self.assertEqual(len(delta), 2)
        self.assertEqual(delta[0]["after"], "new")
        self.assertIsNone(delta[1]["after"])

    def test_unsafe_paths(self):
        for name in [
            "../escape",
            "/root",
            "src/../../escape",
            "src\\escape",
            "a\nb",
            ".git/config",
        ]:
            with self.subTest(name=name), self.assertRaises(DispatchError):
                valid_path(name)
