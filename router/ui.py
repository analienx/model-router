"""Terminal graphics for claude-model-router: the launch banner, the per-prompt verdict line and status symbols.

Standard library only. Degrades gracefully:
  - colour only on a terminal, never when NO_COLOR is set (https://no-color.org), forced with FORCE_COLOR=1;
  - Unicode box drawing only when the stream can encode it, else plain ASCII;
  - Windows: switches on ANSI processing for the console, or falls back to no colour.
"""
import os
import re
import shutil
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 256-colour codes close to the README palette: teal / indigo / orange for the three tiers.
TIER_COLOR = {"fast": "38;5;43", "standard": "38;5;105", "deep": "38;5;209"}
STYLE = {"bold": "1", "dim": "2", "ok": "38;5;71", "warn": "38;5;214", "fail": "38;5;203", "accent": "38;5;111"}
TIER_WORK = {"fast": "Haiku handles it", "standard": "implementer (Sonnet)", "deep": "architect (Opus)"}

BOX_UNICODE = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
               "ml": "├", "mr": "┤", "dot": "◆", "arrow": "→", "sep": "·",
               "ok": "✓", "warn": "!", "fail": "✗", "todo": "→", "le": "≤"}
BOX_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|", "ml": "+", "mr": "+", "dot": "*",
             "arrow": "->", "sep": "|", "ok": "ok", "warn": "!!", "fail": "XX", "todo": "->", "le": "<="}


def _enable_windows_ansi(stream):
    """Turn on virtual-terminal processing for a Windows console. False if that isn't possible."""
    try:
        import ctypes
        import msvcrt
        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


class Painter(object):
    """Colours and glyphs for one output stream."""

    def __init__(self, stream=None, environ=None, color=None, unicode=None):
        self.stream = stream if stream is not None else sys.stderr
        env = os.environ if environ is None else environ
        if color is None:
            if env.get("NO_COLOR"):
                color = False
            elif env.get("FORCE_COLOR") not in (None, "", "0"):
                color = True
            else:
                try:
                    color = self.stream.isatty() and env.get("TERM") != "dumb"
                except (AttributeError, ValueError, OSError):
                    color = False
                if color and os.name == "nt":
                    color = _enable_windows_ansi(self.stream)
        if unicode is None:
            enc = getattr(self.stream, "encoding", None) or "ascii"
            try:
                "".join(BOX_UNICODE.values()).encode(enc)
                unicode = True
            except (LookupError, UnicodeEncodeError):
                unicode = False
        self.color = bool(color)
        self.g = BOX_UNICODE if unicode else BOX_ASCII

    def paint(self, text, *styles):
        if not self.color or not styles:
            return text
        codes = ";".join(TIER_COLOR.get(s) or STYLE.get(s, s) for s in styles)
        return "\x1b[%sm%s\x1b[0m" % (codes, text)

    def tier(self, tier, text=None):
        return self.paint(text if text is not None else tier.upper(), "bold", tier)

    def status(self, level):
        """Symbol for [ok] / [warn] / [fail] / [todo] lines."""
        sym = self.g.get(level, level)
        return self.paint(sym, "bold", {"todo": "accent"}.get(level, level))


def visible_len(text):
    return len(ANSI_RE.sub("", text))


def pad(text, width):
    return text + " " * max(0, width - visible_len(text))


def clip(text, width):
    return text if len(text) <= width else text[:max(1, width - 1)] + "~"


def box(lines, p, title=None, footer=None, max_width=None):
    """Draw `lines` (rows of text; None = divider) inside a rounded box."""
    g = p.g
    body = [l for l in lines if l is not None]
    width = max([visible_len(l) for l in body] + [visible_len(title or "")])
    if max_width:
        width = min(width, max_width - 4)
    out = [g["tl"] + g["h"] * (width + 2) + g["tr"]]
    if title:
        out.append("%s %s %s" % (g["v"], pad(title, width), g["v"]))
        out.append(g["ml"] + g["h"] * (width + 2) + g["mr"])
    for l in lines:
        if l is None:
            out.append(g["ml"] + g["h"] * (width + 2) + g["mr"])
        else:
            out.append("%s %s %s" % (g["v"], pad(l, width), g["v"]))
    out.append(g["bl"] + g["h"] * (width + 2) + g["br"])
    if footer:
        out.append("  " + footer)
    return out


