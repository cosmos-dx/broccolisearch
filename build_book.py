#!/usr/bin/env python3
"""Render BOOK.md to a printable PDF, diagrams and formulas included.

    python3 build_book.py             # -> BOOK.pdf
    python3 build_book.py --html      # also keep the intermediate BOOK.html

How it works: the Markdown is embedded verbatim into a single HTML file that
converts it in the browser with marked.js, draws the diagrams with mermaid.js and
the formulas with MathJax, all loaded from a CDN. Headless Chrome then prints the
page.

Doing the conversion browser-side rather than in Python is what keeps this
dependency-free: no pandoc, no LaTeX distribution, no npm install, and nothing
added to the library's own requirements. Chrome is already fetching mermaid and
MathJax, so fetching one more script costs nothing.

ponytail: needs Chrome (or any Chromium/Edge build) and a network connection for
the three CDN scripts. Ceiling: no offline build. Upgrade path: vendor the three
scripts next to the HTML and point the tags at local files.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BroccoliSearch</title>
<script>
  window.MathJax = {
    tex: { displayMath: [['$$', '$$']], inlineMath: [] },
    options: { skipHtmlTags: ['script','noscript','style','textarea','pre','code'] },
    startup: { typeset: false }
  };
</script>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" id="MathJax-script"></script>
<style>
  :root { --ink:#1a1a1a; --muted:#5b6470; --rule:#d7dce2; --accent:#2f6f4f; }
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body {
    font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 10.5pt; line-height: 1.62; color: var(--ink);
    max-width: 870px; margin: 0 auto; padding: 0 26px;
  }
  h1, h2, h3, h4 { line-height: 1.25; font-weight: 650; }
  h1 { font-size: 23pt; margin: 1.1em 0 .5em; color: var(--accent);
       border-bottom: 2.5px solid var(--accent); padding-bottom: .22em;
       page-break-before: always; }
  h1:first-of-type { page-break-before: avoid; }
  h2 { font-size: 15.5pt; margin: 1.5em 0 .45em; border-bottom: 1px solid var(--rule);
       padding-bottom: .16em; page-break-after: avoid; }
  h3 { font-size: 12.5pt; margin: 1.25em 0 .35em; color: #2c3742; page-break-after: avoid; }
  h4 { font-size: 11pt; margin: 1em 0 .3em; color: var(--muted); page-break-after: avoid; }
  p, li { orphans: 3; widows: 3; }
  a { color: #1f5fa8; text-decoration: none; }
  code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: .85em;
         background: #f2f4f6; padding: .1em .34em; border-radius: 3px; }
  pre { background: #f7f9fa; border: 1px solid var(--rule);
        border-left: 3px solid var(--accent); border-radius: 5px;
        padding: 10px 13px; overflow-x: auto; page-break-inside: avoid;
        font-size: 8.5pt; line-height: 1.45; white-space: pre-wrap;
        word-wrap: break-word; }
  pre code { background: none; padding: 0; font-size: inherit; }
  blockquote { margin: 1em 0; padding: .55em 1.05em;
               border-left: 3.5px solid var(--accent); background: #f2f7f4;
               color: #24303a; page-break-inside: avoid; }
  blockquote p { margin: .35em 0; }
  table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 8.8pt;
          page-break-inside: avoid; }
  th, td { border: 1px solid var(--rule); padding: 5px 8px; text-align: left;
           vertical-align: top; }
  th { background: #eef2f0; font-weight: 640; }
  tr:nth-child(even) td { background: #fafbfc; }
  hr { border: 0; border-top: 1px solid var(--rule); margin: 1.8em 0; }
  /* Diagrams get the full printable width rather than the text measure: a wide
     figure scaled into a narrow column is what makes Mermaid output unreadable. */
  .mermaid { text-align: center; margin: 1.4em 0; page-break-inside: avoid;
             background: #fff; width: 112%; margin-left: -6%; }
  .caption { text-align: center; font-size: 8.8pt; color: var(--muted);
             margin: -.5em 0 1.6em; line-height: 1.45; page-break-before: avoid; }

  /* ------------------------------ front matter ------------------------- */
  .pagebreak { page-break-after: always; }
  .cover { page-break-after: always; margin: 0; text-align: center; }
  /* Sized by height so the cover fills the sheet; max-width keeps it inside the
     page box when the paper is narrower than the artwork's 3:4 ratio. */
  .cover img { display: block; margin: 0 auto; height: 95vh; width: auto;
               max-width: 100%; }
  .titlepage { page-break-after: always; text-align: center; padding-top: 26vh; }
  .titlepage h1 { font-size: 40pt; border: 0; color: var(--accent);
                  page-break-before: avoid; margin-bottom: .1em; }
  .titlepage h2 { font-size: 15pt; border: 0; font-weight: 400; color: #2c3742;
                  margin-top: 0; }
  .titlepage em { display: block; margin: 2.4em 0; color: var(--muted); }
  .titlepage strong { display: block; margin-top: 3.4em; font-size: 15pt; }
  .titlepage code { display: block; margin-top: .5em; background: none;
                    color: var(--muted); }
  .titlepage p:last-child { margin-top: 2.6em; color: var(--muted);
                            letter-spacing: 2px; font-size: 9pt; }
  .copyright { page-break-after: always; font-size: 8.8pt; color: #40484f;
               padding-top: 8vh; }
  .dedication { page-break-after: always; text-align: center; padding-top: 32vh;
                font-style: italic; color: var(--muted); line-height: 2; }
  .endmark { text-align: center; color: var(--muted); font-size: 9pt;
             letter-spacing: 2.5px; margin-top: 3em; }
  mjx-container[display="true"] { margin: .85em 0 !important;
                                  page-break-inside: avoid; }
  @page { size: A4; margin: 15mm 13mm 16mm; }
</style>
</head>
<body>
<script id="source" type="text/plain">__MARKDOWN__</script>
<div id="content"></div>
<script>
(async () => {
  let md = document.getElementById('source').textContent;

  // Pull $$...$$ out before Markdown runs. Otherwise the underscores and
  // asterisks inside a formula get read as emphasis and the TeX is mangled
  // before MathJax ever sees it.
  const formulas = [];
  md = md.replace(/\\$\\$[\\s\\S]*?\\$\\$/g, (hit) => {
    formulas.push(hit);
    return `@@FORMULA${formulas.length - 1}@@`;
  });

  marked.setOptions({ gfm: true, breaks: false });
  let rendered = marked.parse(md);
  rendered = rendered.replace(/@@FORMULA(\\d+)@@/g, (_, i) => formulas[i]);
  document.getElementById('content').innerHTML = rendered;

  // marked emits ```mermaid as <pre><code class="language-mermaid">; mermaid
  // wants the raw source in a plain container.
  document.querySelectorAll('pre code.language-mermaid').forEach((block) => {
    const holder = document.createElement('div');
    holder.className = 'mermaid';
    holder.textContent = block.textContent;
    block.parentElement.replaceWith(holder);
  });

  // A wide diagram scaled to the page width becomes an unreadable strip, so
  // the font is set large and the tall layouts are preferred in the source.
  mermaid.initialize({ startOnLoad: false, theme: 'neutral', fontSize: 15,
                       flowchart: { htmlLabels: true, useMaxWidth: true,
                                    nodeSpacing: 45, rankSpacing: 45 },
                       sequence: { useMaxWidth: true } });
  try { await mermaid.run(); } catch (e) { console.error('mermaid', e); }
  try { await MathJax.typesetPromise(); } catch (e) { console.error('mathjax', e); }

  document.body.setAttribute('data-ready', 'true');
})();
</script>
</body>
</html>
"""


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    sys.exit("No Chrome/Chromium found. Install Google Chrome, or run with\n"
             "--html and print BOOK.html from any browser (File > Print > PDF).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=os.path.join(HERE, "BOOK.md"))
    ap.add_argument("--out", default=os.path.join(HERE, "BOOK.pdf"))
    ap.add_argument("--html", action="store_true",
                    help="keep the intermediate HTML next to the PDF")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        sys.exit(f"no such file: {args.source}")

    with open(args.source, encoding="utf-8") as fh:
        markdown = fh.read()
    # The only sequence that can terminate a <script> block early.
    markdown = markdown.replace("</script", "<\\/script")

    page = TEMPLATE.replace("__MARKDOWN__", markdown)
    html_path = os.path.splitext(args.out)[0] + ".html"
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"{os.path.basename(args.source)}: {len(markdown) // 1024} KB, "
          f"{markdown.count('```mermaid')} diagrams, "
          f"{markdown.count('$$') // 2} formulas")

    chrome = find_chrome()
    print(f"printing with {os.path.basename(chrome)} ...")
    # --virtual-time-budget gives the CDN scripts and the layout passes time to
    # finish; without it the PDF contains raw diagram source and unrendered TeX.
    done = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
         "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
         "--virtual-time-budget=90000",
         f"--print-to-pdf={args.out}", f"file://{html_path}"],
        capture_output=True, text=True, timeout=600, check=False)

    if not os.path.exists(args.out):
        sys.exit(f"Chrome produced no PDF:\n{done.stderr.strip()[:900]}")
    if not args.html:
        os.remove(html_path)

    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1024:.0f} KB)")
    if args.html:
        print(f"kept  {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
