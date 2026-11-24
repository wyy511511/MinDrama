#!/usr/bin/env python3
"""Micro-dramas with a consistent cast, rendered end to end on Nunchux Modelverse.

How the cast stays the same from shot to shot:
  refs       FLUX 3 Image Klein draws one reference sheet per character, prop and location.
  keyframes  FLUX 3 Klein Edit draws each shot's first frame from those sheets (up to 10 references), and a
             last frame when the shot needs a precise end state (a number that changes, a car that stops).
  clips      MiniMax H3 Fast animates each shot from its first (and last) frame, with dialogue and sound.
  assemble   ffmpeg joins the clips with timed subtitles, a title card and an end card with the receipts.

Steps, in order (each one keeps what already exists, so any of them can be re-run):
    python3 make_film.py plan                  # shot list, timing checks, cost estimate; no API calls
    python3 make_film.py requests              # every request body the run would send; no API calls
    python3 make_film.py credits               # your balance and limits
    python3 make_film.py refs                  # characters, props, locations
    python3 make_film.py keyframes             # first (and last) frames of every shot
    python3 make_film.py voices                # one draft clip per voice, to listen before rendering everything
    python3 make_film.py clips --pass draft    # 480p on the Radical Value tier
    python3 make_film.py assemble --pass draft
    python3 make_film.py review                # out/<story>/review.html: every image, prompt and clip
    python3 make_film.py all --pass final      # 768p on the Radical Speed tier, then the film

Re-roll with a new seed: --shots e1s04,e1s11 --redo (refs: refs --redo character:clerk).
--story picks the story file; --dry-run draws placeholders with ffmpeg (no key, no cost, out-dryrun/).
Every call is appended to out/<story>/receipts.jsonl with its latency and estimated cost, and a run stops before
the estimated spend would pass --budget (default $8, or BUDGET_USD in .env). Needs ffmpeg; standard library only.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import html
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_STORY = ROOT / "stories" / "rental-booking-paradox.json"
API = "https://api.nunchux.com"
UA = "mindrama/1.0"
IMAGE_MODEL = "nunchux-flux.3-image-klein"
EDIT_MODEL = "nunchux-flux.3-image-klein-edit"
VIDEO_MODEL = "nunchux-minimax-h3-fast-fl2va"
PASSES = {
    "draft": {"tier": "radical_value", "short_edge": 480},
    "final": {"tier": "radical_speed", "short_edge": 768},
}
CLIP_SECONDS = (4, 15)        # what MiniMax H3 Fast accepts per call
# Estimates, $ per megapixel (images) and $ per output second (video). Video: nunchux.ai/pricing, read 2026-11-23.
# Images: calibrated against the account balance on 2026-11-23. Text to image cost about $0.0014 per output MP;
# an edit is billed on the output and every reference image together, about $0.006 per MP on Radical Speed
# (Radical Value figures are scaled from the price list). Receipts call these estimates; the balance is the truth.
T2I_PER_MP = {"radical_speed": 0.0014, "radical_value": 0.001}
EDIT_PER_MP = {"radical_speed": 0.006, "radical_value": 0.004}
VIDEO_PER_S = {("radical_speed", 480): 0.022, ("radical_speed", 768): 0.030,
               ("radical_value", 480): 0.018, ("radical_value", 768): 0.024}
FONTS = {
    "text": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "title": ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/Library/Fonts/Arial Black.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/ariblk.ttf"],
}

OUT = ROOT / "out"            # set in main(): out/<story>/ or out-dryrun/<story>/
_print_lock = threading.Lock()


class ApiError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- request bodies (shared by real calls and previews)
def image_body(prompt: str, w: int, h: int, tier: str, seed: int) -> dict:
    return {"model": IMAGE_MODEL, "prompt": prompt, "width": w, "height": h, "tier": tier, "seed": seed,
            "response_format": "url"}


def edit_body(prompt: str, images: list[str], w: int, h: int, tier: str, seed: int) -> dict:
    return {"model": EDIT_MODEL, "prompt": prompt, "image": images[:10], "width": w, "height": h, "tier": tier,
            "seed": seed, "response_format": "url"}


def video_body(prompt: str, first_url: str, last_url: str | None, duration: float, short_edge: int, aspect: str,
               tier: str, seed: int) -> dict:
    refs = [{"role": "first_frame", "url": first_url}]
    if last_url:
        refs.append({"role": "last_frame", "url": last_url})
    return {"model": VIDEO_MODEL, "prompt": prompt, "tier": tier, "duration": duration, "seed": seed, "refs": refs,
            "extra": {"short_edge": short_edge, "aspect_ratio": aspect}, "response_format": "url"}


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 120,
              attempts: int = 5) -> dict:
    """JSON over HTTPS. Retries 429 and 5xx with backoff (honouring Retry-After), as the API's docs ask.
    A POST that times out is not retried: a synchronous generation may still have run and been billed."""
    data = json.dumps(body).encode() if body is not None else None
    path = urllib.parse.urlparse(url).path
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:1500]
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                wait = float(e.headers.get("Retry-After") or 2 ** i)
                log(f"    {e.code} from {path}; retrying in {wait:.0f} s ({text[:160]})")
                time.sleep(wait)
                continue
            raise ApiError(f"{method} {path} answered {e.code}: {text}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if method == "GET" and i < attempts - 1:
                time.sleep(2 ** i)
                continue
            raise ApiError(f"{method} {path} failed: {e}") from None
    raise ApiError(f"{method} {path}: out of retries")


def output_of(r: dict) -> dict:
    d = (r.get("data") or [{}])[0]
    if d.get("url"):
        return {"url": d["url"]}
    if d.get("b64_json"):
        return {"b64": d["b64_json"]}
    raise ApiError(f"no url or b64_json in the answer: {json.dumps(r)[:400]}")


def trimmed(r: dict) -> dict:
    """The answer without base64 payloads, for the receipts."""
    return json.loads(re.sub(r'"b64_json":\s*"[^"]{200,}"', '"b64_json": "<omitted>"', json.dumps(r)))


# ---------------------------------------------------------------- providers
class Nunchux:
    """Nunchux Modelverse (docs.nunchux.ai). Images and the optimized video models answer synchronously."""
    dry = False

    def __init__(self, key: str):
        self.h = {"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA}

    def image(self, prompt, w, h, tier, seed) -> dict:
        r = http_json("POST", f"{API}/v1/images/generations", self.h, image_body(prompt, w, h, tier, seed), timeout=240)
        return {"output": output_of(r), "raw": trimmed(r)}

    def edit(self, prompt, images, w, h, tier, seed) -> dict:
        r = http_json("POST", f"{API}/v1/images/edits", self.h, edit_body(prompt, images, w, h, tier, seed), timeout=240)
        return {"output": output_of(r), "raw": trimmed(r)}

    def video(self, prompt, first_url, last_url, duration, short_edge, aspect, tier, seed) -> dict:
        body = video_body(prompt, first_url, last_url, duration, short_edge, aspect, tier, seed)
        r = http_json("POST", f"{API}/v1/videos/generations", self.h, body, timeout=900, attempts=4)
        if not r.get("data"):
            task = r.get("id") or r.get("task_id")
            if not task:
                raise ApiError(f"no video in the answer: {json.dumps(r)[:400]}")
            r = self._poll(str(task))
        return {"output": output_of(r), "raw": trimmed(r)}

    def _poll(self, task: str, timeout_s: float = 900) -> dict:
        """The docs call this model synchronous; if it ever answers with a task instead, look for it at the
        usual places. Neither route is documented for this model, so an error names the raw answer."""
        deadline = time.time() + timeout_s
        routes = [f"{API}/v1/videos/generations/{task}", f"{API}/v1/tasks/{task}"]
        while time.time() < deadline:
            for url in list(routes):
                try:
                    r = http_json("GET", url, self.h, timeout=60, attempts=2)
                except ApiError as e:
                    if "answered 404" in str(e):
                        routes.remove(url)
                        continue
                    raise
                if r.get("data"):
                    return r
                status = str(r.get("status", "")).lower()
                if status in ("failed", "error", "canceled", "cancelled", "expired"):
                    raise ApiError(f"task {task} ended {status}: {json.dumps(r)[:400]}")
            if not routes:
                raise ApiError(f"the API answered with task {task} and no documented route to poll it")
            time.sleep(3)
        raise ApiError(f"task {task} did not finish in {timeout_s:.0f} s")

    def credits(self) -> dict:
        try:
            return http_json("GET", f"{API}/v1/credits", self.h, timeout=30, attempts=2)
        except ApiError as e:
            return {"error": str(e)[:300]}


class DryRun:
    """Placeholder media drawn by ffmpeg, so the whole pipeline (and the page) can be checked with no key."""
    dry = True

    def __init__(self, scratch: Path):
        self.scratch = scratch
        scratch.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.lock = threading.Lock()

    def _next(self, ext: str) -> Path:
        with self.lock:
            self.n += 1
            return self.scratch / f"dry-{os.getpid()}-{self.n:03d}{ext}"

    @staticmethod
    def _color(text: str) -> str:
        return "0x%06x" % (int(hashlib.md5(text.encode()).hexdigest()[:6], 16) & 0x5f5f5f)

    def _still(self, prompt: str, w: int, h: int, label: str) -> dict:
        time.sleep(random.uniform(0.2, 0.6))
        out = self._next(".png")
        cols = max(24, int(w / 22))
        text_card(out, w, h, None, [(f"DRY RUN · {label}", "text", 30, "h*0.40"),
                                    ("\n".join(textwrap.wrap(prompt, cols)[:7]), "text", 20, "h*0.40+56")],
                  bg=self._color(prompt))
        return {"output": {"url": f"dryrun://{out}"}, "raw": {"dry_run": True}}

    def image(self, prompt, w, h, tier, seed):
        return self._still(prompt, w, h, "text to image")

    def edit(self, prompt, images, w, h, tier, seed):
        return self._still(prompt, w, h, f"edit, {len(images)} refs")

    def video(self, prompt, first_url, last_url, duration, short_edge, aspect, tier, seed):
        time.sleep(random.uniform(0.4, 1.0))
        w, h = frame_size(short_edge, aspect)
        out = self._next(".mp4")
        src = first_url[len("dryrun://"):] if first_url.startswith("dryrun://") else None
        inputs = ["-loop", "1", "-i", src] if src else ["-f", "lavfi", "-i", f"color=c=0x202024:s={w}x{h}:r=24"]
        run(["ffmpeg", "-y", "-v", "error", *inputs, "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
             "-t", str(duration), "-vf", f"scale={w}:{h},setsar=1,fps=24,format=yuv420p",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)])
        return {"output": {"url": f"dryrun://{out}"}, "raw": {"dry_run": True}}

    def credits(self) -> dict:
        return {"dry_run": True}


# ---------------------------------------------------------------- local media helpers
def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-800:]}")


def probe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(p.stdout or "{}")


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def duration_of(path: Path) -> float:
    return float(probe(path).get("format", {}).get("duration") or 0)


def frame_size(short_edge: int, aspect: str) -> tuple[int, int]:
    a, b = (int(x) for x in aspect.split(":"))
    long_edge = round(short_edge * max(a, b) / min(a, b) / 2) * 2
    return (short_edge, long_edge) if a < b else (long_edge, short_edge)


def font(kind: str) -> str | None:
    """A copy of a system font under the output folder, so filter arguments never hold spaces."""
    dest = OUT / "_fonts" / f"{kind}.ttf"
    if dest.exists():
        return str(dest)
    for cand in FONTS[kind]:
        if Path(cand).exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(cand, dest)
            return str(dest)
    return None


def drawtext(textfile: Path, kind: str, size: int, y: str, color: str = "white", box: bool = False,
             enable: tuple[float, float] | None = None) -> str:
    f = font(kind)
    parts = [f"textfile={textfile}", "expansion=none", f"fontsize={size}", f"fontcolor={color}",
             "x=(w-text_w)/2", f"y={y}", "line_spacing=10", "text_align=C"]   # text_align: ffmpeg 6.1+
    if f:
        parts.append(f"fontfile={f}")
    if box:
        parts += ["box=1", "boxcolor=black@0.5", "boxborderw=14"]
    if enable:
        parts.append(f"enable='between(t,{enable[0]:.2f},{enable[1]:.2f})'")
    return "drawtext=" + ":".join(parts)


def text_file(text: str) -> Path:
    tmp = OUT / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"t-{hashlib.md5(text.encode()).hexdigest()[:12]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "24",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def text_card(out: Path, w: int, h: int, seconds: float | None, blocks: list[tuple[str, str, int, str]],
              bg: str = "0x0b0b0d") -> Path:
    """A still (seconds=None) or a silent video segment with centered lines of text.
    blocks: (text, font kind, size, y expression)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = ",".join(drawtext(text_file(t), k, s, y) for t, k, s, y in blocks)
    if seconds is None:
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h}", "-vf", vf,
             "-frames:v", "1", str(out)])
    else:
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h}:r=24:d={seconds}",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", f"{vf},format=yuv420p", "-t", str(seconds),
             *ENCODE, "-shortest", str(out)])
    return out


