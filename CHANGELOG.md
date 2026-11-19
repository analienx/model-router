# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/).

## [1.3.0] - 2026-11-19

Fixes for the findings of an external review of 1.2.1. Each fix has a regression test (`test_review2_*`), and the end-to-end test checks the main ones with the real Claude Code.

### Fixed
- **`deep-haiku` evidence is scoped to the current prompt and to real failures.** The transcript check no longer falls back to an `architect`/`implementer` call of an earlier prompt, and it requires a notification with `<status>failed</status>` or an error `tool_result`. A successful result that mentions rate limits never counts.
- **Parallel hooks no longer lose updates.** State changes run under a per-session lock, and each prompt has a generation number, so a slow update for an older prompt cannot overwrite a newer one. Three parallel `architect` calls are now all recorded.
- **A main session started with `--agent NAME` is treated as the main session.** The guard identifies subagents by `agent_id`, not `agent_type`.
- **One delegate policy, enforced as documented.** STANDARD and DEEP refuse `general-purpose` and forks from the main session; agent calls made inside subagents are checked for `model` overrides and for `deep-haiku` evidence.
- **No prompt-derived text crosses providers or lands in default logs.** The next judge gets only the previous tier, not its reason. Model-written reasons and raw judge errors are logged only with the new `logging.log_judge_details`.
- **Inherited environment variables can no longer override the router.** `CLAUDE_CODE_MAX_RETRIES` and `CLAUDE_CODE_RETRY_WATCHDOG` are replaced for every child; judges ignore `CLAUDE_CODE_EFFORT_LEVEL` and `MAX_THINKING_TOKENS`; Foundry children drop `CLAUDE_CODE_OAUTH_TOKEN`. The policy is documented in the guide, section 7.6.
- **A user `--settings` no longer drops the deployment mapping.** The launcher merges it with the router's `modelOverrides` into one file. Two tiers mapping the same model ID to different deployments are rejected.
- **Install and doctor follow the profile Claude Code reads** (`CLAUDE_CONFIG_DIR`). The launcher warns when the active profile has no router hooks.
- **A same-named agent of yours stops the install** instead of being kept while routing is switched on; `--force-agents` replaces it with a backup. `doctor` validates the agent Claude Code will load, including a project's own agents.
- **Project installs in a git subfolder are ignored correctly**, with a separate ignore block per project. Installs and uninstalls are serialised by a lock, so parallel runs keep every registry entry.
- **Edit counting matches Claude Code** for `replace_all` on files with Windows line endings and for notebook cell inserts.
- **The offline zip passes its own self-tests** (the bundle-builder tests skip outside a source checkout), and CI now runs them from the extracted zip.
- **Release files are reproducible and never replaced.** The zip has fixed timestamps and permissions, and a rerun of the release workflow keeps already-published files.
- `report` computes judge latency only from prompts a model judge rated, not from notifications and shortcuts.

### Added
- **Terminal graphics.** `claude-router` opens with a framed, coloured banner of your routing: tiers and deployments, the judge chain, edit budgets and warnings. After each prompt Claude Code shows you a one-line verdict (a `systemMessage`, never sent to the model). `install.py` prints coloured ✓ / ! / ✗ marks. Colour is used only on terminals, `NO_COLOR` is honoured, and the banner falls back to ASCII where needed. The new `ui` settings (`banner`, `verdict_line`) turn it down or off.
- `python install.py eval`: runs the judge chain over a labeled corpus and reports accuracy, under- and over-routing, latency and cost. A starter corpus of 30 synthetic prompts is in `tests/eval/`.
- End-to-end: new scenarios for a named main session and parallel calls, plus inherited-environment, `--settings` and launcher-banner checks; 34 checks in total, and the report shows the banner as printed on each runner.

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

[1.3.0]: https://github.com/analienx/model-router/releases/tag/v1.3.0
[1.2.1]: https://github.com/analienx/model-router/releases/tag/v1.2.1
[1.2.0]: https://github.com/analienx/model-router/releases/tag/v1.2.0
[1.1.0]: https://github.com/analienx/model-router/releases/tag/v1.1.0
[1.0.0]: https://github.com/analienx/model-router/releases/tag/v1.0.0
