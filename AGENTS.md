# Repository guidance

- On Windows, prefer PowerShell 7 and `rg` / `rg --files`.
- This repository is the portable plugin source. AstrBot owns runtime configuration, conversations, platform credentials and logs outside the release files.
- Keep implementation under `sakura/`, examples under `examples/`, developer dependencies under `requirements/`, and release tools under `scripts/`. The root `main.py` registers AstrBot handlers and delegates to the runtime; do not register a second plugin from an implementation module.
- Use relative paths or AstrBot path APIs. Never embed developer machine paths, user IDs, API keys or personal provider URLs.
- Keep image credentials in process environment variables. Keep the administrator allow-list empty by default.
- Read only the task-relevant parts of any skill. Do not invoke skills just for keyword matches.
- Create any worktrees or temporary project copies under this project's `.codex/worktrees/` or `.codex/copies/`.
- The `.gitignore` uses a release allow-list because the maintenance workspace may contain private data. Add new release files explicitly; never force-add runtime material.
- Run `python -m unittest discover -s tests -p "test_*.py"` and `python scripts/check_release.py` before a release. Tests must import the repository copy, not an installed personal plugin.
- Keep local tests, actual image-provider requests and QQ/WeChat delivery evidence separate. Do not send platform messages or change a running installation unless the task authorizes it.
