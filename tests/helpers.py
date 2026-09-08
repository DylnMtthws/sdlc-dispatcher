import subprocess
from pathlib import Path

from sdlc_dispatcher.config import Project


def project(root: Path, **kwargs):
    values = dict(
        id="sample",
        repository=str(root / "source"),
        base_ref="main",
        image="test:local",
        checks=[["python", "-m", "unittest", "discover"]],
        allowed_paths=["src/**", "tests/**", "test_*.py"],
    )
    values.update(kwargs)
    return Project(**values)


def repository(root: Path, files=None):
    root.mkdir()
    for name, data in (files or {"src/app.py": "value = 1\n"}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    for args in (
        ["init", "-b", "main"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "Fixture",
        ],
    ):
        subprocess.run(["/usr/bin/git", "-C", str(root), *args], check=True, capture_output=True)
    return root
