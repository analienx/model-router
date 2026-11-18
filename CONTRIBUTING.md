# Contributing

Thanks for helping. Bug reports with the output of `python install.py doctor` and `python install.py report --last 10` are the most useful contributions. Remove anything private before posting.

## Development setup

No dependencies: Python 3.8+ and the standard library only. Clone the repository and you are ready.

```bash
python -m unittest discover -s tests -v       # 79 unit tests, ~10 s, no network
```

### End-to-end test with the real Claude Code

```bash
npm install -g @anthropic-ai/claude-code@2.1.327
python tests/e2e/run_e2e.py                    # add --subscription-judge skip if you are logged in to Claude
```

The end-to-end test installs the router into a temporary home folder and runs the real `claude` CLI against `tests/e2e/mock_anthropic.py`, a local mock of the Anthropic/Foundry API. Child processes get a minimal allow-listed environment, so API keys and cloud tokens in your shell are not passed on.

**If your machine has a Claude login, pass `--subscription-judge skip`.** The test's first judge step deliberately calls `claude -p` with your normal login, expecting it to fail on CI runners. On a logged-in machine it would make a real model call.

Useful flags:
- `--keep`: keep the temporary folder for inspection;
- `--only deep,fast`: run only some scenarios;
- `--debug-hooks`: save every hook input;
- `--artifacts DIR`: copy the report and logs to `DIR`.

## Rules for changes

- **Every bug fix ships with a test that fails without the fix.** Name review or e2e regressions `test_review_*` / `test_e2e_*`.
- **Standard library only**, and Python 3.8 syntax (no `match`, no `X | Y` type unions, no `str.removeprefix`).
- **Hooks must never break a Claude Code session.** Unexpected input or errors end in a safe default (exit 0, a clear directive), never a traceback with a blocking exit code.
- **New readers of config, state, transcripts or settings need a malformed-input test.** Valid JSON with the wrong shape must not crash.
- **Text subprocesses pin `encoding="utf-8"`**, and files are written with `write_exact` / `atomic_write_text`, never `Path.write_text`. Source rules in the tests enforce both.
- **Keep the docs honest.** If behaviour changes, update `docs/SETUP.md` and `CHANGELOG.md` in the same pull request. Say what is proven and what is not.

## Pull requests

Fill in the template. CI must be green on all three operating systems. For changes to how the router talks to Claude Code, include the relevant part of the end-to-end report.
