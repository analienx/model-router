# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead: go to the repository's **Security** tab and choose **Report a vulnerability**. If that option isn't available, open an issue that only asks for a private contact, without any details.

You will get an answer within a week. Fixes are released as a new version, with credit unless you prefer otherwise.

## What the router handles

- **Where data goes.**
  - The judge sends each new prompt (up to 6,000 characters), the folder name, the previous verdict's tier (one word, never its reason) and optionally the git branch and the count of uncommitted files to Anthropic, through your Claude subscription. If that judge fails, the same goes to the Foundry judge. Everything else goes wherever your Claude Code session goes (Azure Foundry).
  - The judge is started with CLAUDE.md loading, user settings, hooks, MCP servers and tools all switched off.
  - See [the setup guide, section 2](docs/SETUP.md#2-read-this-before-installing-where-your-data-goes).
- **Secrets.** The Foundry API key is read from an environment variable that you name, and is never written to a file or a log. `--router-dry-run` masks it.
- **Logs.** `~/.claude-router/logs/decisions.jsonl` stores a hash and the length of each prompt, not the text, unless you set `logging.log_prompt_text`. It also stores no text written by a model judge (reasons, raw error messages), only error categories, unless you set `logging.log_judge_details`. Session state files keep the current verdict and its reason for at most 7 days. The full inventory is in [the setup guide, section 2](docs/SETUP.md#2-read-this-before-installing-where-your-data-goes).
- **Child environments.** Every Claude Code process the router starts gets an environment built by one policy ([guide, section 7.6](docs/SETUP.md#76-environment-policy)): provider, credential, model and retry variables are removed, and only the subscription judge keeps a `CLAUDE_CODE_OAUTH_TOKEN`.
- **Debug dumps.** `CLAUDE_ROUTER_DEBUG_DIR` is off by default. When you turn it on, it saves full hook inputs, including prompts, to the folder you choose.

## Out of scope

The router limits what the small model can do with Claude Code's edit tools. It is **not a sandbox**: commands run through Bash are not inspected. Use Claude Code's own permission settings for that.
