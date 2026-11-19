#!/usr/bin/env python3
"""Build the offline bundle for machines you can only reach by email or USB.

    python scripts/build_bundle.py [--out dist]

Creates:
  <out>/claude-model-router-<version>.zip   everything needed to install, plus SETUP.md
  <out>/SETUP-offline.md                    the guide + the full source of every file with SHA-256 checksums,
                                            for mail systems that block .zip / .py attachments
Standard library only.
"""
import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "README.md", "LICENSE", "CHANGELOG.md", "install.py",
    "router/common.py", "router/judge_hook.py", "router/guard_hook.py", "router/launch.py", "router/ui.py",
    "router/judge_system.md", "router/router.config.example.json",
    "agents/architect.md", "agents/implementer.md", "agents/deep-haiku.md",
    "tests/fake_claude.py", "tests/test_router.py", "tests/e2e/mock_anthropic.py", "tests/e2e/run_e2e.py",
    "tests/eval/routing_corpus.jsonl",
]
ZIP_ONLY = ["docs/assets/%s" % n for n in ("banner-light.svg", "banner-dark.svg", "how-it-works-light.svg",
                                           "how-it-works-dark.svg", "session.svg")]  # README images, not in the appendix
LANG = {".py": "python", ".md": "markdown", ".json": "json", "": "text"}
FENCE = "`" * 6  # built, never written literally, so no source file can contain it by accident
APPENDIX_TITLE = "## 15. Appendix: full source of every file"
APPENDIX_INTRO = (
    "Use this section only if you can't transfer the zip. Create each file at the path shown, with exactly this "
    "content, saved as UTF-8 with LF line endings. To check a file afterwards, run `certutil -hashfile <file> SHA256` "
    "on Windows, or `sha256sum <file>` on macOS and Linux, and compare with the table.")


def version():
    m = re.search(r'VERSION = "([^"]+)"', (ROOT / "router" / "common.py").read_text(encoding="utf-8"))
    if not m:
        sys.exit("VERSION not found in router/common.py")
    return m.group(1)


def _add(z, arcname, data):
    """Reproducible entry: fixed timestamp and permissions, so the same source always gives the same zip bytes."""
    info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3                # "Unix", whatever OS builds it
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def offline_setup():
    guide = (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8").rstrip() + "\n"
    guide = guide.replace("](../CHANGELOG.md)", "](CHANGELOG.md)").replace("](../", "](")
    guide = guide.replace("14. [File reference](#14-file-reference)\n",
                          "14. [File reference](#14-file-reference)\n"
                          "15. [Appendix: full source of every file](#15-appendix-full-source-of-every-file)\n", 1)
    out = [guide.rstrip(), "", "---", "", APPENDIX_TITLE, "", APPENDIX_INTRO, "", "| File | SHA-256 |", "|---|---|"]
    bodies = []
    for name in FILES:
        data = (ROOT / name).read_bytes()
        if b"\r" in data:
            sys.exit("%s has CR characters; the repository must use LF line endings" % name)
        text = data.decode("utf-8")
        if FENCE in text:
            sys.exit("%s contains a %d-backtick fence; pick a longer FENCE in build_bundle.py" % (name, len(FENCE)))
        out.append("| `%s` | `%s` |" % (name, hashlib.sha256(data).hexdigest()))
        bodies += ["", "### `%s`" % name, "", FENCE + LANG.get(Path(name).suffix, "text"), text.rstrip("\n"), FENCE]
    return "\n".join(out + bodies) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ver = version()
    setup = offline_setup()
    (out / "SETUP-offline.md").write_bytes(setup.encode("utf-8"))
    top = "claude-model-router-%s" % ver
    zpath = out / ("%s.zip" % top)
    with zipfile.ZipFile(str(zpath), "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES + ZIP_ONLY:
            _add(z, "%s/%s" % (top, name), (ROOT / name).read_bytes())
        _add(z, "%s/SETUP.md" % top, setup.encode("utf-8"))
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    sys.stdout.write("built %s (%d files, sha256 %s)\nbuilt %s\n" % (zpath, len(FILES) + len(ZIP_ONLY) + 1, digest, out / "SETUP-offline.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