def to_jpeg(src: Path) -> Path:
    dst = src.with_suffix(".jpg") if src.suffix.lower() != ".jpg" else src
    if dst != src:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-q:v", "3", str(dst)])
    return dst


def data_uri(jpg: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpg.read_bytes()).decode()


EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}


def fetch(output: dict, dest_stem: Path) -> Path:
    """Save an API output right away: clip URLs can expire within the hour."""
    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    if "b64" in output:
        raw = base64.b64decode(output["b64"])
        ext = ".mp4" if raw[4:8] == b"ftyp" else ".png" if raw[:4] == b"\x89PNG" else ".jpg"
        dest = dest_stem.with_suffix(ext)
        dest.write_bytes(raw)
        return dest
    url = output["url"]
    if url.startswith("dryrun://"):
        src = Path(url[len("dryrun://"):])
        dest = dest_stem.with_suffix(src.suffix)
        shutil.copy(src, dest)
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        ext = EXT.get(ctype) or Path(urllib.parse.urlparse(url).path).suffix or ".bin"
        dest = dest_stem.with_suffix(ext)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- state
class Ledger:
    """receipts.jsonl: one row per API call. The budget counts calls that succeeded plus calls in flight."""

    def __init__(self, path: Path, budget: float):
        self.path, self.budget = path, budget
        self.lock = threading.Lock()
        self.pending = 0.0
        self.spent = sum(r.get("est_cost", 0) for r in self.rows() if r.get("status") == "ok")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def reserve(self, est: float, what: str) -> None:
        with self.lock:
            total = self.spent + self.pending + est
            if total > self.budget + 1e-9:
                raise BudgetExceeded(f"{what} would take the estimated spend to ${total:.2f}, over the "
                                     f"${self.budget:.2f} budget (raise it with --budget)")
            self.pending += est

    def record(self, row: dict, est: float) -> None:
        with self.lock:
            self.pending -= est
            if row["status"] == "ok":
                self.spent += est
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Manifest:
    """manifest.json: what exists now (paths relative to the output folder), with the URL and seed behind each file."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for k, v in (("refs", {}), ("keyframes", {}), ("clips", {}), ("runs", []), ("films", {})):
            self.data.setdefault(k, v)

    def get(self, section: str, key: str, pass_: str | None = None) -> dict | None:
        sec = self.data[section].get(pass_, {}) if pass_ else self.data[section]
        return sec.get(key)

    def put(self, section: str, key: str, value: dict, pass_: str | None = None) -> None:
        with self.lock:
            sec = self.data[section].setdefault(pass_, {}) if pass_ else self.data[section]
            sec[key] = value
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- story → prompts
def seed_for(story: dict, key: str, attempt: int) -> int:
    return int(hashlib.sha1(f"{story['seed']}:{key}".encode()).hexdigest()[:8], 16) % 1_000_000 + attempt


def sizes(story: dict) -> dict:
    kf = story["keyframe_size"]
    return {"character": story.get("sheet_size", [896, 1152]), "prop": story.get("prop_size", [1024, 1024]),
            "location": story.get("location_size", kf), "keyframe": kf}


def on_screen(story: dict, cid: str) -> bool:
    return not story["characters"][cid].get("vo")


def character_sheet_prompt(story: dict, c: dict, w: int, h: int) -> str:
    if w > h:
        layout = ("Character turnaround sheet of one person on a seamless plain light-grey (#DCDCDC) background, "
                  "landscape layout: a large head close-up facing the camera on the left; full-body front, side and "
                  "back views in the middle; a row of three head close-ups along the bottom (front, three-quarter, "
                  "profile) with a neutral expression. The same single person in every view, identical proportions, "
                  "outfit and hair; feet fully visible; even diffuse light, no shadows.")
    else:
        layout = ("Character reference photo of one person: full body, standing, facing the camera, relaxed neutral "
                  "expression, plain light-grey studio backdrop, soft even light, sharp focus on the face.")
    pose = c.get("pose", "arms relaxed at the sides")
    return (f"{layout} {c['name']}: {c['look'].rstrip('.')}. Outfit: {c['outfit']}. Posture upright and relaxed, "
            f"{pose}, the other hand relaxed at the side. Photoreal live-action: real skin and fabric texture. "
            f"No text, no logos, no jewelry or accessories beyond the description.")


def prop_sheet_prompt(p: dict, w: int, h: int) -> str:
    layout = ("Three-view reference sheet of one object on a seamless plain light-grey (#DCDCDC) background, three "
              "panels side by side: front, back, and a 45-degree three-quarter view; the same single object in every "
              "panel with identical details; even soft light, no shadows; no people, no hands."
              if w > h else "Product reference photo of one object on a plain light-grey backdrop, soft even light.")
    marks = p.get("marks")
    return (f"{layout} {p['look'].rstrip('.')}. Photoreal product photography. "
            + (f"The only markings: {marks}." if marks else "No text or logos."))


def location_prompt(story: dict, loc: dict) -> str:
    return (f"Empty establishing shot with no people: {loc['look'].rstrip('.')}. {loc.get('light', '')} "
            f"{story['style']} {story['avoid']}").replace("  ", " ")


def ref_items(story: dict) -> list[tuple[str, str, str, list[int]]]:
    """(manifest key, kind, prompt, size) for every reference sheet."""
    sz = sizes(story)
    items = []
    for cid, c in story["characters"].items():
        if on_screen(story, cid):
            items.append((f"character:{cid}", "character", c.get("sheet_prompt")
                          or character_sheet_prompt(story, c, *sz["character"]), sz["character"]))
    for pid, p in story.get("props", {}).items():
        items.append((f"prop:{pid}", "prop", p.get("sheet_prompt") or prop_sheet_prompt(p, *sz["prop"]), sz["prop"]))
    for lid, loc in story["locations"].items():
        items.append((f"location:{lid}", "location", loc.get("plate_prompt") or location_prompt(story, loc),
                      sz["location"]))
    return items


def shot_refs(story: dict, shot: dict) -> list[str]:
    """The manifest keys of a shot's reference images, in the order its prompt names them (Image 1, 2…)."""
    keys = [f"character:{c}" for c in shot.get("characters", []) if on_screen(story, c)]
    keys += [f"prop:{p}" for p in shot.get("props", [])]
    if shot.get("location_ref", True):
        keys.append(f"location:{shot['location']}")
    return keys[:10]


