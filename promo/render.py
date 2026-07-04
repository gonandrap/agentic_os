"""Render the 60-second Jarvis promo: real screenshots + deterministic HTML scenes
+ generated track → promo/out/jarvis-os-60s.mp4.

    uv run python promo/render.py [--skip-screens] [--skip-music]

Pipeline (see promo/README.md):
  1. capture_screens.py photographs the REAL dashboard over a seeded fixture fleet
  2. each scene HTML exposes window.seek(t); we screenshot 30 frames/second
  3. ffmpeg assembles frames + track.wav into H.264/AAC
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).parent
OUT = HERE / "out"
FRAMES = OUT / "frames"
SCREENS = OUT / "screens"
SCENES = HERE / "scenes"
FPS = 30
FINAL = OUT / "jarvis-os-60s.mp4"


def scene_url(name: str, **params) -> str:
    url = (SCENES / name).resolve().as_uri()
    return f"{url}?{urlencode(params)}" if params else url

def shot(name: str) -> str:  # screenshots referenced relative to scenes/ dir
    return (SCREENS / f"{name}.png").resolve().as_uri()


# The master timeline — 60.0s total. Captions live here so future cuts remix
# without touching scene internals.
def timeline() -> list[tuple[str, float, dict]]:
    return [
        ("title.html", 6.0, {}),
        ("fleet.html", 8.0, {}),
        ("showcase.html", 7.0, {
            "img": shot("dashboard_busy"), "dur": 7, "zoom": 1.07, "pan": 40,
            "cmd": 'jarvis wo create webapp "Fix password reset link expiring"',
            "caption": "one dashboard: fleet, attention, <b>everything live</b>",
        }),
        ("spawn.html", 7.0, {}),
        ("showcase.html", 6.0, {
            "img": shot("wo_detail"), "dur": 6, "zoom": 1.06, "pan": 260,
            "cmd": 'jarvis wo send wo-510139c1 "use the staging bucket"',
            "caption": "talk to a running worker — <b>context carries over</b>",
        }),
        ("neo.html", 8.0, {}),
        ("showcase.html", 7.0, {
            "img": shot("neo_tab"), "dur": 7, "zoom": 1.06, "pan": 330,
            "cmd": "jarvis neo review 2   # approve, or correct — it learns you",
            "caption": "review Neo's answers — <b>corrections teach it to be you</b>",
        }),
        ("showcase.html", 5.0, {
            "img": shot("dashboard_quiet"), "dur": 5, "zoom": 1.05, "pan": 0,
            "cmd": "jarvis status",
            "caption": 'review, approve — <span class="ok">● all quiet</span>',
        }),
        ("outro.html", 6.0, {}),
    ]


def render_frames() -> int:
    from playwright.sync_api import sync_playwright

    if FRAMES.exists():
        for f in FRAMES.glob("*.png"):
            f.unlink()
    FRAMES.mkdir(parents=True, exist_ok=True)

    frame = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(
            viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
        ).new_page()
        for name, dur, params in timeline():
            page.goto(scene_url(name, **params))
            page.wait_for_load_state("networkidle")
            n = round(dur * FPS)
            print(f"  🎬 {name:<16} {dur:>4}s → {n} frames")
            for i in range(n):
                page.evaluate(f"seek({i / FPS})")
                page.screenshot(path=str(FRAMES / f"f_{frame:05d}.png"))
                frame += 1
        browser.close()
    return frame


def assemble(total_frames: int) -> None:
    track, sfx = OUT / "track.wav", OUT / "sfx.wav"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(FPS), "-i", str(FRAMES / "f_%05d.png"),
    ]
    audio_inputs = [p for p in (track, sfx) if p.exists()]
    for p in audio_inputs:
        cmd += ["-i", str(p)]
    cmd += [
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if len(audio_inputs) == 2:
        # music + keystroke foley, summed then safety-limited
        cmd += ["-filter_complex",
                "[1:a][2:a]amix=inputs=2:duration=longest:normalize=0,"
                "alimiter=limit=0.95[a]",
                "-map", "0:v", "-map", "[a]"]
    if audio_inputs:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += [str(FINAL)]
    subprocess.run(cmd, check=True)
    dur = total_frames / FPS
    size = FINAL.stat().st_size / 1e6
    print(f"  ✅ {FINAL}  ({dur:.1f}s, {size:.1f} MB)")


def main(argv: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if "--skip-screens" not in argv or not SCREENS.exists():
        print("📸 capturing real UI screenshots …")
        subprocess.run([sys.executable, str(HERE / "capture_screens.py")], check=True)
    if "--skip-music" not in argv or not (OUT / "track.wav").exists():
        print("🎵 synthesizing track …")
        subprocess.run([sys.executable, str(HERE / "music.py")], check=True)
        print("⌨️  synthesizing keystroke foley …")
        subprocess.run([sys.executable, str(HERE / "sfx.py")], check=True, cwd=HERE)
    if "--skip-frames" in argv and FRAMES.exists():
        frames = len(list(FRAMES.glob("f_*.png")))
        print(f"🎞  reusing {frames} existing frames (--skip-frames)")
    else:
        print("🎞  rendering scenes …")
        frames = render_frames()
    print("📦 assembling with ffmpeg …")
    assemble(frames)


if __name__ == "__main__":
    main(sys.argv[1:])
