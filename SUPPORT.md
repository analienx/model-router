# Getting help

1. **Run `python install.py doctor`.** It checks Python, the config, Foundry authentication, the deployments, `claude`, the hooks and the agents, and explains each problem it finds.
2. **Check the [troubleshooting table](docs/SETUP.md#11-troubleshooting)** in the setup guide. It covers the most common install and routing problems.
3. **See what the router decided** with `python install.py report --last 20`. For a deeper look, set `CLAUDE_ROUTER_DEBUG_DIR` to save every hook input ([guide, section 11](docs/SETUP.md#11-troubleshooting)). These files contain your prompts, so don't attach them to a public issue.
4. **Still stuck?** Open a [bug report](https://github.com/analienx/model-router/issues/new?template=bug_report.yml) and include the `doctor` output, your OS, and your Claude Code and Python versions. Remove resource names and anything private first.

For ideas, use a [feature request](https://github.com/analienx/model-router/issues/new?template=feature_request.yml). For security problems, follow [SECURITY.md](SECURITY.md) instead of opening an issue.