def keyframe_prompt(story: dict, shot: dict) -> str:
    w, h = story["keyframe_size"]
    sheet_w, sheet_h = sizes(story)["character"]
    parts = [story["style"], f"A {'vertical' if h > w else 'widescreen'} {story['aspect_ratio']} film frame, "
             f"{shot['shot']}.", shot["frame"].rstrip(".") + "."]
    for i, key in enumerate(shot_refs(story, shot), 1):
        kind, name = key.split(":")
        if kind == "character":
            c = story["characters"][name]
            outfit = shot.get("wardrobe", {}).get(name, c["outfit"])
            sheet = " (a reference sheet with several views; draw this person once)" if sheet_w > sheet_h else ""
            parts.append(f"{c['name']} is the person in Image {i}{sheet}: the same face, hair, skin tone and build, "
                         f"wearing {outfit}.")
        elif kind == "prop":
            parts.append(f"{story['props'][name]['name']} looks exactly like the object in Image {i}.")
        else:
            loc = story["locations"][name]
            parts.append(f"The setting is the place in Image {i}. {loc.get('light', '')}".strip())
    parts.append(f"The only readable text: {shot['text']}." if shot.get("text") else "No readable text anywhere.")
    parts.append(story["avoid"])
    return " ".join(parts)


def last_refs(shot: dict) -> list[str]:
    """Reference sheets the last frame needs beyond the first frame: something that only appears by the end
    (e1s09: the sedan pulls up into a lane that was empty in the first frame)."""
    return list(shot.get("last_refs", []))[:9]


def last_frame_prompt(story: dict, shot: dict) -> str:
    named = ""
    for i, key in enumerate(last_refs(shot), 2):
        kind, name = key.split(":")
        label = (story["characters"][name]["name"] if kind == "character" else
                 story["props"][name]["name"] if kind == "prop" else story["locations"][name].get("name", name))
        named += f" {label} looks exactly like Image {i}."
    return (f"Image 1 is the first frame of this shot. {shot['last_frame'].rstrip('.')}.{named} Keep the people, faces, "
            f"outfits, positions, light and framing identical. "
            + (f"The only readable text: {shot['text']}. " if shot.get("text") else "") + story["avoid"])


