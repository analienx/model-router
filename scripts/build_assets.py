#!/usr/bin/env python3
"""Generate the README images in docs/assets/, each in a light and a dark variant.

    python scripts/build_assets.py           # writes docs/assets/*.svg
    python scripts/build_assets.py --png     # also renders social-preview.png (needs Playwright + Chromium)

Edit the palette, layout or text here, never the generated SVGs. Standard library only (except --png).
The text in session.svg quotes the hooks' own messages (router/judge_hook.py, router/guard_hook.py).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "assets"

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

PALETTES = {
    "light": {"bg": "#ffffff", "panel": "#f6f8fa", "border": "#d0d7de", "text": "#1f2328", "muted": "#59636e",
              "grid": "#d8dee4", "fast": "#0d9488", "standard": "#4f46e5", "deep": "#c2410c", "guard": "#cf222e",
              "ok": "#1a7f37", "glow": "0.10"},
    "dark": {"bg": "#0d1117", "panel": "#161b22", "border": "#30363d", "text": "#e6edf3", "muted": "#9198a1",
             "grid": "#262c36", "fast": "#2dd4bf", "standard": "#818cf8", "deep": "#fb923c", "guard": "#ff7b72",
             "ok": "#3fb950", "glow": "0.16"},
}
# The terminal illustration is dark in both themes, like most terminals.
TERM = {"bg": "#0b0f14", "bar": "#161b22", "border": "#30363d", "text": "#e6edf3", "muted": "#8b949e",
        "hook": "#a5b4fc", "prompt": "#2dd4bf", "deny": "#ff7b72", "ok": "#3fb950", "deep": "#fb923c"}

MOTION = """
.flow{stroke-dasharray:2 12;stroke-linecap:round;animation:flow 1.4s linear infinite}
.d1{animation-delay:-.47s}.d2{animation-delay:-.93s}
@keyframes flow{to{stroke-dashoffset:-28}}
@media (prefers-reduced-motion:reduce){.flow{animation:none}}
"""


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(w, h, title, desc, body, css=""):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
            'aria-labelledby="title desc">\n<title id="title">%s</title>\n<desc id="desc">%s</desc>\n'
            '<style>\ntext{font-family:%s}\n.mono{font-family:%s}\n%s</style>\n%s\n</svg>\n'
            % (w, h, w, h, esc(title), esc(desc), SANS, MONO, css.strip() + "\n", "\n".join(body)))


def defs(p, extra=""):
    return """<defs>
