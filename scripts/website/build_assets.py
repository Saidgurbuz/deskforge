"""Regenerate web assets from the paper repository.

Images are the paper's own files, only resized and re-encoded (never retouched).
Every output records the SHA-256 of its source in assets/manifest.json.

Usage: python scripts/website/build_assets.py --paper <paper repo> [--video <demo.mp4>]

Writes into docs/assets/. Needs pymupdf, pillow and ffmpeg (on PATH, or set
DESKFORGE_FFMPEG). The paper repository is not part of this repository.
"""
import argparse
import base64
import io
import os
import re
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]  # repository root
SITE = ROOT / "docs"                          # served by GitHub Pages
IMG = SITE / "assets" / "img"
VID = SITE / "assets" / "video"
FFMPEG = os.environ.get("DESKFORGE_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"

# (source relative to the paper repo, output stem, max widths)
RASTERS = [
    ("figures/samples/ubuntu_like.png", "presets/ubuntu_like", (960, 1920)),
    ("figures/samples/windows_redmond.png", "presets/windows_redmond", (960, 1920)),
    ("figures/samples/macos_tahoe_like.png", "presets/macos_tahoe_like", (960, 1920)),
    ("figures/samples/quartz_night.png", "presets/quartz_night", (960, 1920)),
    *[(f"figures/samples_gallery/{i}_{k}.png", f"samples/{i}_{k}", (960, 1920)) for i in range(5) for k in ("clean", "annot")],
    *[(f"figures/detector_examples/{a}_{k}.png", f"detector/{a}_{k}", (960, 1920)) for a in ("blender", "shotcut", "obs") for k in ("gt", "pred")],
    *[(f"figures/interaction_sources/{f}/{k}.png", f"interaction/{f}_{k}", (960, 1920)) for f in ("01_008", "03_023", "05_037") for k in ("before", "after")],
    ("figures/deskforge_gallery.jpg", "gallery", (1200, 2400)),
    ("figures/annotation_zoom.png", "annotation_zoom", (1000, 2000)),
    ("figures/screentag_crop.png", "screentag_crop", (800, 1600)),
]

# PDFs rendered to raster at the given output width (2x of the display width).
PDFS = [
    ("figures/deskforge_overview.pdf", "overview", 2400),
    ("figures/grounding_robustness.pdf", "grounding_robustness", 2200),
    *[(f"figures/trajectories/{t}.pdf", f"trajectories/{t}", 2200) for t in ("t5_openapps_043", "t5_openapps_015", "t5_infinity_gitlab_h8", "t5_infinity_figma_h8")],
    *[(f"figures/grounding_failures/gf_{i}.pdf", f"failures/gf_{i}", 2000) for i in range(4)],
    ("figures/grounding_failures/gf_legend.pdf", "failures/gf_legend", 2000),
]

# PDFs also exported as SVG: text and shapes stay vector, so the figure is sharp at any zoom.
# The width is the SVG's natural width (what the lightbox shows); it still scales from the viewBox.
SVGS = [
    ("figures/deskforge_overview.pdf", "overview", 2400),
]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def save_webp(im, stem, width, quality=86):
    # Named by the requested width so the page can reference fixed names; never upscaled.
    out = IMG / f"{stem}-{width}.webp"
    out.parent.mkdir(parents=True, exist_ok=True)
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    im.save(out, "WEBP", quality=quality, method=6)
    return out, im.size


def pdf_to_svg(pdf, out, width):
    """Vector SVG of a one-page PDF: glyphs as paths, embedded rasters re-encoded as WebP, white ground."""
    page = pymupdf.open(pdf)[0]
    svg = page.get_svg_image(matrix=pymupdf.Identity, text_as_path=True)
    height = width * page.rect.height / page.rect.width
    svg = re.sub(r'(<svg[^>]*?) width="[^"]*" height="[^"]*"', rf'\1 width="{width}" height="{height:.2f}"', svg, count=1)

    def webp(m):
        im = Image.open(io.BytesIO(base64.b64decode(re.sub(r"\s", "", m.group(2)))))
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=92, method=6)
        return m.group(1) + "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode() + '"'

    svg = re.sub(r'(<image[^>]*?href=")data:image/png;base64,([^"]+)"', webp, svg, flags=re.S)
    svg = re.sub(r"(<svg[^>]*>)", r'\1\n<rect width="100%" height="100%" fill="#fff"/>', svg, count=1)
    out.write_text(svg)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True, help="checkout of the paper repository")
    ap.add_argument("--video")
    a = ap.parse_args()
    paper = Path(a.paper)
    mpath = SITE / "assets" / "manifest.json"
    old = json.loads(mpath.read_text()) if mpath.exists() else {}
    commit = subprocess.run(["git", "-C", str(paper), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    manifest = {"paper_repo": paper.name, "paper_commit": commit or None, "images": {}, "video": old.get("video", {})}

    for src, stem, widths in RASTERS:
        p = paper / src
        im = Image.open(p).convert("RGB")
        outs = []
        for w in widths:
            out, size = save_webp(im, stem, w)
            outs.append({"file": str(out.relative_to(SITE)), "size": list(size)})
        manifest["images"][stem] = {"source": src, "source_sha256": sha(p), "source_size": [im.width, im.height], "outputs": outs}

    for src, stem, width in PDFS:
        p = paper / src
        page = pymupdf.open(p)[0]
        zoom = width / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        outs = []
        for w in (width // 2, width):
            out, size = save_webp(im, stem, w, quality=90)
            outs.append({"file": str(out.relative_to(SITE)), "size": list(size)})
        manifest["images"][stem] = {"source": src, "source_sha256": sha(p), "rendered_from_pdf": True, "outputs": outs}

    for src, stem, width in SVGS:
        p = paper / src
        out = pdf_to_svg(p, IMG / f"{stem}.svg", width)
        manifest["images"][f"{stem}-svg"] = {"source": src, "source_sha256": sha(p), "outputs": [{"file": str(out.relative_to(SITE))}]}

    # Link-preview card (Open Graph / Twitter): 1200x630 JPEG, overview figure centered on white.
    page = pymupdf.open(paper / "figures/deskforge_overview.pdf")[0]
    zoom = 2400 / page.rect.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    fig = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    card = Image.new("RGB", (1200, 630), "white")
    fig.thumbnail((1200 - 2 * 36, 630 - 2 * 30), Image.LANCZOS)
    card.paste(fig, ((1200 - fig.width) // 2, (630 - fig.height) // 2))
    card.save(IMG / "social-card.jpg", "JPEG", quality=90, optimize=True, progressive=True)
    manifest["images"]["social-card"] = {"source": "figures/deskforge_overview.pdf", "source_sha256": sha(paper / "figures/deskforge_overview.pdf"), "outputs": [{"file": "assets/img/social-card.jpg", "size": [1200, 630]}]}

    if a.video:
        v = Path(a.video)
        VID.mkdir(parents=True, exist_ok=True)
        # The original 1080p 30 fps file is used byte-for-byte; it is remuxed (no re-encode)
        # only if its index is not already at the front for streaming.
        out = VID / "long_horizon.mp4"
        head = v.read_bytes()[:1 << 20]
        if 0 <= head.find(b"moov") < head.find(b"mdat"):
            shutil.copyfile(v, out)
        else:
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(v), "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
        manifest["video"] = {"long_horizon.mp4": {"source_sha256": sha(v), "output_sha256": sha(out)}}
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "4", "-i", str(v), "-frames:v", "1", str(VID / "poster.png")], check=True)
        save_webp(Image.open(VID / "poster.png").convert("RGB"), "../video/poster", 1920, quality=85)
        (VID / "poster.png").unlink()

    mpath.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"{len(manifest['images'])} images, {len(manifest['video'])} videos")


if __name__ == "__main__":
    main()
