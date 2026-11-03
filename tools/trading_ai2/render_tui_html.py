"""Renders a captured TUI stream (with ANSI colour codes) as a terminal page.

Usage: python tools/trading_ai2/render_tui_html.py <in.log> <out.html> [title]
"""
from __future__ import annotations

import html
import re
import sys

SGR = {
    "0": "", "1": "b", "2": "dim", "4": "u",
    "30": "k", "31": "r", "32": "g", "33": "y", "34": "b", "35": "m",
    "36": "c", "37": "w", "90": "gk", "91": "gr", "92": "gg", "93": "gy",
    "94": "gb", "95": "gm", "96": "gc", "97": "gw",
}

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #07090d; display: flex; justify-content: center; }
.term { width: min(1720px, 96vw); margin: 24px 0 40px; padding: 26px 34px 34px;
  background: #0b0f16; border: 1px solid #1d2635; border-radius: 14px;
  box-shadow: 0 30px 80px rgba(0,0,0,.7); }
.bar { display: flex; align-items: center; gap: 10px; margin-bottom: 18px;
  padding-bottom: 12px; border-bottom: 1px solid #1d2635;
  font: 500 15px/1.2 "Segoe UI", system-ui, sans-serif; color: #8b93a3; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
pre { margin: 0; font: 400 15px/1.45 "Cascadia Mono", Consolas, monospace;
  color: #d7dce6; white-space: pre-wrap; word-break: break-word; }
.k{color:#6b7484}.r{color:#f2607a}.g{color:#5fd08c}.y{color:#f0c35b}
.b{color:#7aa2f7}.m{color:#c17ce8}.c{color:#54d2e0}.w{color:#eef2f8}
.gk{color:#98a2b3}.gr{color:#ff8095}.gg{color:#82e6a6}.gy{color:#ffd873}
.gb{color:#9db8ff}.gm{color:#e0a0ff}.gc{color:#8ce8f4}.gw{color:#ffffff}
.b{font-weight:700}.u{text-decoration:underline}
b,strong{font-weight:700}
"""


def render(lines: list[str]) -> str:
    out = []
    for raw in lines:
        line = raw.rstrip("\n").replace("\t", "    ")
        parts, cls, i = [], "", 0
        for m in ANSI_RE.finditer(line):
            parts.append((cls, line[i:m.start()]))
            codes = (m.group(1) or "0").split(";")
            keep = [c for c in codes if c and c != "0"]
            if "0" in codes:
                cls = ""
            for c in keep:
                sgr = SGR.get(c)
                if sgr and sgr != cls:
                    cls = sgr
            i = m.end()
        parts.append((cls, line[i:]))
        buf = []
        for c, text in parts:
            esc = html.escape(text)
            buf.append(f'<span class="{c}">{esc}</span>' if c else esc)
        out.append("".join(buf) or "&nbsp;")
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else src
    body = render(open(src, encoding="utf-8", errors="replace").readlines())
    html_doc = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        '<body><div class="term"><div class="bar">'
        '<span class="dot" style="background:#f2607a"></span>'
        '<span class="dot" style="background:#f0c35b"></span>'
        '<span class="dot" style="background:#5fd08c"></span>'
        f"&nbsp;{html.escape(title)} — NEXUS trading AI-2 (TUI)</div>"
        f"<pre>{body}</pre></div></body></html>"
    )
    open(dst, "w", encoding="utf-8").write(html_doc)
    print(f"{dst} <- {src} ({len(body.splitlines())} строк)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