def judge_chain_text(cfg, p):
    steps = []
    for s in cfg["judge"]["chain"]:
        if s["backend"] == "rules":
            steps.append("rules")
        else:
            steps.append("%s %s%s" % (s["backend"], s["model"], " (%s)" % s["effort"] if s.get("effort") else ""))
    return (" %s " % p.g["arrow"]).join(steps)


def banner(cfg, version, endpoint, p, mode="full", warnings=(), columns=None):
    """The launch banner: tier -> deployment map, judge chain, budgets. Returns a list of lines."""
    import common  # noqa: WPS433  (same folder; imported late so ui.py also works on its own)
    g = p.g
    columns = columns or shutil.get_terminal_size((100, 24)).columns
    tiers = []
    for t in common.TIERS:
        spec = cfg["tiers"][t]
        eff = common.effective_tier(t, cfg)
        dep = cfg["tiers"][eff]["deployment"]
        tiers.append((t, dep if eff == t else "%s (no deployment: uses %s)" % (dep, eff.upper())))
    warn_lines = [p.paint("%s %s" % (g["warn"], w), "warn") for w in warnings]
    if mode == "off":
        return warn_lines
    if mode == "compact" or columns < 72:
        parts = ["%s %s" % (p.tier(t), clip(dep, 24)) for t, dep in tiers]
        line = "%s %s %s %s  judge %s" % (p.paint(g["dot"], "bold", "accent"), p.paint("model-router", "bold"),
                                           (" %s " % g["sep"]).join(parts), g["sep"], judge_chain_text(cfg, p))
        return [line] + warn_lines
    budget = cfg["guard"]["main_edit_budget"]
    dep_w = max(len(clip(d, 44)) for _, d in tiers)
    rows = []
    for t, dep in tiers:
        rows.append("%s  %s %s  %s" % (p.tier(t, t.upper().ljust(8)), p.paint(g["arrow"], "dim"),
                                       pad(clip(dep, 44), dep_w), p.paint(TIER_WORK[t], "dim")))
    title = "%s %s %s" % (p.paint(g["dot"], "bold", "accent"), p.paint("model-router", "bold"),
                          p.paint(version, "dim"))
    title = pad(title, 30) + p.paint("difficulty-based model routing", "dim")
    lines = rows + [None,
                    "%s  %s" % (p.paint("judge  ", "dim"), clip(judge_chain_text(cfg, p), 60)),
                    "%s  Haiku edits %s %d / %d / %d lines %s retries %d" % (
                        p.paint("guard  ", "dim"), g["le"], budget["fast"], budget["standard"], budget["deep"],
                        g["sep"], cfg["launcher"]["max_retries"]),
                    "%s  %s" % (p.paint("foundry", "dim"), clip("%s %s %s" % (endpoint, g["sep"], cfg["foundry"]["auth"]),
                                                                60))]
    footer = p.paint("::fast  ::standard  ::deep  choose a tier yourself %s claude-router --router-dry-run"
                     % g["sep"], "dim")
    return box(lines, p, title=title, footer=footer, max_width=columns) + warn_lines


def verdict_line(tier, decision, cfg, ms=None, g=None):
    """One plain line shown to the user for each routed prompt (Claude Code's systemMessage: no colour codes)."""
    g = g or BOX_UNICODE
    budget = cfg["guard"]["main_edit_budget"][tier]
    work = TIER_WORK[tier] + (" (edits %s %d lines)" % (g["le"], budget) if tier == "fast" else "")
    parts = ["%s router: %s %s %s" % (g["dot"], tier.upper(), g["arrow"], work)]
    source = decision.get("source") or "?"
    if source == "override":
        parts.append("your %s override" % decision.get("reason", "").replace("user override ", ""))
    elif source in ("sticky", "escalation", "slash-command"):
        parts.append({"sticky": "follow-up keeps the verdict", "escalation": "moved up one tier after a failure",
                      "slash-command": "slash command keeps the verdict"}[source])
    else:
        conf = decision.get("confidence")
        parts.append("judge %s%s" % (source, " %.2f" % conf if conf is not None else ""))
        failed = [a for a in decision.get("attempts", []) if isinstance(a, dict) and not a.get("ok")]
        if failed:
            parts.append("%s failed" % ", ".join(str(a.get("step")) for a in failed))
    if decision.get("tier") in TIER_WORK and decision.get("tier") != tier:
        parts.append("no %s deployment" % decision["tier"].upper())
    if ms is not None and source not in ("override", "sticky", "escalation", "slash-command"):
        parts.append("%.1fs" % (ms / 1000.0))
    return (" %s " % g["sep"]).join(parts)
