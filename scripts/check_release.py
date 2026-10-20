#!/usr/bin/env python3
"""Check release paths and common secret/machine-specific data without printing values."""
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = (
    ".gitignore", ".gitattributes", "examples/env.example", "AGENTS.md", "README.md", "LICENSE",
    "__init__.py", "main.py", "sakura/__init__.py", "sakura/runtime.py", "sakura/settings.py",
    "sakura/message_flow.py", "sakura/permissions.py", "sakura/web_reading.py", "sakura/image_api.py",
    "metadata.yaml", "_conf_schema.json", "requirements.txt", "requirements/dev.txt",
    "scripts/check_release.py", "docs/deployment.md",
    "tests/support.py", "tests/test_sakura_runtime_context.py", "tests/test_sakura_message_flow.py",
    "tests/test_sakura_site_reading.py", "tests/test_sakura_web_permissions.py",
    "tests/test_settings.py", "tests/test_deployment.py",
)
PATTERNS = {
    "machine-specific drive path": re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"),
    "personal home path": re.compile(r"/(?:home|Users|root)/[A-Za-z0-9_.-]+"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "API or access token": re.compile(r"(?:sk-|github_pat_|gh[pousr]_)[A-Za-z0-9_-]{16,}"),
    "personal email": re.compile(r"[A-Za-z0-9_.+-]+@(?:qq|163|126|gmail|outlook)\.com", re.I),
    "account identifier": re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32}(?![A-Za-z0-9])"),
    "credential in URL": re.compile(r"https?://[^\s/'\"]+:[^\s/'\"]+@"),
}


def findings(path, text):
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                result.append(f"{path}:{number}: {label}")
    return result


def main():
    errors = []
    for name in RELEASE_FILES:
        if not (ROOT / name).is_file():
            errors.append(f"{name}: required release file is missing")
    listed = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=ROOT, capture_output=True)
    staged = listed.returncode == 0 and bool(listed.stdout)
    paths = listed.stdout.decode("utf-8").rstrip("\0").split("\0") if staged else RELEASE_FILES
    for name in paths:
        if name not in RELEASE_FILES or ".." in PurePosixPath(name).parts:
            errors.append(f"{name}: file is outside the release allow-list")
            continue
        if staged:
            blob = subprocess.run(["git", "show", f":{name}"], cwd=ROOT, capture_output=True, check=True).stdout
        else:
            if not (ROOT / name).is_file():
                continue
            blob = (ROOT / name).read_bytes()
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{name}: release sources must be UTF-8 text")
            continue
        errors.extend(findings(name, text))
    if staged:
        errors.extend(f"{name}: not present in Git index" for name in RELEASE_FILES if name not in paths)
    if errors:
        print("Release check FAIL (values withheld):")
        print("\n".join(errors))
        return 1
    source = "Git index" if staged else "release working files"
    print(f"Release check PASS: {len(paths)} files scanned from {source}; no common secret or machine-path patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
