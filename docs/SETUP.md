# claude-model-router: setup and operating guide

Version 1.3.0. Written against Claude Code 2.1.327 and its documentation as of November 2026. The full change history is in [CHANGELOG.md](../CHANGELOG.md).


This package makes Claude Code run like this:

- Your session runs on **Haiku on Azure (Microsoft Foundry)**. It is cheap and fast and handles easy work itself.
- Before Haiku sees each new prompt, a **judge** rates how hard the prompt is. The judge is **Sonnet at max effort on your Claude subscription** (Pro, Max, Team or Enterprise).
- Depending on the verdict, the work goes to **Sonnet or Opus on Azure Foundry**, running as subagents.
- Hooks **enforce** the verdict: Haiku can't make large edits itself when the verdict says the work belongs to a stronger model.

Everything is plain Python (standard library only) plus Claude Code's own hooks and agent files. Nothing is sent anywhere except through `claude`.

---

## Contents

1. [How it works in one picture](#1-how-it-works-in-one-picture)
2. [Read this before installing: where your data goes](#2-read-this-before-installing-where-your-data-goes)
3. [Prerequisites checklist](#3-prerequisites-checklist)
4. [Installation, step by step](#4-installation-step-by-step)
5. [Configuration reference (`router.config.json`)](#5-configuration-reference-routerconfigjson)
6. [Verify the installation](#6-verify-the-installation)
7. [How it works in detail](#7-how-it-works-in-detail)
8. [Daily use](#8-daily-use)
9. [Tuning and calibration](#9-tuning-and-calibration)
10. [Cost and latency](#10-cost-and-latency)
11. [Troubleshooting](#11-troubleshooting)
12. [Limits and known gaps](#12-limits-and-known-gaps)
13. [Uninstall](#13-uninstall)
14. [File reference](#14-file-reference)

---

## 1. How it works in one picture

```
 you type a prompt
        |
        v
 +-----------------------------+     claude -p --model sonnet --effort max
 | UserPromptSubmit hook       | --> (your Claude subscription login)
 | router/judge_hook.py        |     -> {"tier": "deep", "confidence": 0.86, "reason": "..."}
 |                             |     if that fails: Foundry Sonnet judge, then keyword rules
 +-------------+---------------+
               | verdict saved per session + routing directive injected into Haiku's context
               v
 +-----------------------------+
 | Main session: Haiku         |  runs on Azure Foundry (CLAUDE_CODE_USE_FOUNDRY=1, set by the launcher only)
 |                             |
 |  FAST     -> does it itself (small edits only)
 |  STANDARD -> Agent tool -> `implementer` subagent (sonnet alias -> your Foundry Sonnet deployment)
 |  DEEP     -> Agent tool -> `architect` subagent   (opus alias   -> your Foundry Opus deployment)
 |  429/529 on a subagent -> `deep-haiku` (haiku alias at max effort)
 +-------------+---------------+
               |
               v
 +-----------------------------+
 | PreToolUse hook             |  counts main-thread edit lines per prompt against the tier's budget,
 | router/guard_hook.py        |  denies over-budget edits with "delegate to implementer/architect",
 |                             |  checks the delegate matches the verdict (DEEP -> architect),
 |                             |  allows deep-haiku only after a visible 429/529,
 |                             |  denies `model` overrides on implementer/architect
 +-----------------------------+
```

The router is **on only when you start Claude Code with the `claude-router` launcher**. Plain `claude` is not affected, apart from the three agent files being visible.

---

## 2. Read this before installing: where your data goes

| What | Goes to | Billed as |
|---|---|---|
| Your prompt text (up to 6,000 characters), the repository folder name, the git branch name and the count of uncommitted files | **Anthropic**, through your Claude subscription (the judge) | your subscription's usage allowance |
| The same, but only if the subscription judge fails | **Azure Foundry** (fallback judge) | Azure tokens |
| Everything Claude Code normally sends: the conversation, file contents, tool results | **Azure Foundry** (main session and subagents) | Azure tokens |

The judge receives **only** its instructions (`judge_system.md`), the items in the first row, and the **tier** (one word) of the previous verdict in this session. It never receives the previous verdict's reason, because a reason written by a model can quote an earlier prompt, and that prompt may have been judged by the other provider. It is started with:
- CLAUDE.md loading switched off (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`), so your user and project memory files are not sent;
- your user settings left out (`--setting-sources project,local`, run in an empty folder);
- hooks switched off (`disableAllHooks`);
- no MCP servers (`--strict-mcp-config`) and no tools.

Organisation-managed policy files still apply, as they do for every Claude Code run.

So once the router is installed, each prompt goes to **two providers**. If your company chose Azure Foundry for compliance or data residency, **get approval for the judge path first**. If you can't get it, delete the `subscription` step from `judge.chain` (section 5.6). The judge then runs on Foundry only and nothing goes to the subscription.


**What the router keeps on your machine** (in `%USERPROFILE%\.claude-router`):

| File | Contains by default | More only if you turn it on |
|---|---|---|
| `logs\decisions.jsonl` | per prompt: time, tier, source (for example `subscription:sonnet`), confidence, latency, reported cost, a 16-character hash and the length of the prompt, and for each failed judge step a short error **category** (`timeout`, `unusable-output`, ...). Reasons are kept only when the router wrote them itself (rules, shortcuts, notifications). | `logging.log_judge_details`: the reason written by a model judge and the full error messages, which can quote the prompt. `logging.log_prompt_text`: the first 2,000 characters of each prompt. |
| `state\<session>.json` | the current verdict and its reason, edit counters and the IDs of this prompt's `architect`/`implementer` calls. Deleted after 7 days. | - |
| `backups\` | copies of your settings and agent files from before each install change | - |
| any folder you name | nothing | `CLAUDE_ROUTER_DEBUG_DIR`: every hook input, including prompts (troubleshooting only) |

---

## 3. Prerequisites checklist

Tick every line before step 4.

- [ ] **Claude Code is installed and up to date.** Run `claude update`, then `claude --version`. This guide was written against 2.1.327; older versions may not support exec-form hooks or the judge flags. On Windows, prefer the **native installer** (`claude.exe`) over the npm package (`claude.cmd`).
- [ ] **Python 3.8 or newer.** Check with `py -3 --version` or `python --version` on Windows, `python3 --version` on macOS and Linux. Run the installer with the same Python you want the hooks to use, because its absolute path is written into the hook configuration.
- [ ] **Claude subscription login works.** You can sign in to Claude Code with your Claude account, for example your company's Enterprise SSO (step 4.3).
- [ ] **An Azure Microsoft Foundry resource** with Claude deployments. You need:
  - the **resource name**: the part before `.services.ai.azure.com`, for example `contoso-ai`
  - the **deployment name and model** of each Claude deployment you want to use: Haiku, Sonnet and Opus. Not all three are required (section 5.3).
- [ ] **Azure access**, one of:
  - **Microsoft Entra ID (recommended):** your account has the **Foundry User** role (older name *Azure AI User*) or **Cognitive Services User** on the Foundry resource, and the Azure CLI (`az`) is installed so you can run `az login`.
  - **API key:** a key from the resource's *Endpoints and keys* page, which you will keep in an environment variable. Never put it in a file.
- [ ] **Organisation policy allows** user hooks and the Foundry provider. If `doctor` reports organisation-managed settings, check them with IT (section 11).
- [ ] **Windows, optional:** Git for Windows (Git Bash). The default hook style doesn't need it.

---

## 4. Installation, step by step

The commands below use **Windows PowerShell**. Replace `%USERPROFILE%` or `$env:USERPROFILE` with `~` on macOS and Linux, and `python` with `python3`.

### 4.1 Get the files onto the work computer

1. Copy `claude-model-router.zip` to the work computer, then unzip it anywhere, for example `C:\Tools\claude-model-router`. The installer copies what it needs into `%USERPROFILE%\.claude-router`, so this folder only holds the installer and the tests.
2. If your mail system blocks the zip (it contains `.py` files), send yourself the **offline bundle's** `SETUP.md` instead: its last section has the full source of every file with a SHA-256 checksum for each. Download `SETUP-offline.md` from the [Releases page](https://github.com/analienx/model-router/releases), or build it with `python scripts/build_bundle.py`. Recreate the folder layout from section 14 and paste each file in exactly as shown.

Open PowerShell in the unzipped folder:

```powershell
cd C:\Tools\claude-model-router
```

### 4.2 Optional: run the self-tests (about 5 seconds, no network)

```powershell
python -m unittest discover -s tests
```

Expected: `Ran 106 tests ... OK` (from the offline zip: `OK (skipped=2)`, because the zip does not ship the bundle builder). The tests use a fake `claude` (`tests/fake_claude.py`), so no model is called and nothing is billed.

### 4.3 Log in to your Claude subscription (used by the judge)

The judge calls `claude -p` **without any Foundry variables**, so it uses the account you are logged in to with Claude Code.

1. Open a **new** terminal where no `CLAUDE_CODE_USE_FOUNDRY`, `ANTHROPIC_API_KEY` or `ANTHROPIC_*` variables are set. Check with `Get-ChildItem Env:ANTHROPIC*, Env:CLAUDE_CODE_USE*`, which should print nothing.
2. Run `claude`, then type `/login` and sign in with your Claude account (for Enterprise: company SSO).
3. Type `/status`. It should show your organisation and a Claude subscription login, not Microsoft Foundry. Then exit with `/exit`.
4. Quick check that headless calls use the subscription:

   ```powershell
   claude -p "Reply with the single word OK" --model sonnet
   ```

   It should print `OK`.

**Important:** never put `CLAUDE_CODE_USE_FOUNDRY` or `ANTHROPIC_*` variables in any Claude Code `settings.json` `env` block. A settings `env` value overrides the shell, so the judge's subscription call would then be sent to Foundry. The launcher sets those variables for its own process only. `doctor` checks this.

### 4.4 Prepare Azure Foundry

1. In the Azure portal (or Foundry portal), open your Foundry resource and note the **resource name**.
2. Open **Deployments** and note, for each Claude deployment:
   - the **deployment name**
   - the **model and version**. Microsoft recommends pinning a specific version rather than "auto-update to latest".

   Best practice from the Claude Code docs: **name each deployment after its model ID**, for example a deployment called `claude-opus-5-5` for Opus 5.5. If your names differ, for example `team-opus-prod`, that's fine: section 5.3 shows how to map them.
3. **Entra ID:** make sure you have the **Foundry User** or **Cognitive Services User** role on the resource, then run:

   ```powershell
   az login
   # if you have several tenants:
   az login --tenant <your-tenant-id>
   ```

   **API key instead:** set the key in an environment variable called `FOUNDRY_API_KEY` (you can change the name in section 5.2), only in the terminal or user environment you launch from:

   ```powershell
   $env:FOUNDRY_API_KEY = "<key from Endpoints and keys>"   # current terminal only
   ```

### 4.5 Run the installer

Install **for your user**. It applies to every project, but only when you start with `claude-router`. This is the recommended scope:

```powershell
python install.py install
```

To install for **one project only**, use the line below. In a git repository, the installer adds the personal files it creates to `.git/info/exclude`, which is your local, never-committed ignore list. Outside git, don't commit them.

```powershell
python install.py install --scope project --project C:\src\your-repo
```

What the installer does:

| Step | User scope | Project scope |
|---|---|---|
| Copies the router code | `%USERPROFILE%\.claude-router\router\` | same |
| Creates the config, only if missing | `%USERPROFILE%\.claude-router\router.config.json` | same |
| Writes 3 agents (`architect`, `implementer`, `deep-haiku`) | `%USERPROFILE%\.claude\agents\` | `<repo>\.claude\agents\` |
| Adds 2 hooks | `%USERPROFILE%\.claude\settings.json` | `<repo>\.claude\settings.local.json` |
| Backs up the settings file first | `%USERPROFILE%\.claude-router\backups\` (never inside a repository) | same |
| Records the install, so uninstall knows which scopes still use the code | `%USERPROFILE%\.claude-router\installs.json` | same |
| Keeps personal files out of git | - | adds them to `<repo>\.git\info\exclude` |
| Writes the launchers | `%USERPROFILE%\.claude-router\bin\claude-router(.cmd/.ps1)` | same |

The installer:
- stops without changing anything if the agents folder already has an `architect.md`, `implementer.md` or `deep-haiku.md` that the router did not write: Claude Code would load yours instead, and routed work would go to the wrong agent. Rename yours, or run install with `--force-agents` to replace it (a backup is kept);
- installs into the profile Claude Code actually reads: `%CLAUDE_CONFIG_DIR%` when that variable is set, otherwise `%USERPROFILE%\.claude`. Run install, doctor and `claude-router` with the same `CLAUDE_CONFIG_DIR`; the launcher warns when it finds no router hooks in the active profile;
- for a project in a subfolder of a git repository, adds ignore patterns relative to the repository root, and keeps a separate block per project, so uninstalling one project never touches another;
- runs one install or uninstall at a time (a lock in the router folder), so parallel runs cannot lose each other's records;
- keeps your existing hooks and settings, and only ever removes hooks that point at the router's own folder;
- is safe to run again;
- checks your settings file first and stops without changing anything if it isn't valid JSON or its `hooks` section has the wrong shape.

**Hook style.** The default `exec` style runs Python directly, with no shell, so there are no quoting problems on Windows. Each hook command first checks that the router script exists and **does nothing if it is missing**. A deleted router folder therefore never blocks your prompts or edits; normally Python would exit with code 2, which Claude Code treats as "block". If your Claude Code version doesn't run the hooks (section 11), reinstall with one of these:

```powershell
python install.py install --hook-style bash         # Git Bash is installed
python install.py install --hook-style powershell   # no Git Bash
```

### 4.6 Edit the configuration

Open `%USERPROFILE%\.claude-router\router.config.json` and set at least:

- `foundry.resource`: your resource name, for example `contoso-ai`;
- `tiers.*.deployment` and `tiers.*.model_id`: your deployments.

Section 5 explains every field and has ready-made examples. If you change judge timeouts later, **run `python install.py install` again** so the hook timeout is updated.

### 4.7 Put the launcher on your PATH

**Windows**, once:

```powershell
[Environment]::SetEnvironmentVariable("Path",
  [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:USERPROFILE\.claude-router\bin", "User")
```

Then open a new terminal. If PowerShell refuses to run `claude-router.ps1` because of the execution policy, run `claude-router.cmd` instead, which works from PowerShell and cmd, or delete the `.ps1`.

**macOS and Linux:** add `export PATH="$HOME/.claude-router/bin:$PATH"` to your `~/.zshrc` or `~/.bashrc`.

### 4.8 Run the checks

```powershell
python install.py doctor
python install.py test-judge
claude-router --router-dry-run
```

Section 6 explains what each one should print.

### 4.9 Start working

```powershell
cd C:\src\your-repo
claude-router
```

You'll see a banner like this (in colour on a terminal that supports it, with each tier in its own colour):

```text
╭────────────────────────────────────────────────────────────────────╮
│ ◆ model-router 1.3.0          difficulty-based model routing       │
├────────────────────────────────────────────────────────────────────┤
│ FAST      → claude-haiku-5-5  Haiku handles it                     │
│ STANDARD  → team-sonnet       implementer (Sonnet)                 │
│ DEEP      → claude-opus-5-5   architect (Opus)                     │
├────────────────────────────────────────────────────────────────────┤
│ judge    subscription sonnet (max) → foundry sonnet (high) → rules │
│ guard    Haiku edits ≤ 40 / 10 / 0 lines · retries 3               │
│ foundry  contoso-ai · entra                                        │
╰────────────────────────────────────────────────────────────────────╯
  ::fast  ::standard  ::deep  choose a tier yourself · claude-router --router-dry-run
```

After each prompt, Claude Code shows you one line with the verdict. It is shown to you only and is not added to the model's context:

```text
◆ router: DEEP → architect (Opus) · judge subscription:sonnet 0.86 · 2.4s
◆ router: FAST → Haiku handles it (edits ≤ 40 lines) · your ::fast override
```

Background-agent notifications stay quiet, except when one unlocks the `deep-haiku` fallback. The look is set in section 5.9. Colour follows the usual conventions: none when output isn't a terminal or `NO_COLOR` is set, forced with `FORCE_COLOR=1`. Where the console can't print box-drawing characters (some Windows code pages), the banner falls back to plain ASCII.

Any normal `claude` arguments still work, for example `claude-router --resume` or `claude-router -c`.

---

## 5. Configuration reference (`router.config.json`)

The file lives at `%USERPROFILE%\.claude-router\router.config.json`; set the environment variable `CLAUDE_ROUTER_HOME` to move the whole router folder, and use the same value for install and uninstall. Keys starting with `_` are ignored, so you can use them as comments. Missing keys take the defaults shown here. Every value is type-checked: for example `true` is not accepted where a number is expected, and `null` is not accepted for a section. If anything is invalid, routing switches off and Claude is told which field is wrong, but your session keeps working.

### 5.1 Full default file

```json
{
  "version": 1,
  "foundry": {
    "resource": "YOUR-FOUNDRY-RESOURCE-NAME",
    "base_url": "",
    "auth": "entra",
    "api_key_env": "FOUNDRY_API_KEY",
    "prompt_caching_1h": false
  },
  "tiers": {
    "fast":     { "model_id": "claude-haiku-5-5",  "deployment": "claude-haiku-5-5" },
    "standard": { "model_id": "claude-sonnet-5-5", "deployment": "claude-sonnet-5-5" },
    "deep":     { "model_id": "claude-opus-5-5",   "deployment": "claude-opus-5-5" }
  },
  "launcher": { "max_retries": 3, "fallback_models": [], "extra_args": [] },
  "judge": {
    "chain": [
      { "backend": "subscription", "model": "sonnet", "effort": "max",  "timeout_s": 90 },
      { "backend": "foundry",      "model": "sonnet", "effort": "high", "timeout_s": 45 },
      { "backend": "rules" }
    ],
    "min_confidence": 0.6,
    "max_prompt_chars": 6000,
    "include_git_signals": true,
    "sticky_phrases": ["yes", "y", "yep", "yeah", "ok", "okay", "sure", "continue", "go on", "go ahead",
                       "do it", "proceed", "please do", "sounds good", "lgtm", "thanks", "thank you", "next", "go"],
    "escalate_phrases": ["try again", "still failing", "still fails", "still broken", "still not working",
                         "still wrong", "didn't work", "did not work", "doesn't work", "does not work",
                         "not working", "same error", "same problem", "it failed", "failed again", "again"],
    "claude_path": ""
  },
  "guard": {
    "main_edit_budget": { "fast": 40, "standard": 10, "deep": 0 },
    "other_subagent_edit_lines": 20
  },
  "logging": { "enabled": true, "log_prompt_text": false, "log_judge_details": false },
  "ui": { "banner": "full", "verdict_line": true }
}
```

### 5.2 `foundry`

| Field | Meaning |
|---|---|
| `resource` | Foundry resource name only, for example `contoso-ai`. A URL or host name is rejected, matching Claude Code's own rule for `ANTHROPIC_FOUNDRY_RESOURCE`. |
| `base_url` | Use instead of `resource` when you must give a full URL, for example `https://contoso-ai.services.ai.azure.com/anthropic`. It is set as `ANTHROPIC_FOUNDRY_BASE_URL`. |
| `auth` | `"entra"`: no key variables are set and Claude Code uses the Azure default credential chain (`az login`, managed identity and so on). `"api_key_env"`: the key is read from the variable named in `api_key_env`. |
| `api_key_env` | Name of the environment variable that holds the key (default `FOUNDRY_API_KEY`). The key itself is never written to any file. |
| `prompt_caching_1h` | `true` sets `ENABLE_PROMPT_CACHING_1H=1`, a one-hour cache instead of the default. Cache writes cost more with the 1-hour TTL. |

### 5.3 `tiers`: map tiers to your Foundry deployments

| Tier | Claude Code alias | Used by | Default model id |
|---|---|---|---|
| `fast` | `haiku` | the main session and `deep-haiku` | `claude-haiku-5-5` |
| `standard` | `sonnet` | `implementer` (and the Foundry judge) | `claude-sonnet-5-5` |
| `deep` | `opus` | `architect` | `claude-opus-5-5` |

The launcher sets `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL` and `ANTHROPIC_DEFAULT_OPUS_MODEL` from these values. Each field means:

- `model_id`: the real Claude model ID the deployment serves. Claude Code uses it to recognise the model, for example its context window.
- `deployment`: your Foundry deployment name. If it differs from `model_id`, the launcher writes `%USERPROFILE%\.claude-router\generated\foundry-settings.json` containing `modelOverrides` and passes it with `--settings`, which is how Claude Code maps a model to a differently named deployment.
- `deployment: null` (or `""`): that tier isn't available. Verdicts for it go to the nearest available tier, stronger first, otherwise weaker, and the alias points there too.

**Example A: deployments named after model IDs (simplest).** Keep the defaults and just set `foundry.resource`.

**Example B: your own deployment names.**

```json
"tiers": {
  "fast":     { "model_id": "claude-haiku-5-5",  "deployment": "haiku-prod" },
  "standard": { "model_id": "claude-sonnet-5-5", "deployment": "sonnet-prod" },
  "deep":     { "model_id": "claude-opus-5-5",   "deployment": "opus-prod" }
}
```

**Example C: older models on Foundry.** Use whatever your resource actually has; the router doesn't care which generation it is:

```json
"tiers": {
  "fast":     { "model_id": "claude-haiku-4-5", "deployment": "claude-haiku-4-5" },
  "standard": { "model_id": "claude-sonnet-5",  "deployment": "claude-sonnet-5" },
  "deep":     { "model_id": "claude-opus-4-8",  "deployment": "claude-opus-4-8" }
}
```

**Example D: no Opus deployment.** DEEP work goes to Sonnet via `implementer`, and the directive tells Haiku so.

```json
"deep": { "model_id": "claude-opus-5-5", "deployment": null }
```

**Example E: no Haiku deployment.** The main session then runs on your Sonnet deployment, so there are no savings, but routing still works.

### 5.4 `launcher`

| Field | Meaning |
|---|---|
| `max_retries` | Sets `CLAUDE_CODE_MAX_RETRIES` for the session; an inherited value, or `CLAUDE_CODE_RETRY_WATCHDOG`, never overrides it (section 7.6). Claude Code normally retries up to 10 times with growing delays, which is the "two-minute wait" on 429 errors. A value of 3 fails faster, so the `deep-haiku` fallback starts sooner. |
| `fallback_models` | Optional `--fallback-model` chain, for example `["sonnet", "haiku"]`. Claude Code uses it only when a model is **overloaded or unavailable**, not on rate limits (429). The switch lasts one turn and also applies to subagents. |
| `extra_args` | Extra arguments always passed to `claude`, for example `["--permission-mode", "plan"]`. |

### 5.5 `judge`

| Field | Meaning |
|---|---|
| `chain` | Ordered list of judges. The first one that returns a valid verdict wins. |
| `chain[].backend` | `"subscription"`: `claude -p` with all provider variables removed, so it uses your subscription login. `"foundry"`: `claude -p` on Foundry. `"rules"`: a local keyword classifier with no network. |
| `chain[].model` | Alias: `sonnet`, `opus`, `haiku` or `fable`. On Foundry it resolves through `tiers`. |
| `chain[].effort` | `low`, `medium`, `high`, `xhigh` or `max`. **`max` is the most thorough and the slowest**; see section 10. |
| `chain[].timeout_s` | Hard limit for that judge call, from 1 to 600. The judge process is also started with `CLAUDE_CODE_MAX_RETRIES=1`, so a throttled judge fails fast and the next one in the chain takes over. |
| `min_confidence` | If the model's confidence is below this value, the verdict moves **up** one tier ("when unsure, use the stronger model"). |
| `max_prompt_chars` | Longer prompts are cut off before they are sent to the judge. |
| `include_git_signals` | Adds the branch name and the count of uncommitted files to the judge input. Set `false` to send only the prompt and the folder name. |
| `sticky_phrases` | A prompt that is **exactly** one of these phrases reuses the previous verdict without calling the judge. Case, spaces and trailing punctuation don't matter. Any other prompt is judged, however short, so "redesign the auth schema" is judged. `[]` turns this off. |
| `escalate_phrases` | A prompt that is one of these phrases, or starts with one (up to 80 characters), moves the previous verdict **up one tier** without calling the judge: "still failing" after FAST gives STANDARD, after STANDARD gives DEEP. `[]` turns this off. |
| `claude_path` | Leave `""` to find `claude` on `PATH`. Set a full path if the hooks can't find it, for example `"C:/Users/you/.local/bin/claude.exe"`. |

The judge's instructions are in `%USERPROFILE%\.claude-router\router\judge_system.md`. Edit the **team-specific examples** at its end; this is the best way to make the judge match your code (section 9).

### 5.6 Common judge variants

Subscription judge at high effort, a good balance between speed and quality:

```json
"chain": [
  { "backend": "subscription", "model": "sonnet", "effort": "high", "timeout_s": 60 },
  { "backend": "rules" }
]
```

No subscription at all, everything stays on Azure:

```json
"chain": [
  { "backend": "foundry", "model": "sonnet", "effort": "high", "timeout_s": 45 },
  { "backend": "rules" }
]
```

No model judge, so instant and free, but much weaker:

```json
"chain": [ { "backend": "rules" } ]
```

After changing timeouts, run `python install.py install` again so the hook timeout covers the whole chain.

### 5.7 `guard`

| Field | Meaning |
|---|---|
| `main_edit_budget` | Total lines the **main Haiku thread** may change per prompt, by tier. Defaults: FAST 40, STANDARD 10, DEEP 0. Beyond that, edits are denied with a message telling Haiku to delegate. |
| `other_subagent_edit_lines` | Total lines per prompt that subagents other than `implementer`, `architect` and `deep-haiku` may change, together. Those other subagents, such as `general-purpose` and forks, run on Haiku. |

### 5.8 `logging`

| Field | Meaning |
|---|---|
| `enabled` | Writes one JSON line per prompt to `%USERPROFILE%\.claude-router\logs\decisions.jsonl`. |
| `log_prompt_text` | `false` (default) stores only a hash and the length of the prompt. `true` also stores the first 2,000 characters, which is useful for calibration but should only be used if that is allowed for your data. |
| `log_judge_details` | `false` (default) stores no text written by a model judge: its reason is replaced by a placeholder, and judge errors are stored as a short category. `true` keeps the judge's reason and the full error messages, which can quote the prompt. `test-judge` always shows the full detail on screen. |

---

### 5.9 `ui`

| Field | Meaning |
|---|---|
| `banner` | `"full"` (default): the framed banner in section 4.9. `"compact"`: one line. `"off"`: nothing, except warnings such as "no router hooks". Terminals narrower than 72 columns always get the one-line form. |
| `verdict_line` | `true` (default): after each prompt, Claude Code shows you the verdict, who does the work, the judge and its latency. `false`: nothing is shown; routing works the same. |

`python install.py` uses coloured ✓ / ! / ✗ marks on a colour terminal, and the plain `[ok]` / `[warn]` / `[fail]` tags otherwise (for logs and scripts).

---

## 6. Verify the installation

### 6.1 `python install.py doctor`

Each line starts with `[ok]`, `[warn]` or `[fail]`, and the command exits with code 1 if anything failed. It checks:
- the Python version;
- that the config is valid and the Foundry resource is set;
- the auth method (Azure CLI present, or the API key variable set);
- each tier's deployment;
- that `claude --version` runs;
- that no settings file sets provider variables;
- that the router hooks and the three agents are installed;
- organisation-managed settings, if any;
- Git Bash, on Windows.

Fix every `[fail]` first. A `[warn]` about `judge effort is max` is only a latency reminder.

### 6.2 `python install.py test-judge`

This runs **every judge in your chain once**, one after another, on a sample prompt about duplicate rows in a concurrent export, which should come out DEEP:

```
[ok]   subscription:sonnet (effort max, timeout 90s) tier=deep confidence=0.9 23.4s cost=$0.0211 reason=...
[ok]   foundry:sonnet (effort high, timeout 45s)     tier=deep confidence=0.85 9.8s cost=$0.0156 reason=...
[ok]   rules                                         tier=deep reason=rules: matched '...'
```

(The timings and costs above are illustrative only.) The subscription call uses your seat allowance; the Foundry call costs a few cents of Azure tokens. Test one backend with `--backend subscription` or `--backend foundry`, or your own prompt with `--prompt "..."`. A `[fail]` line shows the real error, for example "not logged in", a timeout, or a 429.

### 6.3 `claude-router --router-dry-run`

This prints the exact `claude` command and only the variables the launcher sets or removes, with secrets shown as `***`, without starting Claude Code. Variables you already had are not printed. Check:
- `CLAUDE_CODE_USE_FOUNDRY=1`;
- your resource or base URL;
- the three `ANTHROPIC_DEFAULT_*_MODEL` values;
- `CLAUDE_ROUTER=1`.

### 6.4 First live session

Inside `claude-router`:

1. `/status` should show the API provider **Microsoft Foundry** and your resource.
2. `/agents` should list `architect`, `implementer` and `deep-haiku`.
3. `/hooks` should list a `UserPromptSubmit` hook and a `PreToolUse` hook that point at `.claude-router`.
4. Try three prompts and then check the log:
   - `where is the main entry point of this project?` should be **FAST**: Haiku answers itself.
   - `add a --verbose flag to the CLI and a test for it` should be **STANDARD**: Haiku briefs `implementer`.
   - `::deep review how errors are handled in the export module` should be **DEEP** (forced): Haiku briefs `architect`.

   ```powershell
   python install.py report --last 5
   ```

   You should see one line per prompt with its tier and source.

---

## 7. How it works in detail

### 7.1 Components

| Piece | File | Runs when |
|---|---|---|
| Launcher | `router/launch.py`, called by `bin/claude-router*` | You start a session. It sets the Foundry variables, `CLAUDE_ROUTER=1` and `CLAUDE_CODE_MAX_RETRIES`, then runs `claude --model haiku`. |
| Judge hook | `router/judge_hook.py` (UserPromptSubmit) | Before Claude sees each prompt. |
| Guard hook | `router/guard_hook.py` (PreToolUse on `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Agent` and `Task`) | Before each matching tool call, in the main thread and in subagents. |
| Agents | `architect.md` (opus alias, high effort), `implementer.md` (sonnet, medium), `deep-haiku.md` (haiku, max) | When Haiku calls them with the Agent tool. |
| Judge instructions | `router/judge_system.md` | Passed to the judge with `--system-prompt-file`. |
| State | `.claude-router/state/<session>.json` | The tier and the lines used in the current prompt, by the main thread and by small-model subagents. It is reset as soon as a new prompt arrives, even if the judge then fails. Files older than 7 days are cleaned up. |
| Log | `.claude-router/logs/decisions.jsonl` | One line per prompt. |
| Install registry | `.claude-router/installs.json` | Which settings files have router hooks, so uninstall keeps the code while another scope still uses it. |
| Backups | `.claude-router/backups/` | A copy of each settings file before the router changes it. |
| Judge settings | `.claude-router/generated/judge-*-settings.json` | `disableAllHooks` (plus `modelOverrides` for the Foundry judge), passed with `--settings`. |

Both hooks do nothing unless `CLAUDE_ROUTER=1` is set, which only the launcher sets. They also do nothing inside the judge's own `claude -p` process, because `CLAUDE_ROUTER_JUDGE=1` is set there, so the judge can't trigger itself.

### 7.2 What happens for each prompt

0. **Background-agent notification:** Claude Code runs subagents in the background and reports their result as a prompt that starts with `<task-notification>`. That is not a new task, so it keeps the current verdict **and** the edit counters, and no judge is called. If it reports that an `architect` or `implementer` call of this prompt failed with a 429/529, `deep-haiku` becomes allowed (section 7.5).
1. **Override:** a prompt starting with `::fast`, `::standard` (or `::std`) or `::deep` sets the tier directly. No judge is called.
2. **Slash command:** a prompt that starts with a command name, such as `/review` or `/fix-issue 12`, keeps the previous tier, or gets STANDARD if there isn't one. A prompt that starts with a path, such as `/src/app.py has a race…`, is **not** a command and is judged normally.
3. **Escalation phrase** (`judge.escalate_phrases`, for example "still failing" or "try again"): the previous tier moves up one step. With no previous tier it becomes STANDARD.
4. **Follow-up phrase** (`judge.sticky_phrases`, for example "yes" or "continue"): the prompt must match a phrase exactly, and then it keeps the previous tier.
5. **Judge chain**, in the order configured. Each model judge runs:

   ```
   claude -p "Classify the coding task given on standard input. Reply with the JSON object only."
          --model sonnet --effort max --output-format json --max-turns 1
          --no-session-persistence --strict-mcp-config
          --setting-sources project,local --settings <generated/judge-subscription-settings.json>
          --disallowedTools "*" --system-prompt-file <judge_system.md>
   ```

   - The task text goes in on **standard input**, never as a command-line argument, so special characters in your prompt can't break the command.
   - **Isolation:**
     - `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` stops every CLAUDE.md file from loading.
     - `--setting-sources project,local` leaves out your user settings, and the judge runs in an empty folder (`.claude-router/judge-cwd`), so there are no project files either.
     - The `--settings` file sets `disableAllHooks`.
     - There are no MCP servers and no tools.
   - The **foundry** backend adds `--bare`, which also skips plugins and skills. Bare mode keeps reading Foundry credentials, but it **never reads the subscription login**, so the subscription backend can't use it.
   - It saves no session, so judge runs don't show up in `claude --resume`.
   - The **subscription** backend removes every `ANTHROPIC_*` and `CLAUDE_CODE_USE_*` variable first; the **foundry** backend sets them from your config. Both are started with `CLAUDE_CODE_MAX_RETRIES=1`.
   - **Timeout:** the judge reads and writes temporary files, not pipes. On timeout, the whole process tree is killed (`taskkill /T` on Windows, a process group on macOS and Linux), so an npm `claude.cmd` shim can't stretch the wait.
   - The answer must contain a JSON object with a valid `tier`. Anything else, including any unexpected error, counts as a failure, and the next judge in the chain runs.
6. **Confidence check:** if confidence is below `min_confidence`, the tier moves up one step.
7. **Availability check:** a tier without a deployment is replaced by the nearest available one.
8. The verdict is saved, the edit counters are reset to 0, a log line is written, and this **directive** is added to Haiku's context:
   - **FAST:** "Handle this yourself. Main-thread file edits are limited to 40 changed lines…"
   - **STANDARD:** "Delegate the implementation to the `implementer` subagent (or `architect` if it turns out to need design work)…"
   - **DEEP:** "Before answering or editing, delegate to the `architect` subagent… `implementer` is not accepted for this verdict. Main-thread file edits are blocked this turn."
   - **Always:** "never pass `model` on an Agent call to architect or implementer; never use general-purpose or a fork for this work; `deep-haiku` only after a 429/529 from architect or implementer, and say so; report results honestly; ::fast/::standard/::deep overrides exist." Any judge failures are listed too, so Claude can tell you about them.

Before any of this, the hook writes a fresh **provisional** state: STANDARD, 0 lines used. So even if something unexpected fails, the guard never applies the previous prompt's verdict or line count to this prompt, and Claude still gets a STANDARD directive.

### 7.3 What the guard enforces

**Who is acting.** A hook input with an `agent_id` comes from a subagent, and `agent_type` says which one. Without `agent_id` it is the main session, even when you started it with `claude-router --agent NAME` (Claude Code then sends that name as `agent_type`). A named main session therefore gets the main session's budget and delegate rules, not a subagent's.

| Situation | Result |
|---|---|
| Main thread, total lines this prompt within the tier budget | allowed, and counted |
| Main thread, over budget (DEEP: any edit at all) | **denied**, with "Delegate with the Agent tool to `implementer` or `architect`…" |
| Inside `implementer`, `architect` or `deep-haiku` | allowed, no limit |
| Inside any other subagent (`general-purpose`, a fork, `Explore`, your own agents), once all of them together pass 20 lines this prompt | **denied**, because those run on Haiku |
| Agent call to `implementer` or `architect` with a `model` parameter, from anywhere | **denied**: Claude Code would otherwise honour that override over the agent's pinned model |
| Any Agent call that doesn't match the delegate rules below | **denied**, with the agent to use instead |

Delegate rules (the same table is in `router/guard_hook.py`):

| Delegate | Main session, FAST | Main session, STANDARD | Main session, DEEP | From inside a subagent |
|---|---|---|---|---|
| `architect` | yes | yes | yes | yes |
| `implementer` | yes | yes | **no** (use `architect`) | yes (so `architect` can hand off subtasks) |
| `deep-haiku` | yes | only after a 429/529 | only after a 429/529 | only after a 429/529 |
| `general-purpose`, or a fork (no `subagent_type`) | yes | **no** | **no** | yes (its edits share the 20-line limit) |
| any other agent, such as `Explore` | yes | yes | yes | yes |

"After a 429/529" means: an `architect` or `implementer` call **of the current prompt** failed with a rate-limit or overloaded error (section 7.5).
| `Bash`, `Read` and other tools | not inspected (see section 12) |

How lines are counted (every call counts **at least 1**, so DEEP blocks even an empty write):

| Tool | Counted lines |
|---|---|
| Edit | the larger of the old and new text. With `replace_all`, that number times the occurrences in the file, matched the way Claude Code matches them (so a file with Windows line endings counts every match); an unreadable file counts as "too large". |
| Write | the larger of the new content and **the file it replaces**, so overwriting a 300-line file with 1 line counts 300 |
| MultiEdit | the sum over its edits, with `replace_all` handled as for Edit |
| NotebookEdit | replace: the larger of the new source and the existing cell. Insert: the new cell only (the cell it is inserted after stays as it is). Delete: the deleted cell. |

### 7.4 Enforced versus advisory

| Behaviour | Enforced by code? |
|---|---|
| The judge runs on every new prompt | Yes |
| No large edits in the main thread when the tier says delegate | Yes (guard) |
| `architect` and `implementer` keep their pinned models | Yes (guard) |
| When Haiku delegates, the agent matches the verdict (the delegate rules in section 7.3) | Yes (guard) |
| `deep-haiku` is used only after a 429/529 from `architect`/`implementer` | Yes (guard and judge hook, matched by tool-use ID, see section 7.5) |
| Haiku actually calls `architect` for a DEEP question that needs no edit | **No.** It's a directive in the context, and Haiku usually follows it. Use `::deep` when it matters. |
| Haiku switches to `deep-haiku` after a 429 or 529 | **No.** It's a directive; the shorter `max_retries` makes the error appear sooner. |
| File changes made through Bash (`sed`, redirection, scripts) | **No** |

### 7.5 Rate limits (429), overloaded errors (529) and fallbacks

- The **judge** is started with `CLAUDE_CODE_MAX_RETRIES=1` and a hard timeout. If the subscription is throttled, the Foundry judge runs next, then the rules.
- The **session** uses `max_retries` (default 3) instead of Claude Code's 10, so a throttled subagent errors in seconds rather than minutes. Haiku is then told to rerun the brief with `deep-haiku` and to say so. The guard accepts `deep-haiku` only after a 429/529 failure of an `architect` or `implementer` call of this prompt:
  1. When the guard allows that call, it records the call's `tool_use_id`.
  2. Claude Code reports the background agent's result as a `<task-notification>` naming the same tool-use ID. The judge hook receives it before Haiku does, and marks that call as throttled only if the status is **failed** and the error text contains `429`, `529`, "rate limit" or "overloaded". The ID and the output-file path in the notification are ignored, so a project folder such as `issue-429` never counts.
  3. For agents that run synchronously, an **error** `tool_result` for that ID in the transcript also counts.

  The recorded IDs start empty with every new prompt, and the transcript check looks only at calls made after your latest prompt, so a 429 from an earlier prompt never unlocks the fallback. A successful result that merely mentions rate limits never counts, and neither does text anywhere else, such as the router's own directive, which mentions "429". This was verified end to end with the real Claude Code (section 12). If the guard refuses although the model really is throttled, start the next prompt with `::fast`.
- **Foundry 429s** come from your deployment's quota (tokens or requests per minute). Raising the quota in Azure is the real fix.
- `fallback_models` helps only with **overloaded or unavailable** errors; Claude Code never switches models because of a rate limit.

### 7.6 Environment policy

The router decides these variables for every Claude Code process it starts. An inherited value never changes the routing:

| Variables | Main session (launcher) | Judge on Foundry | Judge on subscription |
|---|---|---|---|
| `ANTHROPIC_*`, `CLAUDE_CODE_USE_*`, `CLAUDE_CODE_SUBAGENT_MODEL(_FORCE)`, `AWS_BEARER_TOKEN_BEDROCK` | removed, then the Foundry values are set | removed, then the Foundry values are set | removed |
| `CLAUDE_CODE_MAX_RETRIES`, `CLAUDE_CODE_RETRY_WATCHDOG` | removed; `CLAUDE_CODE_MAX_RETRIES` = `launcher.max_retries` | removed; 1 retry | removed; 1 retry |
| `CLAUDE_CODE_EFFORT_LEVEL`, `MAX_THINKING_TOKENS` | kept (your choice for the session) | removed; effort from `judge.chain` | removed; effort from `judge.chain` |
| `CLAUDE_CODE_OAUTH_TOKEN` | removed | removed | **kept**: it is a subscription login in token form (`claude setup-token`), and Claude Code uses it instead of the stored login |
| everything else | kept | kept | kept |

Settings files can also set variables in an `env` block, and those override the shell. `doctor` warns about provider variables there.

**`--settings`.** Claude Code reads only one `--settings` argument. When you pass your own (`claude-router --settings my.json`, or in `launcher.extra_args`), the launcher merges it with the router's deployment mapping (`modelOverrides`) into one file, so your settings can never send requests to the raw model ID instead of your deployment. If your file maps one of the router's model IDs differently, the router's mapping wins and the launcher says so.

---

## 8. Daily use

| Want to | Do |
|---|---|
| Start with routing | `claude-router` (plus any normal claude arguments) |
| Start without routing | `claude` (your normal subscription session; the hooks do nothing) |
| Force a tier for one prompt | Start the prompt with `::fast`, `::standard` or `::deep` |
| Keep the tier for a follow-up | Reply with exactly "yes", "ok", "continue", "go ahead"… (`judge.sticky_phrases`) |
| Move up a tier after a failed attempt | Reply "still failing", "try again", "didn't work"… (`judge.escalate_phrases`) |
| See recent decisions | `python install.py report --last 20` (run from the unzipped folder) |
| Check the setup after changes | `python install.py doctor` |
| Test the judges after an Azure or login change | `python install.py test-judge` |

---

## 9. Tuning and calibration

0. **Measure first.** `python install.py eval` runs the judge chain over a labeled set of prompts and prints a confusion matrix, the number of **under-routed** prompts (judged weaker than labeled: the costly mistake) and **over-routed** ones (slower and more expensive), latency and reported cost. It makes one real judge call per prompt. The shipped set (`tests/eval/routing_corpus.jsonl`, 30 synthetic prompts) is a start; copy it, add sanitized prompts from your own work with the tier you expect, and run `eval --corpus your-file.jsonl` after every change to `judge_system.md` or the judge model.
1. **Collect data for one or two weeks** with the defaults. If your data policy allows it, set `logging.log_prompt_text` to `true` while calibrating.
2. **Review** `python install.py report --last 50`. For each prompt, ask whether the tier was right, and write down the misses.
   - **Under-routing**, where a hard task got FAST, is the costly error: it costs you quality.
   - **Over-routing**, where an easy task got DEEP, costs money and time.
3. **Teach the judge:** add 5–15 real examples of misses to the *Team-specific examples* list at the end of `judge_system.md`, in the same one-line format. Remember that reinstalling copies the packaged version over your installed `judge_system.md`, so keep your edited copy in the unzipped folder and edit it there.
4. **Adjust thresholds:**
   - raise `min_confidence` to route up more often, or lower it to route up less;
   - lower `main_edit_budget.fast` if Haiku does too much itself.
5. **Check the simple alternative.** Run a day with everything on Sonnet at low effort (plain `claude --model sonnet --effort low` on Foundry) and compare cost per finished task. If it's similar, the router isn't paying for itself on your work.

---

## 10. Cost and latency

These are rough estimates; measure your own with `test-judge` and `report`.

- **Subscription judge.** Every judged prompt is one small `claude -p` call: a short system prompt, your prompt and a short JSON answer. It counts against your subscription's usage allowance (for Team and Enterprise seats this resets in five-hour and weekly windows and is shared with Claude chat). The `cost` that `claude -p` reports for subscription calls is notional, not an invoice.
- **Latency.** Each judged prompt waits for `claude` to start (a second or two) plus the model's thinking time. **At `max` effort that can be tens of seconds.** Short follow-ups, slash commands and `::` overrides skip the judge entirely. If it feels slow, use `effort: "high"`; for a classification task the verdict often doesn't change.
- **Foundry.** You pay Azure per token for the main Haiku session (cheap), for `implementer` on Sonnet and `architect` on Opus (the expensive part), and for the fallback judge when it runs. Each subagent starts with an empty context and re-reads the files it needs, so a delegation is not free.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No `[router]` banner, and `/status` shows your subscription instead of Foundry | You started plain `claude` | Start with `claude-router` |
| `claude-router` is not recognised | The `bin` folder isn't on PATH | Section 4.7, then open a new terminal |
| PowerShell: "running scripts is disabled" | Execution policy blocks `.ps1` | Run `claude-router.cmd`, or delete `claude-router.ps1` |
| `Failed to get token from azureADTokenProvider: ChainedTokenCredential authentication failed` | No Azure credential | Run `az login` (with the right `--tenant`), or switch to `auth: "api_key_env"` |
| "Switched to … because … is not available", or "model not found" | Deployment name or model ID mismatch | Fix `tiers` (section 5.3); check deployment names in the portal; `claude-router --router-dry-run` shows what is being used |
| Every verdict comes from `rules` and the log shows judge errors | The subscription judge fails | Run `python install.py test-judge --backend subscription` to see the real error. Usually you aren't logged in (step 4.3), the hooks can't find `claude` (set `judge.claude_path`), or it times out (lower the effort or raise `timeout_s` and reinstall) |
| The judge works but every prompt is slow | Max effort | `effort: "high"`, or a lower `timeout_s` so the fallback starts sooner |
| `/hooks` shows nothing | Wrong scope or settings file, or the session was started before installing | Re-run install for the right scope and restart `claude-router`; check that `doctor` reports "router hooks installed" |
| `claude-router` warns "no router hooks in …", and the log shows no decisions | `CLAUDE_CONFIG_DIR` points Claude Code at a different profile from the one you installed into | Run `python install.py install` and `doctor` with the same `CLAUDE_CONFIG_DIR` you use for `claude-router` |
| Install stops: "… already exist(s) and was not installed by the router" | You have your own `architect`, `implementer` or `deep-haiku` agent | Rename it, or `python install.py install --force-agents` (your file is backed up). `doctor` checks the agent Claude Code actually loads, including a project's `.claude/agents` |
| `/hooks` shows the hooks but they never fire, or show errors | Your Claude Code version doesn't run exec-form hooks | `claude update`, or reinstall with `--hook-style bash` (Git Bash) or `--hook-style powershell` |
| `doctor` warns about organisation-managed settings | IT policy can restrict hooks, providers or login methods | Ask IT whether user hooks and the Foundry provider are allowed |
| "Routing is OFF … configuration is invalid" | JSON error or a wrong value in the config | `python install.py doctor` prints the exact field |
| Edits denied: "…subagents running on the small model…" | Working as designed: `general-purpose`, forks and other agents run on Haiku | Let Haiku delegate to `implementer` or `architect` |
| "`general-purpose` runs on your own small model. Delegate to …" | Working as designed for STANDARD and DEEP (section 7.3) | Nothing to do; Haiku retries with the agent named. Research agents such as `Explore` are allowed. |
| "The router verdict for this prompt is DEEP: delegate to `architect`, not `implementer`" | Working as designed | Nothing to do; Haiku retries with `architect`. To force Sonnet for one prompt, start it with `::standard`. |
| "`deep-haiku` is only for after … failed with a rate-limit (429) or overloaded (529) error" | No `architect`/`implementer` call of this prompt has failed with a 429/529 yet | Let Haiku call `architect` or `implementer` first. If you know the model is throttled, start the next prompt with `::fast`. |
| Every verdict comes from `rules` and the error says "unknown option" (`--setting-sources`, `--settings`, `--effort`, `--bare`…) | Claude Code is older than the flags the judge uses | `claude update`, then `python install.py test-judge` |
| You deleted `.claude-router` by hand and Claude Code now runs without routing | Working as designed: the hooks do nothing when the router code is missing, rather than blocking you | `python install.py install` again, or `python install.py uninstall` to remove the leftover hook entries |
| Uninstall says "router code kept: still installed for …" | Another scope still uses the router | Uninstall that scope too, or use `--purge` to remove every scope at once |
| You need to see exactly what Claude Code sends to the hooks | - | Start with the environment variable `CLAUDE_ROUTER_DEBUG_DIR=<folder>`: every hook input is saved there as a JSON file. It contains your prompts, so delete the folder afterwards. |
| Haiku answers a DEEP question itself without editing | The directive is advisory (section 7.4) | Prefix the prompt with `::deep`; add examples to `judge_system.md` |
| Foundry 429 on Sonnet or Opus | Deployment quota | Raise the quota in Azure. Meanwhile `deep-haiku` takes over and says so. |
| The judge prompt shows up in `claude --resume` | A very old Claude Code without `--no-session-persistence` | `claude update` |

---

## 12. Limits and known gaps

Read these honestly before relying on the router.

1. **What has been proven, and how.**
   - **106 unit tests** with a fake `claude`, covering the hook contracts, environment separation, fallbacks, the guard's counting and agent rules, parallel hooks and installs, the installer, and malformed and non-ASCII input. 27 of them are regressions from the first code review (24 fail on version 1.0.0; the other 3 guard new code or a Windows-only behaviour), 3 come from the end-to-end runs (2 fail on 1.1.0, 1 on 1.2.0), and 21 cover the findings of the second review (version 1.3.0).
   - **An end-to-end test** (`tests/e2e/run_e2e.py`) that runs the **real Claude Code CLI** through the installed `claude-router` launcher against a local mock of the Anthropic/Foundry API, with no model, account or network. It checks 34 things across 7 scenarios (install, DEEP, FAST, 429 fallback, named session, parallel calls, safety): that the judge reaches the Sonnet deployment with `judge_system.md` as its only system prompt, no tools and its configured effort (although the shell sets `CLAUDE_CODE_EFFORT_LEVEL=low`), the main session on the Haiku deployment (also when you pass your own `--settings`), the injected verdict, denied and allowed edits, DEEP refusing `implementer`, `architect` on the Opus deployment, the 429 → `deep-haiku` fallback within the retry limit (although the shell sets `CLAUDE_CODE_MAX_RETRIES=20` and `CLAUDE_CODE_RETRY_WATCHDOG=1`), a main session started with `--agent implementer` keeping the main-session budget, three parallel `architect` calls all recorded, an untouched plain `claude`, and missing router code not blocking Claude Code. The project's CI runs it on every push on Linux, macOS and Windows with Claude Code 2.1.327, and weekly against the latest Claude Code; each run's summary has the full report.
   - **Not proven:** a real Foundry resource, a real subscription judge call in CI (the runners have no login, so the test asserts that step fails cleanly), real interactive use, and **routing quality** on your own work: the mock scripts the verdicts, so the end-to-end test proves the plumbing, not the judge's accuracy. `python install.py eval` measures that (section 9). Your first real run is the final check; that is what sections 4.8 and 6 are for.
2. **Windows-specific code** (the process-tree kill on judge timeout, the launcher's Ctrl+C handling, exact line endings) is exercised by the Windows CI jobs, not by hand. Check the CI results for your Claude Code version.
3. **The judge isolation flags** (`--setting-sources`, `--settings` with `disableAllHooks`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `--bare` for Foundry) are taken from the Claude Code documentation. Nobody has watched them in a live judge run. If your Claude Code rejects one, the judge fails visibly and the rules take over (section 11).
4. **Subscription login and Foundry on one machine:** Anthropic's documentation doesn't say how they coexist. The design keeps them apart: Foundry variables exist only in the launcher's process tree and are removed for the judge, and nothing goes into settings files. `test-judge` confirms that it works on your machine.
5. **Bash edits are not inspected.** Haiku can still change files with shell commands.
6. **Delegation without edits is advisory.** For a DEEP *question*, Haiku is told to ask `architect` but isn't forced to. Which agent it delegates to *is* enforced.
7. **The `deep-haiku` check depends on Claude Code's notification format** (`<task-notification>` with `<tool-use-id>` and the API error text, as written by Claude Code 2.1.327). If a future version words it differently, the fallback is refused and Haiku tells you; `::fast` gets around it, and the end-to-end test will show the change.
8. **Prompt-only judging misses tasks that turn out to be hard partway through.** Escalation phrases ("still failing") and the edit budgets limit the damage, but the judge only ever sees the prompt.
9. **Parallel hooks are serialised by a per-session lock.** If a hook can't get the lock within 5 seconds (a stuck process), it goes on without it rather than hang your session; the counters can then be slightly off for that prompt.
10. **Exec-form hooks need a recent Claude Code.** Other hook styles are available (section 4.5).
11. **The judge is a model and can be wrong.** It is told to treat the task text as data, but a prompt can still sway it. That's harmless for a single user, since the worst case is a wrong tier.
12. **Same-family review:** results from `architect` (Opus) on work done by Haiku come from the same model family, so treat them as a second opinion, not as an independent verification. The code review behind version 1.1.0 was also done by the model family that wrote the code; the findings fixed in 1.3.0 came from an external review.

---

## 13. Uninstall

```powershell
python install.py uninstall                      # user scope; keeps config, logs and backups
python install.py uninstall --scope project --project C:\src\your-repo
python install.py uninstall --purge              # EVERY recorded scope, then deletes %USERPROFILE%\.claude-router
```

Uninstall:
- removes only hook entries that point at this router's scripts, by full path, so your own hooks survive even if they have the same file names;
- removes only agent files that still carry the router marker;
- removes the router block from `.git/info/exclude` for project scopes;
- backs up each settings file to `.claude-router\backups\` before changing it.

The router code is deleted only when **no** settings file still uses it. If you uninstall one project but keep the user scope, the code stays and the output says so. `--purge` uninstalls every scope in `installs.json` (and the user scope) first, then deletes the whole folder, including the backups.

Remove `.claude-router\bin` from your PATH yourself.

---

## 14. File reference

```
model-router/
  README.md                      overview
  docs/SETUP.md                  this guide
  docs/assets/                   README images (generated by scripts/build_assets.py)
  CHANGELOG.md  LICENSE  CONTRIBUTING.md  SECURITY.md  SUPPORT.md  CODE_OF_CONDUCT.md
  install.py                     install / doctor / test-judge / report / uninstall
  router/
    common.py                    config, tiers, environments, state, logging
    judge_hook.py                UserPromptSubmit hook (judge chain, directive)
    guard_hook.py                PreToolUse hook (edit budgets, agent-vs-verdict, pinned models)
    launch.py                    claude-router launcher (Foundry environment)
    ui.py                        launch banner, per-prompt verdict line, coloured installer output
    judge_system.md              judge instructions and team examples (edit me)
    router.config.example.json   default configuration
  agents/
    architect.md                 opus alias, effort high
    implementer.md               sonnet alias, effort medium
    deep-haiku.md                haiku alias, effort max (429/529 fallback)
  tests/
    fake_claude.py               stand-in for the claude CLI (no network)
    test_router.py               106 unit tests (test_review_* / test_review2_* = code review regressions)
    eval/routing_corpus.jsonl    labeled synthetic prompts for `install.py eval`
    e2e/
      mock_anthropic.py          local mock of the Anthropic / Foundry Messages API
      run_e2e.py                 end-to-end proof with the real Claude Code CLI
  scripts/
    build_bundle.py              offline zip + SETUP.md with full sources (for email)
    build_assets.py              README images, light and dark
  .github/workflows/             ci.yml (unit tests), e2e.yml (end-to-end), release.yml
```

After installation:

```
%USERPROFILE%\.claude-router\
  router.config.json             your configuration
  installs.json                  which settings files have router hooks
  router\                        installed code (overwritten by re-install)
  bin\claude-router.cmd|.ps1|    launchers
  generated\foundry-settings.json          modelOverrides (only if deployment names differ)
  generated\judge-*-settings.json          disableAllHooks (+ modelOverrides) for the judge
  backups\                       copies of settings files from before each change
  state\  logs\  judge-cwd\
%USERPROFILE%\.claude\agents\{architect,implementer,deep-haiku}.md
%USERPROFILE%\.claude\settings.json
```

---