<linearGradient id="mark" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#14b8a6"/><stop offset=".55" stop-color="#6366f1"/><stop offset="1" stop-color="#f97316"/>
</linearGradient>
<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1.1" fill="%(grid)s"/></pattern>
<radialGradient id="glow-a" cx=".78" cy=".15" r=".6"><stop offset="0" stop-color="%(standard)s" stop-opacity="%(glow)s"/><stop offset="1" stop-color="%(standard)s" stop-opacity="0"/></radialGradient>
<radialGradient id="glow-b" cx=".98" cy="1" r=".5"><stop offset="0" stop-color="%(deep)s" stop-opacity="%(glow)s"/><stop offset="1" stop-color="%(deep)s" stop-opacity="0"/></radialGradient>
<radialGradient id="glow-c" cx=".05" cy="1" r=".45"><stop offset="0" stop-color="%(fast)s" stop-opacity="%(glow)s"/><stop offset="1" stop-color="%(fast)s" stop-opacity="0"/></radialGradient>
<linearGradient id="fade" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity=".15"/><stop offset=".5" stop-color="#fff" stop-opacity=".45"/><stop offset="1" stop-color="#fff" stop-opacity="1"/></linearGradient>
<mask id="fade-mask"><rect width="100%%" height="100%%" fill="url(#fade)"/></mask>
<marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="%(muted)s"/></marker>
<marker id="arrow-deep" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="%(deep)s"/></marker>
<marker id="arrow-guard" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="%(guard)s"/></marker>
%(extra)s</defs>""" % dict(p, extra=extra)


def backdrop(p, w, h, rx=24):
    return ['<rect width="%d" height="%d" rx="%d" fill="%s"/>' % (w, h, rx, p["bg"]),
            '<rect width="%d" height="%d" rx="%d" fill="url(#dots)" mask="url(#fade-mask)"/>' % (w, h, rx),
            '<rect width="%d" height="%d" rx="%d" fill="url(#glow-a)"/>' % (w, h, rx),
            '<rect width="%d" height="%d" rx="%d" fill="url(#glow-b)"/>' % (w, h, rx),
            '<rect width="%d" height="%d" rx="%d" fill="url(#glow-c)"/>' % (w, h, rx),
            '<rect x=".5" y=".5" width="%d" height="%d" rx="%.1f" fill="none" stroke="%s"/>'
            % (w - 1, h - 1, rx - .5, p["border"])]


def mark(x, y, size=64):
    """The logo: one input fanning out to three routes, on the tier gradient."""
    return ('<g transform="translate(%s %s) scale(%.4f)">'
            '<rect width="64" height="64" rx="17" fill="url(#mark)"/>'
            '<g fill="none" stroke="#fff" stroke-width="3.6" stroke-linecap="round">'
            '<path d="M22 32 C33 32 33 18 44 18"/><path d="M22 32 H44"/><path d="M22 32 C33 32 33 46 44 46"/></g>'
            '<g fill="#fff"><circle cx="17" cy="32" r="5.5"/><circle cx="47.5" cy="18" r="4.2"/>'
            '<circle cx="47.5" cy="32" r="4.2"/><circle cx="47.5" cy="46" r="4.2"/></g></g>' % (x, y, size / 64.0))


def shield(cx, cy, color, k=1.0):
    return ('<path transform="translate(%s %s) scale(%s)" d="M0 -10 L8.5 -6.8 V0 C8.5 6 4.6 9.4 0 11 '
            'C-4.6 9.4 -8.5 6 -8.5 0 V-6.8 Z" fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round"/>'
            '<path transform="translate(%s %s) scale(%s)" d="M-3.6 0.4 L-0.8 3.2 L4 -2.4" fill="none" stroke="%s" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' % (cx, cy, k, color, cx, cy, k, color))


def chip(x, y, label, p, fs=15):
    w = int(len(label) * fs * 0.58 + 32)
    return w, ('<rect x="%d" y="%d" width="%d" height="34" rx="17" fill="%s" stroke="%s"/>'
               '<text x="%.1f" y="%d" font-size="%d" fill="%s" text-anchor="middle">%s</text>'
               % (x, y, w, p["panel"], p["border"], x + w / 2.0, y + 22, fs, p["text"], esc(label)))


def lane_pill(x, cy, w, tier, model, color, p):
    h = 52
    return ['<rect x="%d" y="%d" width="%d" height="%d" rx="26" fill="%s" stroke="%s"/>'
            % (x, cy - h // 2, w, h, p["panel"], p["border"]),
            '<circle cx="%d" cy="%d" r="11" fill="%s" fill-opacity=".18"/>' % (x + 27, cy, color),
            '<circle cx="%d" cy="%d" r="5.5" fill="%s"/>' % (x + 27, cy, color),
            '<text x="%d" y="%.1f" font-size="15" font-weight="700" letter-spacing="1" fill="%s">%s</text>'
            % (x + 48, cy + 5.5, p["text"], tier),
            '<text x="%d" y="%.1f" font-size="15" fill="%s" text-anchor="end" class="mono">%s</text>'
            % (x + w - 24, cy + 5.5, p["muted"], model)]


def routing_graphic(p, x0=660, cy=200, lanes=(110, 200, 290)):
    """prompt -> judge -> guard gate -> three tiers, with flowing dashes."""
    b = []
    jx = x0 + 190                     # judge diamond centre
    gate = jx + 82                    # guard gate
    lx = jx + 130                     # lanes start
    b.append('<rect x="%d" y="%d" width="104" height="40" rx="20" fill="%s" stroke="%s"/>'
             % (x0, cy - 20, p["panel"], p["border"]))
    b.append('<text x="%d" y="%d" font-size="15" fill="%s" text-anchor="middle">prompt</text>' % (x0 + 52, cy + 5, p["text"]))
    b.append('<path d="M%d %d H%d" stroke="%s" stroke-width="2"/>' % (x0 + 104, cy, jx - 36, p["border"]))
    b.append('<path class="flow" d="M%d %d H%d" stroke="%s" stroke-width="2.6"/>' % (x0 + 104, cy, jx - 36, p["muted"]))
    for i, (ly, tier) in enumerate(zip(lanes, ("fast", "standard", "deep"))):
        d = ("M%d %d H%d" % (jx + 36, cy, lx) if ly == cy else
             "M%d %d C%d %d %d %d %d %d" % (jx + 36, cy, jx + 80, cy, jx + 80, ly, lx, ly))
        b.append('<path d="%s" fill="none" stroke="%s" stroke-width="2"/>' % (d, p["border"]))
        b.append('<path class="flow d%d" d="%s" fill="none" stroke="%s" stroke-width="2.6"/>' % (i, d, p[tier]))
    b.append('<polygon points="%d,%d %d,%d %d,%d %d,%d" fill="%s" stroke="url(#mark)" stroke-width="2.5" '
             'stroke-linejoin="round"/>' % (jx, cy - 36, jx + 36, cy, jx, cy + 36, jx - 36, cy, p["panel"]))
    b.append('<text x="%d" y="%d" font-size="13" font-weight="700" fill="%s" text-anchor="middle">judge</text>'
             % (jx, cy + 4.5, p["text"]))
    b.append('<path d="M%d %d V%d" stroke="%s" stroke-width="2" stroke-dasharray="3 6" stroke-linecap="round" '
             'opacity=".8"/>' % (gate, lanes[0] - 42, lanes[-1] + 42, p["guard"]))
    b.append(shield(gate, lanes[0] - 60, p["guard"]))
    b.append('<text x="%d" y="%d" font-size="13" font-weight="600" fill="%s" text-anchor="middle">guard</text>'
             % (gate, lanes[-1] + 64, p["guard"]))
    for ly, tier, model in zip(lanes, ("FAST", "STANDARD", "DEEP"), ("haiku", "sonnet", "opus")):
        b += lane_pill(lx, ly, 240, tier, model, p[tier.lower()], p)
    return b


def banner(p):
    w, h = 1280, 400
    b = [defs(p)] + backdrop(p, w, h)
    b.append(mark(80, 108, 72))
    b.append('<text x="172" y="168" font-size="64" font-weight="750" letter-spacing="-1.5" fill="%s">model-router</text>'
             % p["text"])
    b.append('<text x="80" y="232" font-size="25" fill="%s">Difficulty-based model routing for Claude Code</text>'
             % p["muted"])
    x = 80
    for label in ("judge before work", "hook-enforced", "429-aware fallback"):
        cw, c = chip(x, 262, label, p)
        b.append(c)
        x += cw + 10
    b += routing_graphic(p)
    return svg(w, h, "model-router",
               "Difficulty-based model routing for Claude Code: a judge rates each prompt and routes it to FAST "
               "(Haiku), STANDARD (Sonnet) or DEEP (Opus); a guard hook enforces the verdict.", b, MOTION)


def social(p):
    w, h = 1280, 640
    b = [defs(p)] + backdrop(p, w, h, rx=0)
    b.append(mark(96, 150, 96))
    b.append('<text x="216" y="228" font-size="84" font-weight="750" letter-spacing="-2" fill="%s">model-router</text>'
             % p["text"])
    b.append('<text x="96" y="318" font-size="34" fill="%s">Difficulty-based model routing for Claude Code</text>'
             % p["muted"])
    b.append('<text x="96" y="372" font-size="25" fill="%s">A judge rates every prompt. Haiku does the easy work.</text>'
             % p["text"])
    b.append('<text x="96" y="408" font-size="25" fill="%s">Sonnet and Opus get the rest, and hooks enforce it.</text>'
             % p["text"])
    x = 96
    for tier, model in (("FAST", "haiku"), ("STANDARD", "sonnet"), ("DEEP", "opus")):
        b += lane_pill(x, 480, 250, tier, model, p[tier.lower()], p)
        x += 266
    b.append('<text x="96" y="572" font-size="20" fill="%s" class="mono">github.com/analienx/model-router</text>'
             % p["muted"])
    b.append('<text x="1184" y="572" font-size="20" fill="%s" text-anchor="end">MIT · Python 3.8+ · no dependencies</text>'
             % p["muted"])
    return svg(w, h, "model-router", "Social preview card for the model-router repository.", b)


def card(x, y, w, h, p, stroke=None, dashed=False, fill=None):
    return ('<rect x="%d" y="%d" width="%d" height="%d" rx="16" fill="%s" stroke="%s" stroke-width="%s"%s/>'
            % (x, y, w, h, fill or p["panel"], stroke or p["border"], "1.5" if stroke else "1",
               ' stroke-dasharray="6 5"' if dashed else ""))


def step(cx, cy, n):
    return ('<circle cx="%d" cy="%d" r="13" fill="url(#mark)"/>'
            '<text x="%d" y="%d" font-size="14" font-weight="700" fill="#fff" text-anchor="middle">%s</text>'
            % (cx, cy, cx, cy + 5, n))


def t(x, y, s, p, size=15, color="text", weight=None, anchor=None, mono=False, ls=None):
    attrs = ['x="%s"' % x, 'y="%s"' % y, 'font-size="%s"' % size, 'fill="%s"' % p.get(color, color)]
    if weight:
        attrs.append('font-weight="%s"' % weight)
    if anchor:
        attrs.append('text-anchor="%s"' % anchor)
    if ls:
        attrs.append('letter-spacing="%s"' % ls)
    if mono:
        attrs.append('class="mono"')
    return "<text %s>%s</text>" % (" ".join(attrs), esc(s))


def flow_diagram(p):
    w, h = 1280, 640
    b = [defs(p)] + backdrop(p, w, h)
    b.append(t(32, 62, "How a prompt is routed", p, 24, weight=700))
    b.append(t(32, 90, "One judge call per new prompt. Follow-ups such as “yes” or “continue” skip it.",
               p, 15, "muted"))
    # 0 - the user
    b.append(card(32, 236, 172, 104, p))
    b.append(t(52, 266, "YOU", p, 12, "muted", 700, ls=1.5))
    b.append(t(52, 294, "type a prompt", p, 18))
    b.append(t(52, 320, "claude-router", p, 13, "muted", mono=True))
    b.append('<path d="M204 288 H244" stroke="%s" stroke-width="2" marker-end="url(#arrow)"/>' % p["muted"])
    # 1 - judge
    b.append(card(248, 150, 300, 276, p))
    b.append(step(276, 182, 1))
    b.append(t(298, 189, "Judge hook", p, 20, weight=700))
    b.append(t(276, 216, "rates every new prompt", p, 14, "muted"))
    for i, (main, sub, tag) in enumerate((("Sonnet", " · subscription", "first"),
                                          ("Sonnet", " · Azure Foundry", "then"),
                                          ("keyword rules", "", "last"))):
        y = 234 + i * 54
        b.append('<rect x="268" y="%d" width="260" height="42" rx="10" fill="%s" stroke="%s"/>' % (y, p["bg"], p["border"]))
        b.append('<text x="284" y="%d" font-size="15" fill="%s">%s<tspan fill="%s">%s</tspan></text>'
                 % (y + 26, p["text"], esc(main), p["muted"], esc(sub)))
        b.append(t(512, y + 26, tag, p, 12, "muted", anchor="end", mono=True))
    b.append(t(276, 410, "unsure? → the stronger tier", p, 13, "muted"))
    # 2 - Haiku
    b.append('<path d="M548 288 H596" stroke="%s" stroke-width="2" marker-end="url(#arrow)"/>' % p["muted"])
    b.append(card(600, 226, 210, 124, p))
    b.append(step(628, 258, 2))
    b.append(t(650, 265, "Haiku session", p, 19, weight=700))
    b.append(t(620, 294, "gets the verdict and", p, 14, "muted"))
    b.append(t(620, 314, "the rules as context", p, 14, "muted"))
    b.append(t(620, 336, "Azure Foundry", p, 12, "muted", mono=True))
    # lanes
    lanes = (("fast", "FAST", "Haiku does it itself", "Haiku edits ≤ 40 lines"),
             ("standard", "STANDARD", "implementer · Sonnet", "Haiku edits ≤ 10 lines"),
             ("deep", "DEEP", "architect · Opus", "Haiku edits: none"))
    for i, (key, label, who, budget) in enumerate(lanes):
        y = 118 + i * 108
        cy = y + 44
        d = "M810 288 C842 288 838 %d 866 %d" % (cy, cy) if cy != 288 else "M810 288 H866"
        b.append('<path d="%s" fill="none" stroke="%s" stroke-width="2"/>' % (d, p["border"]))
        b.append('<path class="flow d%d" d="%s" fill="none" stroke="%s" stroke-width="2.6"/>' % (i, d, p[key]))
        b.append(card(870, y, 378, 88, p))
        b.append('<rect x="870" y="%d" width="5" height="88" rx="2.5" fill="%s"/>' % (y, p[key]))
        b.append('<circle cx="896" cy="%d" r="5.5" fill="%s"/>' % (y + 30, p[key]))
        b.append(t(910, y + 35, label, p, 13, key, 700, ls=1.4))
        b.append(t(1228, y + 35, budget, p, 12, "muted", anchor="end", mono=True))
        b.append(t(890, y + 66, who, p, 17))
    # fallback
    b.append('<path d="M1059 422 V442" stroke="%s" stroke-width="2" stroke-dasharray="4 4" marker-end="url(#arrow-deep)"/>'
             % p["deep"])
    b.append(t(1072, 437, "429 / 529", p, 12, "deep", mono=True))
    b.append(card(870, 446, 378, 72, p, stroke=p["deep"], dashed=True, fill=p["bg"]))
    b.append('<text x="890" y="476" font-size="16" fill="%s">deep-haiku<tspan fill="%s"> · Haiku, max effort</tspan></text>'
             % (p["text"], p["muted"]))
    b.append(t(890, 501, "only after a real rate-limit", p, 12, "muted", mono=True))
    # 3 - guard
    b.append('<path d="M705 546 V354" stroke="%s" stroke-width="2" stroke-dasharray="2 6" stroke-linecap="round" '
             'marker-end="url(#arrow-guard)"/>' % p["guard"])
    b.append(t(716, 446, "checks every edit", p, 12, "muted", mono=True))
    b.append(t(716, 464, "and Agent call", p, 12, "muted", mono=True))
    b.append(card(248, 548, 1000, 64, p, stroke=p["guard"]))
    b.append(step(276, 580, 3))
    b.append(shield(306, 580, p["guard"], 0.95))
    b.append(t(324, 586, "Guard hook", p, 18, weight=700))
    b.append(t(442, 586, "denies over-budget edits · the wrong delegate · model overrides · "
                         "fallback without a 429", p, 15, "muted"))
    return svg(w, h, "How model-router routes a prompt",
               "1. The judge hook rates each new prompt with Sonnet on the subscription, then Sonnet on Azure Foundry, "
               "then keyword rules. 2. The Haiku session receives the verdict: FAST it does itself, STANDARD goes to "
               "implementer on Sonnet, DEEP to architect on Opus; deep-haiku is the fallback after a 429 or 529. "
               "3. The guard hook checks every edit and Agent call.", b, MOTION)


def session():
    """Dark terminal card with the hooks' real messages (abridged) for an example DEEP prompt."""
    c = TERM
    lh, x0, y0 = 27, 44, 90
    lines = [
        [("$ ", "prompt"), ("claude-router", "text")],
        [("> ", "prompt"), ("Change the storage schema and migrate the old data", "text")],
        [],
        [("UserPromptSubmit", "hook"), ("  added to Haiku's context", "muted")],
        [("  [router] Verdict for this prompt: ", "text"), ("DEEP", "deep"),
         (" (source: subscription:sonnet, confidence 0.90).", "text")],
        [("  Before answering or editing, delegate to the `architect` subagent with a self-contained", "text")],
        [("  brief … `implementer` is not accepted for this verdict. Main-thread file edits are blocked this turn.", "text")],
        [],
        [("PreToolUse", "hook"), ("  Write(migrations/0042_storage.py)", "text")],
        [("  [router] Write of 50 lines would bring main-thread edits to more than the DEEP budget", "muted")],
        [("  of 0 lines for this prompt (already used: 0). Delegate with the Agent tool to `architect` …", "muted")],
        [],
        [("PreToolUse", "hook"), ("  Agent(implementer)", "text")],
        [("  [router] The router verdict for this prompt is DEEP: delegate to `architect`, not `implementer` \u2026", "muted")],
        [],
        [("PreToolUse", "hook"), ("  Agent(architect)", "text")],
        [("  runs on the Opus deployment; its own edits are not limited", "muted")],
    ]
    status = {8: ("✕ denied", "deny"), 12: ("✕ denied", "deny"), 15: ("✓ allowed", "ok")}
    w = 1280
    h = y0 + lh * (len(lines) - 1) + 36
    b = ['<rect x=".5" y=".5" width="%d" height="%d" rx="14" fill="%s" stroke="%s"/>' % (w - 1, h - 1, c["bg"], c["border"]),
         '<path d="M14.5 .5 H%d a14 14 0 0 1 14 14 V46 H.5 V14.5 a14 14 0 0 1 14 -14 Z" fill="%s"/>' % (w - 15, c["bar"]),
         '<path d="M.5 46 H%d" stroke="%s"/>' % (w - 1, c["border"])]
    for i, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        b.append('<circle cx="%d" cy="23" r="6.5" fill="%s"/>' % (28 + i * 22, col))
    b.append('<text x="%d" y="28" font-size="14" fill="%s" text-anchor="middle">claude-router · what the hooks '
             'say (example prompt)</text>' % (w // 2, c["muted"]))
    for i, segs in enumerate(lines):
        y = y0 + i * lh
        if segs:
            spans = "".join('<tspan fill="%s">%s</tspan>' % (c[color], esc(s).replace("  ", "  "))
                            for s, color in segs)
            b.append('<text x="%d" y="%d" font-size="16" class="mono" xml:space="preserve">%s</text>' % (x0, y, spans))
        if i in status:
            label, color = status[i]
            b.append('<text x="%d" y="%d" font-size="15" font-weight="700" class="mono" fill="%s" text-anchor="end">%s</text>'
                     % (w - 44, y, c[color], label))
    return svg(w, h, "What the router's hooks say",
               "Terminal illustration: for an example DEEP prompt the judge hook adds the verdict to Haiku's context, "
               "the guard denies Haiku's own Write and a hand-off to implementer, and allows architect.", b)


def build():
    files = {"session.svg": session()}
    for theme, p in PALETTES.items():
        files["banner-%s.svg" % theme] = banner(p)
        files["how-it-works-%s.svg" % theme] = flow_diagram(p)
    files["social-preview.svg"] = social(PALETTES["dark"])
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_bytes(text.encode("utf-8"))
    return sorted(files)


def render_png():
    try:
        from playwright.sync_api import sync_playwright  # optional, only for --png
    except ImportError:
        sys.exit("--png needs the Playwright Python package: pip install playwright && playwright install chromium")
    src = (OUT / "social-preview.svg").read_bytes().decode("utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
        page.set_content('<html><body style="margin:0">%s</body></html>' % src)
        page.screenshot(path=str(OUT / "social-preview.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 640})
        browser.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--png", action="store_true", help="also render docs/assets/social-preview.png")
    args = ap.parse_args(argv)
    names = build()
    if args.png:
        render_png()
        names.append("social-preview.png")
    sys.stdout.write("wrote %s\n" % ", ".join("docs/assets/" + n for n in names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