def chain_prompt(story: dict, shot: dict) -> str:
    keys = shot_refs(story, shot)
    names = ", ".join(f"Image {i + 2} is {story['characters'][k.split(':')[1]]['name']}"
                      for i, k in enumerate(keys) if k.startswith("character:"))
    return (f"Image 1 is the last frame of the previous shot. Redraw it exactly, same composition, framing and "
            f"lighting, with every face, hairstyle and outfit matched to the reference images ({names}). "
            f"{story['avoid']}")


def speech(story: dict, shot: dict) -> str:
    if not shot.get("line"):
        return "No one speaks."
    c = story["characters"][shot["speaker"]]
    if c.get("vo"):
        return (f'An off-screen narrator who never appears, in {c["voice"]}, says: "{shot["line"]}" '
                f"No one on screen speaks; every mouth stays closed.")
    how = f" ({shot['delivery']})" if shot.get("delivery") else ""
    return f'{c["name"]}{how}, in {c["voice"]}, says: "{shot["line"]}" Only {c["name"]} speaks.'


def clip_prompt(story: dict, shot: dict, has_last: bool) -> str:
    parts = ["Image 1 is the first frame." + (" Image 2 is the last frame; end exactly on it." if has_last else ""),
             shot["action"].rstrip(".") + ".", f"Camera: {shot['camera']}.", speech(story, shot)]
    if shot.get("sound"):
        parts.append(f"Sound: {shot['sound']}.")
    # H3 draws whoever the action mentions: "her phone" put an unknown woman into the Clerk's close-up (e1s03, draft)
    parts.append(f"{story['clip_audio']} Normal speed, no slow motion. Only the people already in Image 1 appear; "
                 f"no one else enters the frame. Keep every face, hairstyle and outfit exactly as in Image 1. "
                 f"No captions or subtitles.")
    return " ".join(parts)


def t2i_cost(w: int, h: int, tier: str) -> float:
    return w * h / 1e6 * T2I_PER_MP[tier]


def ref_pixels(story: dict, key: str) -> int:
    w, h = sizes(story)[key.split(":")[0]]
    return w * h


def edit_cost(story: dict, w: int, h: int, tier: str, ref_keys: list[str], extra_inputs: int = 0) -> float:
    """Output plus every input image, billed by the megapixel. extra_inputs: further inputs of the frame's own size."""
    pixels = w * h * (1 + extra_inputs) + sum(ref_pixels(story, k) for k in ref_keys)
    return pixels / 1e6 * EDIT_PER_MP[tier]


def check_story(story: dict) -> list[str]:
    """Problems that would fail a call or confuse the models, found before any money is spent."""
    out = []
    chars, props, locs = story["characters"], story.get("props", {}), story["locations"]
    ids = [s["id"] for s in story["shots"]]
    if len(ids) != len(set(ids)):
        out.append("shot ids repeat")
    for s in story["shots"]:
        d = float(s.get("duration", 6))
        if not CLIP_SECONDS[0] <= d <= CLIP_SECONDS[1]:
            out.append(f"{s['id']}: duration {d} s is outside {CLIP_SECONDS[0]}–{CLIP_SECONDS[1]} s")
        for c in s.get("characters", []):
            if c not in chars:
                out.append(f"{s['id']}: unknown character {c}")
        for p in s.get("props", []):
            if p not in props:
                out.append(f"{s['id']}: unknown prop {p}")
        if s["location"] not in locs:
            out.append(f"{s['id']}: unknown location {s['location']}")
        if s.get("line") and s.get("speaker") not in chars:
            out.append(f"{s['id']}: the line has no known speaker")
        if len(shot_refs(story, s)) > 10:
            out.append(f"{s['id']}: more than 10 reference images")
        for k in last_refs(s):
            kind, name = k.split(":") if ":" in k else ("", k)
            pool = {"character": chars, "prop": props, "location": locs}.get(kind, {})
            if name not in pool:
                out.append(f"{s['id']}: unknown last-frame reference {k}")
    return out


