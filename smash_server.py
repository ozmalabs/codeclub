#!/usr/bin/env python3
"""
🏏 Club Smash Map Viewer — browse efficiency maps in your browser.

Serves the interactive Plotly HTML maps and static PNGs from benchmarks/maps/.
Provides an index page with thumbnails and links to all maps.

Usage:
    python smash_server.py              # auto-pick free port
    python smash_server.py --port 8888  # specific port
"""
from __future__ import annotations

import argparse
import html
import socket
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

MAPS_DIR = Path(__file__).parent / "benchmarks" / "maps"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def build_index_html() -> str:
    """Generate an index page listing all maps with thumbnails."""
    html_files = sorted(MAPS_DIR.glob("*.html"))
    png_files = sorted(MAPS_DIR.glob("*.png"))

    # Group by model name (strip extension)
    models: dict[str, dict[str, Path]] = {}
    for f in html_files:
        name = f.stem
        models.setdefault(name, {})["html"] = f
    for f in png_files:
        name = f.stem
        models.setdefault(name, {})["png"] = f

    # Categorize
    singles = {}
    overlays = {}
    comparisons = {}
    for name, files in sorted(models.items()):
        if name.startswith("overlay"):
            overlays[name] = files
        elif name.startswith("quant_") or name.startswith("sizes_"):
            comparisons[name] = files
        else:
            singles[name] = files

    def card(name: str, files: dict[str, Path], width: str = "280px") -> str:
        safe = html.escape(name)
        parts = [f'<div class="card" style="width:{width}">']
        if "png" in files:
            parts.append(
                f'<a href="/maps/{files["png"].name}">'
                f'<img src="/maps/{files["png"].name}" alt="{safe}" loading="lazy">'
                f'</a>'
            )
        parts.append(f'<div class="card-title">{safe}</div>')
        parts.append('<div class="card-links">')
        if "html" in files:
            parts.append(f'<a href="/maps/{files["html"].name}" class="btn">Interactive</a>')
        if "png" in files:
            parts.append(f'<a href="/maps/{files["png"].name}" class="btn btn-secondary">PNG</a>')
        parts.append("</div></div>")
        return "\n".join(parts)

    def section(title: str, items: dict, width: str = "280px") -> str:
        if not items:
            return ""
        cards = "\n".join(card(n, f, width) for n, f in items.items())
        return f'<h2>{title}</h2><div class="grid">{cards}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🏏 Club Smash — Efficiency Maps</title>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --orange: #d29922; --red: #f85149;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    line-height: 1.6; padding: 2rem;
  }}
  h1 {{
    font-size: 2rem; margin-bottom: 0.5rem;
    background: linear-gradient(135deg, var(--green), var(--accent));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .subtitle {{ color: var(--muted); margin-bottom: 2rem; font-size: 1.1rem; }}
  h2 {{
    color: var(--accent); font-size: 1.3rem;
    margin: 2rem 0 1rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }}
  .grid {{
    display: flex; flex-wrap: wrap; gap: 1.5rem;
  }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden;
    transition: transform 0.2s, border-color 0.2s;
  }}
  .card:hover {{
    transform: translateY(-4px); border-color: var(--accent);
  }}
  .card img {{
    width: 100%; height: auto; display: block;
    border-bottom: 1px solid var(--border);
  }}
  .card-title {{
    padding: 0.75rem 1rem 0.25rem; font-weight: 600;
    font-size: 0.95rem;
  }}
  .card-links {{
    padding: 0.25rem 1rem 0.75rem;
    display: flex; gap: 0.5rem;
  }}
  .btn {{
    display: inline-block; padding: 0.35rem 0.75rem;
    background: var(--accent); color: var(--bg);
    border-radius: 6px; text-decoration: none;
    font-size: 0.8rem; font-weight: 600;
    transition: opacity 0.2s;
  }}
  .btn:hover {{ opacity: 0.85; }}
  .btn-secondary {{
    background: var(--border); color: var(--text);
  }}
  .legend {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 1rem 1.5rem; margin: 1.5rem 0;
    display: inline-block;
  }}
  .legend h3 {{ color: var(--green); margin-bottom: 0.5rem; font-size: 1rem; }}
  .legend code {{ color: var(--orange); }}
  .legend p {{ color: var(--muted); font-size: 0.9rem; margin: 0.3rem 0; }}
  footer {{
    margin-top: 3rem; padding-top: 1rem;
    border-top: 1px solid var(--border);
    color: var(--muted); font-size: 0.85rem;
  }}
</style>
</head>
<body>

<h1>🏏 Club Smash — Model Efficiency Maps</h1>
<p class="subtitle">
  Turbo compressor–style maps showing where each model operates efficiently.
  Like a turbo map: find the sweet spot, avoid surge & choke.
</p>

<div class="legend">
  <h3>How to read these maps</h3>
  <p><strong>X-axis:</strong> Task difficulty (0 = trivial → 100 = PhD-level)</p>
  <p><strong>Y-axis:</strong> Task clarity (0 = vague chat → 100 = full skeleton + tests)</p>
  <p><strong>Colour:</strong> green = peak efficiency, red/black = out of range</p>
  <p><strong>★ star:</strong> model's sweet spot</p>
  <p><strong>◆ diamonds:</strong> benchmark tasks (green = ace it, red = fail)</p>
  <p>Dashed lines: <code style="color:#f66">red</code> = difficulty boundary,
     <code style="color:#68f">blue</code> = clarity floor</p>
</div>

{section("🏆 Overlay — All Models", overlays, "600px")}
{section("📊 Comparisons — Quantization & Size", comparisons, "450px")}
{section("🏏 Individual Models", singles)}

<footer>
  Generated by <code>smash_viz.py</code> · codeclub · Caveman not have H100, caveman only have club.
</footer>

</body>
</html>"""


class MapHandler(SimpleHTTPRequestHandler):
    """Serve index and map files."""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            content = build_index_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path.startswith("/maps/"):
            filename = self.path[6:]  # strip /maps/
            filepath = MAPS_DIR / filename
            if filepath.exists() and filepath.is_file():
                content = filepath.read_bytes()
                self.send_response(200)
                if filename.endswith(".html"):
                    ct = "text/html; charset=utf-8"
                elif filename.endswith(".png"):
                    ct = "image/png"
                else:
                    ct = "application/octet-stream"
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Quieter logging
        pass


def main():
    parser = argparse.ArgumentParser(
        description="🏏 Club Smash Map Viewer",
    )
    parser.add_argument("--port", type=int, default=0, help="Port (0 = auto)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    args = parser.parse_args()

    if not MAPS_DIR.exists():
        print(f"❌  No maps found at {MAPS_DIR}")
        print(f"   Run `python smash_viz.py` first to generate them.")
        sys.exit(1)

    port = args.port or find_free_port()
    server = HTTPServer((args.host, port), MapHandler)

    n_html = len(list(MAPS_DIR.glob("*.html")))
    n_png = len(list(MAPS_DIR.glob("*.png")))

    print(f"\n🏏  Club Smash Map Viewer")
    print(f"   Serving {n_html} interactive + {n_png} static maps")
    print(f"   http://{args.host}:{port}/")
    print(f"   Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n   Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
