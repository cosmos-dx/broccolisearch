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
  :root {
    --ink:#1a1a1a; --muted:#5b6470; --rule:#d7dce2; --accent:#2f6f4f;
    /* A serif measure for reading and a sans for structure: the contrast is
       what separates a typeset book from a printed web page. Both stacks are
       system faces, so the build stays offline-capable for fonts. */
    --serif: Charter, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia,
             "Times New Roman", serif;
    --sans: -apple-system, "Helvetica Neue", "Segoe UI", Arial, sans-serif;
    --mono: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body {
    font-family: var(--serif);
    font-size: 10.8pt; line-height: 1.58; color: var(--ink);
    max-width: 870px; margin: 0 auto; padding: 0 26px;
    text-rendering: optimizeLegibility;
    font-kerning: normal; font-variant-ligatures: common-ligatures;
  }
  h1, h2, h3, h4 { font-family: var(--sans); line-height: 1.22; }
  /* Every chapter opens a page, and never ends one: a heading is glued to the
     text beneath it. Chapters carry the top margin inside the page, so the rule
     sits well below the trim edge rather than against it. */
  h1 { font-size: 24pt; font-weight: 700; letter-spacing: -.6px;
       margin: 0 0 .85em; padding: 9mm 0 .3em; color: var(--accent);
       border-bottom: 2.5px solid var(--accent);
       page-break-before: always; page-break-after: avoid;
       break-before: page; break-after: avoid; }
  h2 { font-size: 13.5pt; font-weight: 650; letter-spacing: -.2px;
       margin: 1.7em 0 .45em; color: #24303a;
       page-break-after: avoid; break-after: avoid; }
  h3 { font-size: 11pt; font-weight: 650; margin: 1.35em 0 .35em; color: #2c3742;
       page-break-after: avoid; break-after: avoid; }
  h4 { font-size: 9pt; font-weight: 700; margin: 1.2em 0 .3em;
       color: var(--accent); text-transform: uppercase; letter-spacing: 1.6px;
       page-break-after: avoid; break-after: avoid; }
  /* Never strand a single line of a paragraph across a page turn. */
  p, li, blockquote, td { orphans: 3; widows: 3; }
  p { margin: .72em 0; }
  li { margin: .2em 0; page-break-inside: avoid; }
  /* A raised initial opens each Part. Not a floated drop cap: some parts open on
     a single-line paragraph, and a float three lines deep there overlaps the
     block beneath it. The .opener class is applied by script because what follows
     a Part heading varies. */
  p.opener::first-letter {
    font-family: var(--sans); font-size: 2.35em; font-weight: 700;
    color: var(--accent); line-height: 1; padding-right: 1px;
  }
  a { color: #1f5fa8; text-decoration: none; }
  code { font-family: var(--mono); font-size: .82em;
         background: #f2f4f6; padding: .1em .34em; border-radius: 3px; }
  pre { background: #f7f9fa; border: 1px solid var(--rule);
        border-left: 3px solid var(--accent); border-radius: 5px;
        padding: 10px 13px; overflow-x: auto; page-break-inside: avoid;
        font-size: 8.5pt; line-height: 1.45; white-space: pre-wrap;
        word-wrap: break-word; }
  pre code { background: none; padding: 0; font-size: inherit; }
  /* Pull-quotes are the one place the sans face carries body text: it marks them
     as asides rather than argument. */
  blockquote { font-family: var(--sans); font-size: 9.6pt; line-height: 1.5;
               margin: 1.2em 0; padding: .7em 1.1em;
               border-left: 3.5px solid var(--accent); background: #f2f7f4;
               color: #24303a; page-break-inside: avoid; }
  blockquote p { margin: .35em 0; }
  /* Tables may break across pages — the glossary is 48 rows and cannot fit one —
     but a row never splits, and the header repeats on each continuation, which is
     the standard book and journal treatment. */
  table { font-family: var(--sans); border-collapse: collapse; width: 100%;
          margin: 1.1em 0; font-size: 8.6pt; line-height: 1.42;
          page-break-inside: auto; }
  thead { display: table-header-group; }
  tr { page-break-inside: avoid; break-inside: avoid; }
  th, td { border: 1px solid var(--rule); padding: 5px 8px; text-align: left;
           vertical-align: top; }
  th { background: #eef2f0; font-weight: 700; color: #24303a;
       font-size: 8.1pt; text-transform: uppercase; letter-spacing: .7px; }
  tr:nth-child(even) td { background: #fafbfc; }
  hr { border: 0; border-top: 1px solid var(--rule); margin: 1.8em 0; }
  /* Diagrams get the full printable width rather than the text measure: a wide
     figure scaled into a narrow column is what makes Mermaid output unreadable. */
  .mermaid { text-align: center; margin: 1.4em 0; page-break-inside: avoid;
             background: #fff; width: 112%; margin-left: -6%; }
  .caption { font-family: var(--sans); text-align: center; font-size: 8.4pt;
             color: var(--muted); margin: -.4em auto 1.7em; max-width: 82%;
             line-height: 1.45; page-break-before: avoid; }
  .caption strong { color: var(--accent); letter-spacing: .4px; }

  /* ------------------------------ front matter ------------------------- */
  .pagebreak { page-break-after: always; }
  .cover { page-break-after: always; margin: 0; text-align: center; }
  /* Sized by height so the cover fills the sheet; max-width keeps it inside the
     page box when the paper is narrower than the artwork's 3:4 ratio. */
  .cover img { display: block; margin: 0 auto; height: 95vh; width: auto;
               max-width: 100%; }
  .titlepage { page-break-after: always; text-align: center; padding-top: 24vh; }
  .titlepage h1 { font-size: 44pt; font-weight: 700; letter-spacing: -1.6px;
                  border: 0; color: var(--accent); padding: 0;
                  page-break-before: avoid; break-before: avoid;
                  margin-bottom: .12em; }
  .titlepage h2 { font-family: var(--sans); font-size: 13.5pt; border: 0;
                  font-weight: 300; color: #2c3742; margin-top: 0;
                  letter-spacing: .2px; }
  .titlepage em { display: block; margin: 2.6em 0; color: var(--muted);
                  font-size: 11pt; }
  .titlepage strong { display: block; font-family: var(--sans); margin-top: 3.6em;
                      font-size: 16pt; font-weight: 500; letter-spacing: .6px; }
  .titlepage code { display: block; margin-top: .55em; background: none;
                    color: var(--muted); font-size: 9.5pt; }
  .titlepage p:last-child { font-family: var(--sans); margin-top: 2.8em;
                            color: var(--muted); letter-spacing: 3px;
                            text-transform: uppercase; font-size: 8pt; }
  .copyright { page-break-after: always; font-family: var(--sans);
               font-size: 8.4pt; line-height: 1.55; color: #40484f;
               padding-top: 8vh; }
  .copyright strong { color: #24303a; }
  .dedication { page-break-after: always; text-align: center; padding-top: 32vh;
                font-style: italic; color: var(--muted); line-height: 2.1;
                font-size: 11pt; }
  .endmark { font-family: var(--sans); text-align: center; color: var(--muted);
             font-size: 8pt; letter-spacing: 3px; text-transform: uppercase;
             margin-top: 3em; }
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

  // Open each Part with a drop cap, on the first paragraph substantial enough
  // to carry one. Titles and copyright live inside divs, so they are skipped.
  document.querySelectorAll('#content > h1').forEach((heading) => {
    let el = heading.nextElementSibling;
    while (el && el.tagName !== 'P' && el.tagName !== 'H1') el = el.nextElementSibling;
    if (el && el.tagName === 'P' && el.textContent.trim().length > 30) {
      el.classList.add('opener');
    }
  });

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
  // 'base' rather than 'neutral': neutral renders every node the same grey, so
  // a reader cannot tell an input from a decision from a result. The palette is
  // shared with the per-diagram classDefs in BOOK.md — blue is lexical, purple
  // vector, amber structured, green the optimizer.
  const SANS = '-apple-system, "Helvetica Neue", "Segoe UI", Arial, sans-serif';
  mermaid.initialize({
    startOnLoad: false,
    theme: 'base',
    themeVariables: {
      fontFamily: SANS, fontSize: '15px',
      primaryColor: '#eaf2ee', primaryTextColor: '#16241d',
      primaryBorderColor: '#2f6f4f', lineColor: '#4d5a63',
      secondaryColor: '#f3f6f4', tertiaryColor: '#ffffff',
      clusterBkg: '#fbfcfb', clusterBorder: '#bccdc4',
      edgeLabelBackground: '#ffffff', textColor: '#1f2933',
      actorBkg: '#dff0e6', actorBorder: '#2f6f4f', actorTextColor: '#12241c',
      actorLineColor: '#9aa7b0',
      signalColor: '#3a4750', signalTextColor: '#1f2933',
      labelBoxBkgColor: '#dff0e6', labelBoxBorderColor: '#2f6f4f',
      labelTextColor: '#12241c', loopTextColor: '#1f2933',
      noteBkgColor: '#fff4d6', noteBorderColor: '#d9a441', noteTextColor: '#4b3a10',
      sequenceNumberColor: '#ffffff', activationBkgColor: '#cfe6da',
    },
    flowchart: { htmlLabels: true, useMaxWidth: true,
                 nodeSpacing: 48, rankSpacing: 50, padding: 12 },
    sequence: { useMaxWidth: true, actorFontFamily: SANS, noteFontFamily: SANS,
                messageFontFamily: SANS, actorFontSize: 14, messageFontSize: 13,
                noteFontSize: 12.5 },
  });
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
    # Absolute, because a relative path yields file://name.html, where Chrome
    # reads "name.html" as the *host* and silently prints a blank Letter page
    # instead of failing.
    out_path = os.path.abspath(args.out)
    html_path = os.path.splitext(out_path)[0] + ".html"
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
         f"--print-to-pdf={out_path}", f"file://{html_path}"],
        capture_output=True, text=True, timeout=600, check=False)

    if not os.path.exists(out_path):
        sys.exit(f"Chrome produced no PDF:\n{done.stderr.strip()[:900]}")
    if not args.html:
        os.remove(html_path)

    print(f"wrote {out_path} ({os.path.getsize(out_path) / 1024:.0f} KB)")
    if args.html:
        print(f"kept  {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
