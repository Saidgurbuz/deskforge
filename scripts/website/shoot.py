"""Screenshot the page for visual checks: python scripts/website/shoot.py OUTDIR [--dark] [--mobile]

Needs playwright with Chromium (PLAYWRIGHT_BROWSERS_PATH). Prints page height, horizontal
overflow (scrollWidth vs viewport), and console/network errors."""
import http.server, socketserver, sys, threading, functools
from pathlib import Path
from playwright.sync_api import sync_playwright

SITE = str(Path(__file__).resolve().parents[2] / "docs")
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
dark = "--dark" in sys.argv; mobile = "--mobile" in sys.argv

H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=SITE)
H.log_message = lambda *a: None
srv = socketserver.TCPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

with sync_playwright() as p:
    b = p.chromium.launch()
    vp = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
    ctx = b.new_context(viewport=vp, device_scale_factor=1, color_scheme="dark" if dark else "light")
    pg = ctx.new_page()
    errs = []
    pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    pg.on("requestfailed", lambda r: errs.append(f"requestfailed: {r.url}"))
    pg.on("response", lambda r: errs.append(f"{r.status}: {r.url}") if r.status >= 400 else None)
    pg.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
    # reveal everything and load lazy images
    pg.evaluate("document.querySelectorAll('.reveal').forEach(e=>e.classList.add('in')); document.querySelectorAll('img[loading=lazy]').forEach(i=>i.loading='eager'); document.querySelectorAll('details').forEach(d=>d.open=true)")
    pg.wait_for_timeout(1500)
    pg.wait_for_load_state("networkidle")
    tag = ("m" if mobile else "d") + ("_dark" if dark else "")
    H_total = pg.evaluate("document.documentElement.scrollHeight")
    sw = pg.evaluate("document.documentElement.scrollWidth")
    step = vp["height"] * 2
    for i, y in enumerate(range(0, H_total, step)):
        pg.evaluate(f"window.scrollTo(0,{y})"); pg.wait_for_timeout(250)
        pg.screenshot(path=str(out / f"{tag}_{i:02d}.png"), clip={"x": 0, "y": y, "width": vp["width"], "height": min(step, H_total - y)}, full_page=True)
    print("height", H_total, "scrollWidth", sw, "viewport", vp["width"])
    print("\n".join(errs) or "no console errors")
    b.close()
srv.shutdown()