# ---------------------------------------------------------------- the pipeline
class Film:
    def __init__(self, story: dict, provider, ledger: Ledger, manifest: Manifest, workers: int):
        self.story, self.p, self.ledger, self.m, self.workers = story, provider, ledger, manifest, workers
        self.shots = {s["id"]: s for s in story["shots"]}
        self.frames_base = ""   # set from --frames-base

    def call(self, step: str, item: str, model: str, tier: str, est: float, fn, params: dict, pass_=None) -> dict:
        """One API call, with its receipt."""
        self.ledger.reserve(est, f"{step} {item}")
        t0 = time.time()
        row = {"ts": now_iso(), "step": step, "item": item, "pass": pass_, "model": model, "tier": tier,
               "est_cost": round(est, 4), "params": params, "dry_run": self.p.dry}
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001 - every failure gets a receipt
            row.update(status="error", seconds=round(time.time() - t0, 2), error=str(e)[:600])
            self.ledger.record(row, est)
            raise
        row.update(status="ok", seconds=round(time.time() - t0, 2), answer=res.get("raw"))
        self.ledger.record(row, est)
        return res

    def parallel(self, fn, keys: list[str]) -> list[str]:
        """Run fn(key) for each key, a few at a time. Returns the keys that failed; a budget stop ends the batch."""
        failed: list[str] = []
        with cf.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(fn, k): k for k in keys}
            for fut in cf.as_completed(futs):
                k = futs[fut]
                try:
                    fut.result()
                except BudgetExceeded as e:
                    log(f"  ■ budget: {e}")
                    for f in futs:
                        f.cancel()
                    failed.append(k)
                except Exception as e:  # noqa: BLE001
                    log(f"  ✗ {k}: {e}")
                    failed.append(k)
        return failed

    @staticmethod
    def image_cost(w: int, h: int, tier: str) -> float:
        return t2i_cost(w, h, tier)

    def edit_cost(self, w: int, h: int, tier: str, ref_keys: list[str], extra_inputs: int = 0) -> float:
        return edit_cost(self.story, w, h, tier, ref_keys, extra_inputs)

    def _attempt(self, section: str, key: str, pass_=None) -> int:
        """The next attempt number. The files of the previous attempt are kept beside it as name.aN.ext,
        so a re-roll can be compared with what it replaced, and rolled back."""
        prev = self.m.get(section, key, pass_)
        if not prev:
            return 0
        for f in {prev.get("file"), prev.get("jpg")} - {None}:
            p = OUT / f
            if p.exists():
                p.rename(p.with_name(f"{p.stem}.a{prev['attempt']}{p.suffix}"))
        return prev["attempt"] + 1

    # references
    def refs(self, redo: set[str]) -> list[str]:
        tier = self.story["image_tier"]
        todo = {it[0]: it for it in ref_items(self.story) if it[0] in redo or not self.m.get("refs", it[0])}
        if not todo:
            log("refs: all present")
            return []
        log(f"refs: {len(todo)} to draw")

        def one(key):
            _, kind, prompt, (w, h) = todo[key]
            attempt = self._attempt("refs", key)
            seed = seed_for(self.story, key, attempt)
            res = self.call("ref", key, IMAGE_MODEL, tier, self.image_cost(w, h, tier),
                            lambda: self.p.image(prompt, w, h, tier, seed), {"width": w, "height": h, "seed": seed})
            path = fetch(res["output"], OUT / "refs" / key.replace(":", "-"))
            jpg = to_jpeg(path)
            self.m.put("refs", key, {"file": rel(path), "jpg": rel(jpg), "url": res["output"].get("url"), "seed": seed,
                                     "attempt": attempt, "prompt": prompt, "made": now_iso()})
            log(f"  ✓ {key}")

        return self.parallel(one, list(todo))

    # first and last frames
    def keyframes(self, ids: list[str], redo: bool) -> list[str]:
        w, h = self.story["keyframe_size"]
        tier = self.story["image_tier"]
        todo = [i for i in ids if self.shots[i].get("first_frame") != "chain" and (
            redo or not self.m.get("keyframes", i)
            or (self.shots[i].get("last_frame") and not self.m.get("keyframes", f"{i}:last")))]
        if not todo:
            log("keyframes: all present")
            return []
        missing = sorted({k for i in todo for k in shot_refs(self.story, self.shots[i]) if not self.m.get("refs", k)})
        if missing:
            raise RuntimeError(f"reference sheets missing: {', '.join(missing)} (run: refs)")
        log(f"keyframes: {len(todo)} shots to draw")

        def one(sid):
            shot = self.shots[sid]
            fresh = redo or not self.m.get("keyframes", sid)
            if fresh:
                prompt = keyframe_prompt(self.story, shot)
                images = [data_uri(OUT / self.m.get("refs", k)["jpg"]) for k in shot_refs(self.story, shot)]
                attempt = self._attempt("keyframes", sid)
                seed = seed_for(self.story, f"keyframe:{sid}", attempt)
                res = self.call("keyframe", sid, EDIT_MODEL, tier, self.edit_cost(w, h, tier, shot_refs(self.story, shot)),
                                lambda: self.p.edit(prompt, images, w, h, tier, seed),
                                {"width": w, "height": h, "seed": seed, "refs": shot_refs(self.story, shot)})
                path = fetch(res["output"], OUT / "keyframes" / sid)
                jpg = to_jpeg(path)
                self.m.put("keyframes", sid, {"file": rel(path), "jpg": rel(jpg), "url": res["output"].get("url"),
                                              "seed": seed, "attempt": attempt, "prompt": prompt,
                                              "refs": shot_refs(self.story, shot), "made": now_iso()})
                log(f"  ✓ {sid}")
            if shot.get("last_frame") and (fresh or not self.m.get("keyframes", f"{sid}:last")):
                first = OUT / self.m.get("keyframes", sid)["jpg"]
                prompt = last_frame_prompt(self.story, shot)
                attempt = self._attempt("keyframes", f"{sid}:last")
                seed = seed_for(self.story, f"keyframe:{sid}:last", attempt)
                images = [data_uri(first)] + [data_uri(OUT / self.m.get("refs", k)["jpg"]) for k in last_refs(shot)]
                res = self.call("lastframe", sid, EDIT_MODEL, tier,
                                self.edit_cost(w, h, tier, last_refs(shot), extra_inputs=1),
                                lambda: self.p.edit(prompt, images, w, h, tier, seed),
                                {"width": w, "height": h, "seed": seed, "from": sid, "refs": last_refs(shot)})
                path = fetch(res["output"], OUT / "keyframes" / f"{sid}-last")
                jpg = to_jpeg(path)
                self.m.put("keyframes", f"{sid}:last", {"file": rel(path), "jpg": rel(jpg),
                                                        "url": res["output"].get("url"), "seed": seed,
                                                        "attempt": attempt, "prompt": prompt, "made": now_iso()})
                log(f"  ✓ {sid} (last frame)")

        return self.parallel(one, todo)

    # clips
    def clips(self, pass_: str, ids: list[str], redo: bool) -> list[str]:
        cfg = PASSES[pass_]
        todo = [i for i in ids if redo or not self.m.get("clips", i, pass_)]
        if not todo:
            log(f"clips ({pass_}): all present")
            return []
        free = [i for i in todo if self.shots[i].get("first_frame") != "chain"]
        chained = [i for i in todo if self.shots[i].get("first_frame") == "chain"]
        secs = sum(float(self.shots[i].get("duration", 6)) for i in todo)
        log(f"clips ({pass_}, {cfg['tier']}, {cfg['short_edge']}p): {len(todo)} shots, {secs:.0f} s, "
            f"about ${secs * VIDEO_PER_S[(cfg['tier'], cfg['short_edge'])]:.2f}")
        failed = self.parallel(lambda sid: self.clip(pass_, sid, self.m.get("keyframes", sid)), free)
        for sid in chained:   # in story order: each starts from the last frame of the shot before it
            try:
                self.clip(pass_, sid, self.chain_frame(pass_, sid))
            except BudgetExceeded as e:
                log(f"  ■ budget: {e}")
                failed.append(sid)
                break
            except Exception as e:  # noqa: BLE001
                log(f"  ✗ {sid}: {e}")
                failed.append(sid)
        return failed

    def chain_frame(self, pass_: str, sid: str) -> dict:
        order = [s["id"] for s in self.story["shots"]]
        prev_id = order[order.index(sid) - 1] if order.index(sid) > 0 else None
        prev = self.m.get("clips", prev_id, pass_) if prev_id else None
        if not prev:
            raise RuntimeError(f"{sid} chains from the shot before it, which has no {pass_} clip yet")
        last = OUT / "frames" / pass_ / f"{prev_id}-last.jpg"
        last.parent.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.1", "-i", str(OUT / prev["file"]), "-frames:v", "1",
             "-q:v", "3", str(last)])
        shot = self.shots[sid]
        w, h = self.story["keyframe_size"]
        tier = self.story["image_tier"]
        images = [data_uri(last)] + [data_uri(OUT / self.m.get("refs", k)["jpg"])
                                     for k in shot_refs(self.story, shot) if k.startswith("character:")]
        seed = seed_for(self.story, f"chain:{sid}", 0)
        prompt = chain_prompt(self.story, shot)
        res = self.call("chain", sid, EDIT_MODEL, tier,
                        self.edit_cost(w, h, tier, [k for k in shot_refs(self.story, shot) if k.startswith("character:")],
                                       extra_inputs=1),
                        lambda: self.p.edit(prompt, images, w, h, tier, seed),
                        {"width": w, "height": h, "seed": seed, "from": prev_id}, pass_=pass_)
        path = fetch(res["output"], OUT / "keyframes" / f"{sid}-chain-{pass_}")
        jpg = to_jpeg(path)
        entry = {"file": rel(path), "jpg": rel(jpg), "url": res["output"].get("url"), "seed": seed, "attempt": 0,
                 "prompt": prompt, "chained_from": prev_id, "made": now_iso()}
        self.m.put("keyframes", f"{sid}@{pass_}", entry)
        return entry

    def frame_url(self, entry: dict) -> str:
        """Where the video endpoint reads a frame from. The image API's URLs are signed and answer 403 after about an
        hour, and the video endpoint then fails with a bare engine_error (final pass, 23 Nov: 17 of 21 clips). With
        frames_base set, frames are read from where the assets are published instead, after checking that the
        published file is the same size as the local one."""
        if not self.frames_base or self.p.dry:
            return entry["url"]
        url = f"{self.frames_base}/{entry['file']}"
        local = (OUT / entry["file"]).stat().st_size
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                total = int((r.headers.get("Content-Range") or "/0").rsplit("/", 1)[-1] or 0)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{url} answered {e.code}: publish the assets before rendering") from None
        if total != local:
            raise RuntimeError(f"{url} is {total} bytes, the local frame {local}: publish the current assets first")
        return url

    def clip(self, pass_: str, sid: str, first: dict | None) -> None:
        if not first or not first.get("url"):
            raise RuntimeError(f"{sid} has no first frame with a URL (run: keyframes)")
        last = self.m.get("keyframes", f"{sid}:last") if self.shots[sid].get("last_frame") else None
        cfg = PASSES[pass_]
        shot = self.shots[sid]
        prompt = clip_prompt(self.story, shot, bool(last))
        dur = float(shot.get("duration", 6))
        first_url = self.frame_url(first)
        last_url = self.frame_url(last) if last else None
        attempt = self._attempt("clips", sid, pass_)
        seed = seed_for(self.story, f"clip:{sid}", attempt)
        est = VIDEO_PER_S[(cfg["tier"], cfg["short_edge"])] * dur
        res = self.call("clip", sid, VIDEO_MODEL, cfg["tier"], est,
                        lambda: self.p.video(prompt, first_url, last_url, dur,
                                             cfg["short_edge"], self.story["aspect_ratio"], cfg["tier"], seed),
                        {"duration": dur, "short_edge": cfg["short_edge"], "seed": seed, "last_frame": bool(last),
                         "frames_from": "published" if self.frames_base else "image API"},
                        pass_=pass_)
        path = fetch(res["output"], OUT / "clips" / pass_ / sid)
        self.m.put("clips", sid, {"file": rel(path), "url": res["output"].get("url"), "seed": seed, "attempt": attempt,
                                  "prompt": prompt, "made": now_iso()}, pass_)
        log(f"  ✓ {sid}  ({pass_})")

    def voice_shots(self) -> list[str]:
        """The first shot each voice speaks in, so every voice can be heard before everything is rendered."""
        seen, ids = set(), []
        for s in self.story["shots"]:
            if s.get("line") and s["speaker"] not in seen:
                seen.add(s["speaker"])
                ids.append(s["id"])
        return ids

    # the film
    def assemble(self, pass_: str, music: Path | None = None) -> Path:
        W, H = self.story["output_size"]
        missing = [s["id"] for s in self.story["shots"] if not self.m.get("clips", s["id"], pass_)]
        if missing:
            raise RuntimeError(f"no {pass_} clip yet for {', '.join(missing)}")
        segdir = OUT / "film" / f"_{pass_}"
        segdir.mkdir(parents=True, exist_ok=True)
        title = self.story["title"]
        s = min(W, H) / 720
        title_lines = textwrap.wrap(title.upper(), 16)
        tsize = min(int(88 * s), int(0.88 * W / (0.72 * max(len(x) for x in title_lines))))
        subtitle = self.story.get("subtitle") or f"a {round(self.film_seconds())}-second micro-drama"
        block = len(title_lines) * tsize * 1.25          # rendered line height is about 1.25 × the font size
        top = int(H * 0.42 - block / 2)
        segs = [text_card(segdir / "00-title.mp4", W, H, 2.6, [
            ("\n".join(title_lines), "title", tsize, str(top)),
            (subtitle, "text", int(30 * s), str(int(top + block + 28 * s)))])]
        srt, t = [], 2.6
        for n, shot in enumerate(self.story["shots"], 1):
            src = OUT / self.m.get("clips", shot["id"], pass_)["file"]
            dst = segdir / f"{n:02d}-{shot['id']}.mp4"
            cues = self.normalize(src, dst, W, H, shot)
            srt += [(t + a, t + b, txt) for a, b, txt in cues]
            segs.append(dst)
            t += duration_of(dst)
        stats = self.stats(pass_)
        esize = int(tsize * 0.72)
        y1 = int(H * 0.28 + len(title_lines) * esize * 1.25 + 30 * s)
        segs.append(text_card(segdir / "99-end.mp4", W, H, 3.8, [
            ("\n".join(title_lines), "title", esize, str(int(H * 0.28))),
            (stats["line1"], "text", int(30 * s), str(y1)),
            (stats["line2"], "text", int(30 * s), str(int(y1 + 48 * s))),
            ("FLUX 3 Klein Edit + MiniMax H3 Fast", "text", int(26 * s), str(int(y1 + 128 * s))),
            ("on Nunchux Modelverse", "text", int(26 * s), str(int(y1 + 170 * s)))]))
        listing = segdir / "concat.txt"
        listing.write_text("".join(f"file '{p.resolve()}'\n" for p in segs), encoding="utf-8")
        film = OUT / "film" / f"{slug(title)}-{pass_}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
             "-movflags", "+faststart", str(film)])
        if music:
            mixed = film.with_name(film.stem + "-music.mp4")
            total = duration_of(film)
            run(["ffmpeg", "-y", "-v", "error", "-i", str(film), "-i", str(music), "-filter_complex",
                 f"[1:a]volume=0.16,afade=t=in:d=1.5,afade=t=out:st={max(0, total - 3):.2f}:d=3[m];"
                 f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(mixed)])
            os.replace(mixed, film)
        film.with_suffix(".srt").write_text("".join(
            f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{txt}\n\n" for i, (a, b, txt) in enumerate(srt, 1)),
            encoding="utf-8")
        self.m.data["films"][pass_] = {"file": rel(film), "seconds": round(duration_of(film), 2), "built": now_iso(),
                                       **{k: v for k, v in stats.items() if not k.startswith("line")}}
        self.m.save()
        log(f"film: {film}  ({duration_of(film):.1f} s)")
        return film

    def normalize(self, src: Path, dst: Path, W: int, H: int, shot: dict) -> list[tuple[float, float, str]]:
        """One clip at the film's size and frame rate, optionally cut to shot['cut'] = [in, out] seconds, with its
        line burned in as subtitles, one sentence at a time. Returns the subtitle cues (start, end, text)."""
        cut = shot.get("cut")
        length = (cut[1] - cut[0]) if cut else duration_of(src)
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
              f"setsar=1,fps=24")
        cues: list[tuple[float, float, str]] = []
        if shot.get("line") and self.story.get("burn_subtitles", True):
            s = min(W, H) / 720
            per_line = max(24, int(W / (21 * s)))
            margin = int(H * (0.17 if H > W else 0.07))
            sentences = [x for x in re.split(r"(?<=[.!?])\s+", shot["line"].strip()) if x]
            start, end = 0.25, max(0.5, length - 0.25)
            total = sum(len(x) for x in sentences)
            a = start
            for x in sentences:
                b = a + (end - start) * len(x) / total
                cues.append((a, b, x))
                a = b
            for a, b, x in cues:
                vf += "," + drawtext(text_file("\n".join(textwrap.wrap(x, per_line))), "text", int(36 * s),
                                     f"h-text_h-{margin}", box=True, enable=(a, b))
        cmd = ["ffmpeg", "-y", "-v", "error"] + (["-ss", str(cut[0]), "-to", str(cut[1])] if cut else []) + ["-i", str(src)]
        if has_audio(src):
            maps = ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            maps = ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        run(cmd + ["-vf", vf + ",format=yuv420p", *maps, *ENCODE, str(dst)])
        return cues

    def film_seconds(self) -> float:
        total = 0.0
        for s in self.story["shots"]:
            total += (s["cut"][1] - s["cut"][0]) if s.get("cut") else float(s.get("duration", 6))
        return total

    def stats(self, pass_: str) -> dict:
        rows = [r for r in self.ledger.rows() if r.get("status") == "ok"]
        all_in = sum(r["est_cost"] for r in rows)
        this_cut = sum(r["est_cost"] for r in rows if r["step"] in ("ref", "keyframe", "lastframe")
                       or (r["step"] in ("clip", "chain") and r.get("pass") == pass_))
        n = len(self.story["shots"])
        runs = [r for r in self.m.data["runs"] if r.get("pass") == pass_ and r.get("render_seconds")
                and r.get("clips_rendered", 0) >= 0.8 * n]
        clip_secs = [r["seconds"] for r in rows if r["step"] == "clip" and r.get("pass") == pass_]
        render = runs[-1]["render_seconds"] if runs else None
        dry = "dry run · " if self.p.dry else ""
        when = (f"{render / 60:.1f} min to render" if render else
                f"{statistics.median(clip_secs):.0f} s per clip" if clip_secs else "")
        return {"all_in_usd": round(all_in, 2), "this_cut_usd": round(this_cut, 2), "render_seconds": render,
                "median_clip_seconds": round(statistics.median(clip_secs), 1) if clip_secs else None,
                "line1": f"{dry}{n} shots · {round(self.film_seconds())} s",
                "line2": f"${all_in:.2f} all-in" + (f" · {when}" if when else "")}


