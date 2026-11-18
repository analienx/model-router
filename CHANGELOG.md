# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/).

## [1.2.1] - 2026-11-18

Found by the end-to-end test on GitHub's runners.

### Fixed
- **A "429" in a notification's ids or file path no longer counts as a rate-limit error.** The `<output-file>` path carries the project folder, so an `architect` that failed for another reason in a folder such as `issue-429` unlocked `deep-haiku`. Both the judge hook and the guard now ignore `<task-id>`, `<tool-use-id>` and `<output-file>` when looking for 429/529.
- End-to-end test: the mock API waited for a 429 with a pattern that also matched a random agent id (`a73c74297540d6f7e`), so on one run it called `deep-haiku` before `architect` had failed, and the guard correctly refused. The mock now waits for the actual failure notification. Temp paths are resolved (Windows 8.3 names, the macOS `/var` symlink), and the mock server's errors are captured in the report.

### Added
- Releases: pushing a version tag publishes a GitHub release with the offline zip and `SETUP-offline.md` attached, after the unit tests pass.
- A new README with generated light and dark images (`scripts/build_assets.py`), checked in CI. The offline zip now includes them.
- The end-to-end test is its own workflow with its own badge. GitHub Actions are updated to their current major versions, and Dependabot keeps them current.
- `CODE_OF_CONDUCT.md`, `SUPPORT.md` and `CODEOWNERS`.

## [1.2.0] - 2026-11-18

Found by the new end-to-end test with the real Claude Code CLI.

### Fixed
- **`deep-haiku` fallback with background agents.** Claude Code 2.1.327 runs subagents in the background and reports a failure as a `<task-notification>` prompt. The transcript line can be written after the next hook runs, so the old transcript-only check refused the fallback. Now the guard records each `architect`/`implementer` call's `tool_use_id`, and the judge hook marks a call as throttled when a notification for that exact ID reports a 429/529. The router's own directive text, which mentions "429", can never count as evidence.
- **Background-agent notifications no longer count as new prompts.** They keep the current verdict and edit counters, and no judge call is made.

### Added
- `tests/e2e/`: a mock Anthropic/Foundry API and an end-to-end runner that drives the real Claude Code CLI through the installed launcher. It runs 25 checks in 5 scenarios.
- GitHub Actions: unit tests on Linux, macOS and Windows with Python 3.8–3.14; end-to-end tests on all three OSes with a pinned Claude Code; a weekly informational run against the latest Claude Code; the offline bundle as an artifact.
- `CLAUDE_ROUTER_DEBUG_DIR`: an opt-in dump of every hook input, for troubleshooting.
- `foundry.base_url` accepts plain `http://` for localhost test servers only.
- `scripts/build_bundle.py`: an offline zip plus a `SETUP.md` that carries the full source with checksums.

## [1.1.0] - 2026-11-18

Fixes for all 15 findings of a code review, each with a regression test.

### Fixed
- Uninstalling one scope no longer deletes code that another scope's hooks still use. Hook commands exit 0 when the router code is missing, instead of blocking every prompt with Python's exit code 2.
- The judge no longer loads CLAUDE.md files, user settings or hooks (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `--setting-sources project,local`, `disableAllHooks`). The Foundry judge also uses `--bare`.
- Edit counting:
  - every edit call counts at least 1 line;
  - `Write` counts the file it replaces;
  - `replace_all` counts every occurrence;
  - deleting a notebook cell counts the cell;
  - small-model subagents share a per-prompt budget.
- The delegate must match the verdict: DEEP refuses `implementer`, and `deep-haiku` requires a 429/529.
- Short new tasks are judged. Only exact follow-up phrases ("yes", "continue") reuse the last verdict, and "still failing" / "try again" move up a tier.
- Prompts starting with a file path (`/src/...`) are no longer taken for slash commands.
- File errors and wrong-shaped config or state no longer crash the judge. The config is type-checked strictly.
- Project installs keep personal files out of git (`.git/info/exclude`). Backups go to the router folder with unique names.
- Install and uninstall only remove hooks that point at the router's own folder.
- The guard matcher includes `Task`.
- Windows: the judge timeout kills the whole process tree, launchers get exact line endings, and Ctrl+C no longer kills Claude Code through the launcher.

## [1.0.0] - 2026-11-18

### Added
- First release:
  - judge hook (subscription, Foundry, keyword rules);
  - guard hook (edit budgets, pinned subagent models);
  - `architect`, `implementer` and `deep-haiku` agents;
  - Foundry launcher;
  - installer with `doctor`, `test-judge` and `report`.

[1.2.1]: https://github.com/analienx/model-router/releases/tag/v1.2.1
[1.2.0]: https://github.com/analienx/model-router/releases/tag/v1.2.0
[1.1.0]: https://github.com/analienx/model-router/releases/tag/v1.1.0
[1.0.0]: https://github.com/analienx/model-router/releases/tag/v1.0.0