def rel(p: Path) -> str:
    return str(p.relative_to(OUT))


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


# ---------------------------------------------------------------- commands without API calls
def plan(story: dict, budget: float) -> int:
    sz = sizes(story)
    tier = story["image_tier"]
    refs = ref_items(story)
    ref_cost = sum(t2i_cost(w, h, tier) for _, _, _, (w, h) in refs)
    n_last = sum(1 for s in story["shots"] if s.get("last_frame"))
    kw, kh = sz["keyframe"]
    kf_cost = (sum(edit_cost(story, kw, kh, tier, shot_refs(story, s)) for s in story["shots"])
               + n_last * edit_cost(story, kw, kh, tier, [], extra_inputs=1))
    secs = sum(float(s.get("duration", 6)) for s in story["shots"])
    print(f"{story['title']}: {len(story['shots'])} shots, {secs:.0f} s of clips, {story['aspect_ratio']}\n")
    for s in story["shots"]:
        line = ""
        if s.get("line"):
            who = story["characters"][s["speaker"]]
            line = f'{who["name"]}{" (V.O.)" if who.get("vo") else ""}: "{s["line"][:60]}{"…" if len(s["line"]) > 60 else ""}"'
        extra = " +last frame" if s.get("last_frame") else ""
        print(f"  {s['id']:<6} {s.get('scene', ''):<4} {float(s.get('duration', 6)):>4.0f}s  {s['shot'][:24]:<24} {line}{extra}")
    print(f"\nreference sheets: {', '.join(k for k, *_ in refs)}")
    print(f"\nestimated cost (nunchux.ai/pricing, 2026-11-23):")
    print(f"  {len(refs)} reference sheets                ${ref_cost:.2f}")
    print(f"  {len(story['shots'])} first frames + {n_last} last frames     ${kf_cost:.2f}")
    for name, cfg in PASSES.items():
        c = VIDEO_PER_S[(cfg["tier"], cfg["short_edge"])] * secs
        print(f"  clips, {name:<5} ({cfg['short_edge']}p, {cfg['tier']:<13})  ${c:.2f}")
    draft = VIDEO_PER_S[(PASSES["draft"]["tier"], 480)] * secs
    final = VIDEO_PER_S[(PASSES["final"]["tier"], 768)] * secs
    print(f"\n  one draft + one final, before re-rolls: ${ref_cost + kf_cost + draft + final:.2f}   (budget ${budget:.2f})")
    problems = check_story(story)
    for p in problems:
        print(f"  ! {p}")
    return 1 if problems else 0


def requests_preview(story: dict, man: Manifest, pass_: str) -> Path:
    """Every request body a full run would send, with references shown as file names instead of base64."""
    tier = story["image_tier"]
    w, h = story["keyframe_size"]
    cfg = PASSES[pass_]
    calls = []
    for key, kind, prompt, (rw, rh) in ref_items(story):
        calls.append({"step": "ref", "item": key, "POST": f"{API}/v1/images/generations",
                      "body": image_body(prompt, rw, rh, tier, seed_for(story, key, 0))})
    for s in story["shots"]:
        refs = shot_refs(story, s)
        calls.append({"step": "keyframe", "item": s["id"], "POST": f"{API}/v1/images/edits",
                      "body": edit_body(keyframe_prompt(story, s), [f"<data URI of refs/{k.replace(':', '-')}.jpg>"
                                                                     for k in refs], w, h, tier,
                                        seed_for(story, f"keyframe:{s['id']}", 0))})
        if s.get("last_frame"):
            calls.append({"step": "lastframe", "item": s["id"], "POST": f"{API}/v1/images/edits",
                          "body": edit_body(last_frame_prompt(story, s), [f"<data URI of keyframes/{s['id']}.jpg>"]
                                            + [f"<data URI of refs/{k.replace(':', '-')}.jpg>" for k in last_refs(s)],
                                            w, h, tier, seed_for(story, f"keyframe:{s['id']}:last", 0))})
    for s in story["shots"]:
        calls.append({"step": "clip", "item": s["id"], "POST": f"{API}/v1/videos/generations",
                      "body": video_body(clip_prompt(story, s, bool(s.get("last_frame"))),
                                         f"<URL of keyframe {s['id']}>",
                                         f"<URL of last frame {s['id']}>" if s.get("last_frame") else None,
                                         float(s.get("duration", 6)), cfg["short_edge"], story["aspect_ratio"],
                                         cfg["tier"], seed_for(story, f"clip:{s['id']}", 0))})
    out = OUT / "requests.preview.json"
    out.write_text(json.dumps({"story": story["title"], "pass": pass_, "headers": {"X-API-Key": "<NUNCHUX_API_KEY>",
                               "Content-Type": "application/json"}, "calls": calls}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return out


def review_page(story: dict, man: Manifest) -> Path:
    """A local page with every reference, frame, prompt and clip (with sound), to look and listen before going on."""
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    ar = story["aspect_ratio"].replace(":", " / ")
    parts = [f"<!doctype html><meta charset=utf-8><title>{e(story['title'])} · review</title>",
             "<style>body{font:14px/1.5 system-ui;background:#111;color:#eee;margin:24px}"
             "h2{margin-top:32px}.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}"
             f"img,video{{width:100%;aspect-ratio:{ar};object-fit:contain;background:#000;border-radius:6px}}"
             ".ref img{aspect-ratio:auto}p{margin:6px 0;color:#aaa}b{color:#fff}details{color:#888}</style>",
             f"<h1>{e(story['title'])}</h1>", "<h2>Reference sheets</h2><div class='g ref'>"]
    for key, entry in man.data["refs"].items():
        parts.append(f"<div><img src='{e(entry['jpg'])}'><p><b>{e(key)}</b> · attempt {entry['attempt']}</p>"
                     f"<details><summary>prompt</summary>{e(entry['prompt'])}</details></div>")
    parts.append("</div>")
    for s in story["shots"]:
        kf = man.get("keyframes", s["id"])
        last = man.get("keyframes", f"{s['id']}:last")
        who = story["characters"].get(s.get("speaker", ""), {})
        line = f"<p><b>{e(who.get('name'))}{' (V.O.)' if who.get('vo') else ''}</b>: {e(s.get('line'))}</p>" if s.get("line") else ""
        parts.append(f"<h2>{e(s['id'])} · {e(s.get('scene'))} · {e(s['shot'])} · {s.get('duration', 6)} s</h2>{line}<div class='g'>")
        if kf:
            parts.append(f"<div><img src='{e(kf['jpg'])}'><p>first frame · attempt {kf['attempt']}</p>"
                         f"<details><summary>prompt</summary>{e(kf['prompt'])}</details></div>")
        if last:
            parts.append(f"<div><img src='{e(last['jpg'])}'><p>last frame · attempt {last['attempt']}</p></div>")
        for p in PASSES:
            c = man.get("clips", s["id"], p)
            if c:
                parts.append(f"<div><video src='{e(c['file'])}' controls preload=metadata></video>"
                             f"<p>{p} clip · attempt {c['attempt']}</p>"
                             f"<details><summary>prompt</summary>{e(c['prompt'])}</details></div>")
        parts.append("</div>")
    for p, f in man.data["films"].items():
        parts.append(f"<h2>Film · {p}</h2><video src='{e(f['file'])}' controls style='max-width:960px'></video>")
    out = OUT / "review.html"
    out.write_text("".join(parts), encoding="utf-8")
    return out


# ---------------------------------------------------------------- main
COMMANDS = ["plan", "requests", "credits", "refs", "keyframes", "voices", "clips", "assemble", "review", "all"]


def main(argv=None) -> int:
    global OUT
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("--story", type=Path, default=Path(os.environ.get("STORY", DEFAULT_STORY)))
    ap.add_argument("--pass", dest="pass_", choices=list(PASSES), default="draft")
    ap.add_argument("--shots", default="", help="comma-separated shot ids (default: all)")
    ap.add_argument("--redo", nargs="?", const="*", default="",
                    help="draw again with a new seed: the selected shots, or for refs keys like character:clerk")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("BUDGET_USD", "8")))
    ap.add_argument("--workers", type=int, default=4, help="requests at a time (the free tier allows 5)")
    ap.add_argument("--music", type=Path, help="an audio file to lay under the film at low volume")
    ap.add_argument("--dry-run", action="store_true", help="placeholder media, no API key, no cost")
    ap.add_argument("--frames-base", default=os.environ.get("FRAMES_BASE_URL", ""),
                    help="read frames for clips from where assets/<story>/ is published, e.g. a raw GitHub URL "
                         "(the image API's own URLs expire after about an hour)")
    a = ap.parse_args(argv)

    story = json.loads(a.story.read_text(encoding="utf-8"))
    if a.command == "plan":
        return plan(story, a.budget)
    OUT = ROOT / ("out-dryrun" if a.dry_run else "out") / slug(story["title"])
    OUT.mkdir(parents=True, exist_ok=True)
    man = Manifest(OUT / "manifest.json")
    if a.command == "requests":
        print(f"wrote {requests_preview(story, man, a.pass_)}")
        return 0
    if a.command == "review":
        print(f"open {review_page(story, man)}")
        return 0
    if a.dry_run:
        provider = DryRun(OUT / "_dry")
    else:
        key = os.environ.get("NUNCHUX_API_KEY")
        if not key:
            print("Set NUNCHUX_API_KEY in .env (or the environment), or use --dry-run.", file=sys.stderr)
            return 2
        provider = Nunchux(key)
    if a.command == "credits":
        print(json.dumps(provider.credits(), indent=2))
        return 0
    problems = check_story(story)
    if problems:
        print("fix the story first:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    ledger = Ledger(OUT / "receipts.jsonl", a.budget)
    film = Film(story, provider, ledger, man, a.workers)
    film.frames_base = a.frames_base.rstrip("/")
    ids = [s for s in a.shots.split(",") if s] or [s["id"] for s in story["shots"]]
    unknown = [s for s in ids if s not in film.shots]
    if unknown:
        print(f"unknown shot ids: {', '.join(unknown)}", file=sys.stderr)
        return 2
    redo_shots = bool(a.redo)
    redo_refs = {k for k in a.redo.split(",") if ":" in k}
    if a.redo == "*" and a.command == "refs":
        redo_refs = {it[0] for it in ref_items(story)}
    font("text"), font("title")   # copy the fonts once, before any threads start
    log(f"{story['title']} · spent so far (estimated): ${ledger.spent:.2f} of ${a.budget:.2f}"
        + ("  [dry run]" if a.dry_run else ""))

    failed: list[str] = []
    t0 = time.time()
    before = provider.credits()

    def note_balance() -> None:
        """The account balance before and after each command: what the receipts estimate, the account measures."""
        after = provider.credits()
        man.data.setdefault("balance", []).append({"cmd": a.command, "pass": a.pass_, "ts": now_iso(),
                                                    "before": before.get("credits_remaining"),
                                                    "after": after.get("credits_remaining")})
        man.save()
        if isinstance(before.get("credits_remaining"), (int, float)) and isinstance(after.get("credits_remaining"), (int, float)):
            log(f"balance: ${before['credits_remaining']:.4f} → ${after['credits_remaining']:.4f} "
                f"(measured ${before['credits_remaining'] - after['credits_remaining']:.4f})")
    try:
        if a.command == "refs":
            failed = film.refs(redo_refs)
        elif a.command == "keyframes":
            failed = film.keyframes(ids, redo_shots)
        elif a.command == "voices":
            vids = [i for i in film.voice_shots() if not a.shots or i in ids]
            log(f"voices: {', '.join(vids)} ({a.pass_} clips; listen in review.html)")
            failed = film.keyframes(vids, False) or film.clips(a.pass_, vids, redo_shots)
            log(f"open {review_page(story, man)}")
        elif a.command == "clips":
            failed = film.clips(a.pass_, ids, redo_shots)
        elif a.command == "assemble":
            film.assemble(a.pass_, a.music)
        elif a.command == "all":
            run_row = {"pass": a.pass_, "started": now_iso(), "credits_before": provider.credits()}
            def ok_clips() -> int:
                return sum(1 for r in ledger.rows() if r["step"] == "clip" and r.get("pass") == a.pass_
                           and r.get("status") == "ok")
            clips_before = ok_clips()   # not "before": that name holds the balance read at the start (bug, 23 Nov)
            failed = film.refs(set()) or film.keyframes(ids, False) or film.clips(a.pass_, ids, redo_shots)
            run_row["render_seconds"] = round(time.time() - t0, 1)
            run_row["clips_rendered"] = ok_clips() - clips_before
            film.m.data["runs"].append(run_row)
            film.m.save()
            if failed:
                log(f"not assembling: {', '.join(failed)} failed (fix and run the same command again; "
                    f"finished items are kept)")
            else:
                film.assemble(a.pass_, a.music)
                run_row["credits_after"] = provider.credits()
                run_row["total_seconds"] = round(time.time() - t0, 1)
                film.m.save()
            review_page(story, man)
    except (BudgetExceeded, RuntimeError, ApiError) as e:
        log(f"stopped: {e}")
        note_balance()
        return 1
    note_balance()
    log(f"done in {time.time() - t0:.0f} s · spent so far (estimated): ${ledger.spent:.2f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
