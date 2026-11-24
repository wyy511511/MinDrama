#!/usr/bin/env python3
"""A micro-drama with consistent characters, rendered end to end on Nunchux Modelverse.

How the characters stay the same from shot to shot:
  1. refs       FLUX 3 Image Klein draws one reference sheet per character, prop and location.
  2. keyframes  FLUX 3 Klein Edit draws every shot's first frame from those sheets (up to 10 references).
  3. clips      MiniMax H3 Fast animates each first frame into a clip with its dialogue and sound.
  4. assemble   ffmpeg joins the clips with subtitles, a title card and an end card with the receipts.

    python3 make_film.py plan                          # shot list and cost estimate; no API calls
    python3 make_film.py all --pass draft              # 480p on the Radical Value tier
    python3 make_film.py clips --pass draft --shots s03,s09 --redo   # re-roll two shots with new seeds
    python3 make_film.py all --pass final              # 768p on the Radical Speed tier
    python3 make_film.py all --pass draft --dry-run    # placeholder media, no key, no cost (out-dryrun/)

Every API call is appended to out/receipts.jsonl with its latency and estimated cost, and the run stops
before a call would take the estimated spend past --budget (default $8, or BUDGET_USD in .env).
Standard library only; needs ffmpeg and ffprobe on PATH.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
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
API = "https://api.nunchux.com"
UA = "microdrama/1.0"
IMAGE_MODEL = "nunchux-flux.3-image-klein"
EDIT_MODEL = "nunchux-flux.3-image-klein-edit"
VIDEO_MODEL = "nunchux-minimax-h3-fast-fl2va"
PASSES = {
    "draft": {"tier": "radical_value", "short_edge": 480},
    "final": {"tier": "radical_speed", "short_edge": 768},
}
# Estimates from nunchux.ai/pricing, read 2026-11-23: $ per megapixel (images) and $ per output second (video).
# Text-to-image Klein is assumed to cost what Klein Edit costs. Check the live page; receipts call these estimates.
IMAGE_PER_MP = {"radical_speed": 0.006, "radical_value": 0.004}
VIDEO_PER_S = {("radical_speed", 480): 0.022, ("radical_speed", 768): 0.030,
               ("radical_value", 480): 0.018, ("radical_value", 768): 0.024}
FONTS = {
    "text": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "title": ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/Library/Fonts/Arial Black.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/ariblk.ttf"],
}

OUT = ROOT / "out"            # set in main(); out-dryrun/ for --dry-run
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


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 120,
              attempts: int = 5) -> dict:
    """JSON over HTTPS. Retries 429 and 5xx with backoff (honouring Retry-After), as the API's docs ask.
    A POST that times out is not retried: a synchronous generation may still have run and been billed."""
    data = json.dumps(body).encode() if body is not None else None
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
                log(f"    {e.code} from {urllib.parse.urlparse(url).path}; retrying in {wait:.0f} s ({text[:160]})")
                time.sleep(wait)
                continue
            raise ApiError(f"{method} {urllib.parse.urlparse(url).path} answered {e.code}: {text}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if method == "GET" and i < attempts - 1:
                time.sleep(2 ** i)
                continue
            raise ApiError(f"{method} {urllib.parse.urlparse(url).path} failed: {e}") from None
    raise ApiError(f"{method} {url}: out of retries")


def output_of(r: dict) -> dict:
    d = (r.get("data") or [{}])[0]
    if d.get("url"):
        return {"url": d["url"]}
    if d.get("b64_json"):
        return {"b64": d["b64_json"]}
    raise ApiError(f"no url or b64_json in the answer: {json.dumps(r)[:400]}")


def trimmed(r: dict) -> dict:
    """The answer without base64 payloads, for the receipts."""
    s = json.dumps(r)
    return json.loads(re.sub(r'"b64_json":\s*"[^"]{200,}"', '"b64_json": "<omitted>"', s))


# ---------------------------------------------------------------- providers
class Nunchux:
    """Nunchux Modelverse (docs.nunchux.ai). Images and the optimized video models answer synchronously."""
    dry = False

    def __init__(self, key: str):
        self.h = {"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA}

    def image(self, prompt: str, w: int, h: int, tier: str, seed: int) -> dict:
        body = {"model": IMAGE_MODEL, "prompt": prompt, "width": w, "height": h, "tier": tier, "seed": seed,
                "response_format": "url"}
        r = http_json("POST", f"{API}/v1/images/generations", self.h, body, timeout=240)
        return {"output": output_of(r), "raw": trimmed(r)}

    def edit(self, prompt: str, images: list[str], w: int, h: int, tier: str, seed: int) -> dict:
        body = {"model": EDIT_MODEL, "prompt": prompt, "image": images[:10], "width": w, "height": h, "tier": tier,
                "seed": seed, "response_format": "url"}
        r = http_json("POST", f"{API}/v1/images/edits", self.h, body, timeout=240)
        return {"output": output_of(r), "raw": trimmed(r)}

    def video(self, prompt: str, first_frame_url: str, duration: float, short_edge: int, aspect: str, tier: str,
              seed: int) -> dict:
        body = {"model": VIDEO_MODEL, "prompt": prompt, "tier": tier, "duration": duration, "seed": seed,
                "refs": [{"role": "first_frame", "url": first_frame_url}],
                "extra": {"short_edge": short_edge, "aspect_ratio": aspect}, "response_format": "url"}
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
                    if " 404" in str(e) or "answered 404" in str(e):
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
        text_card(out, w, h, None, [(f"DRY RUN · {label}", "text", 30, "h*0.42"),
                                    ("\n".join(textwrap.wrap(prompt, 34)[:6]), "text", 22, "h*0.48")],
                  bg=self._color(prompt))
        return {"output": {"url": f"dryrun://{out}"}, "raw": {"dry_run": True}}

    def image(self, prompt, w, h, tier, seed):
        return self._still(prompt, w, h, "text to image")

    def edit(self, prompt, images, w, h, tier, seed):
        return self._still(prompt, w, h, f"edit, {len(images)} refs")

    def video(self, prompt, first_frame_url, duration, short_edge, aspect, tier, seed):
        time.sleep(random.uniform(0.4, 1.0))
        w, h = frame_size(short_edge, aspect)
        out = self._next(".mp4")
        src = first_frame_url[len("dryrun://"):] if first_frame_url.startswith("dryrun://") else None
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
    """A copy of a system font under out/_fonts/, so filter arguments never hold spaces."""
    dest = OUT / "_fonts" / f"{kind}.ttf"
    if dest.exists():
        return str(dest)
    for cand in FONTS[kind]:
        if Path(cand).exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(cand, dest)
            return str(dest)
    return None


def drawtext(textfile: Path, kind: str, size: int, y: str, color: str = "white", box: bool = False) -> str:
    f = font(kind)
    parts = [f"textfile={textfile}", "expansion=none", f"fontsize={size}", f"fontcolor={color}",
             "x=(w-text_w)/2", f"y={y}", "line_spacing=10", "text_align=C"]   # text_align: ffmpeg 6.1+
    if f:
        parts.append(f"fontfile={f}")
    if box:
        parts += ["box=1", "boxcolor=black@0.5", "boxborderw=16"]
    return "drawtext=" + ":".join(parts)


def text_file(text: str) -> Path:
    tmp = OUT / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"t-{hashlib.md5(text.encode()).hexdigest()[:12]}.txt"
    p.write_text(text, encoding="utf-8")
    return p


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


ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "24",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def to_jpeg(src: Path) -> Path:
    dst = src.with_suffix(".jpg") if src.suffix.lower() != ".jpg" else src
    if dst != src:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-q:v", "3", str(dst)])
    return dst


def data_uri(jpg: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpg.read_bytes()).decode()


EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}


def fetch(output: dict, dest_stem: Path) -> Path:
    """Save an API output next to the manifest. URLs can expire within the hour, so this runs right away."""
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
    """out/receipts.jsonl: one row per API call. The budget counts calls that succeeded plus calls in flight."""

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
    """out/manifest.json: what exists now (paths relative to out/), with the URL and seed behind each file."""

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


# ---------------------------------------------------------------- prompts
def seed_for(story: dict, key: str, attempt: int) -> int:
    return int(hashlib.sha1(f"{story['seed']}:{key}".encode()).hexdigest()[:8], 16) % 1_000_000 + attempt


def ref_items(story: dict) -> list[tuple[str, str, str, list[int]]]:
    """(manifest key, kind, prompt, size) for every reference sheet."""
    items = []
    for cid, c in story["characters"].items():
        items.append((f"character:{cid}", "character",
                      f"Character reference photo of {c['name']}: {c['look']}, wearing {c['outfit']}. Full body, "
                      f"standing, facing the camera, relaxed neutral expression, plain light-grey studio backdrop, "
                      f"soft even light, sharp focus on the face. {story['style']} {story['avoid']}",
                      story["sheet_size"]))
    for pid, p in story["props"].items():
        items.append((f"prop:{pid}", "prop",
                      f"Product reference photo of {p['look']}, open, on a plain light-grey backdrop, soft even light. "
                      f"{story['avoid']}", [1024, 1024]))
    for lid, loc in story["locations"].items():
        items.append((f"location:{lid}", "location",
                      f"Establishing shot with no people: {loc['look']}. Vertical {story['aspect_ratio']} frame. "
                      f"{story['style']} {story['avoid']}", story["keyframe_size"]))
    return items


def shot_refs(shot: dict) -> list[str]:
    """The manifest keys of a shot's reference images, in the order its prompt names them (Image 1, 2…)."""
    keys = [f"character:{c}" for c in shot.get("characters", [])] + [f"prop:{p}" for p in shot.get("props", [])]
    keys.append(f"location:{shot['location']}")
    return keys[:10]


def keyframe_prompt(story: dict, shot: dict) -> str:
    parts = [f"{story['style']} Vertical {story['aspect_ratio']} film still, {shot['shot']}.",
             shot["frame"].rstrip(".") + "."]
    for i, key in enumerate(shot_refs(shot), 1):
        kind, name = key.split(":")
        if kind == "character":
            c = story["characters"][name]
            outfit = shot.get("wardrobe", {}).get(name, c["outfit"])
            parts.append(f"{c['name']} is the person in Image {i}: the same face, hair, skin tone and build, "
                         f"wearing {outfit}.")
        elif kind == "prop":
            parts.append(f"{story['props'][name]['name']} looks exactly like the one in Image {i}.")
        else:
            parts.append(f"The setting is the place in Image {i}.")
    parts.append(story["avoid"])
    return " ".join(parts)


def chain_prompt(story: dict, shot: dict) -> str:
    keys = shot_refs(shot)
    names = ", ".join(f"Image {i + 2} is {story['characters'][k.split(':')[1]]['name']}"
                      for i, k in enumerate(keys) if k.startswith("character:"))
    return (f"Image 1 is the last frame of the previous shot. Redraw it exactly, same composition, framing and "
            f"lighting, with every face, hairstyle and outfit matched to the reference images ({names}). "
            f"{story['avoid']}")


def clip_prompt(story: dict, shot: dict) -> str:
    parts = ["Image 1 is the first frame.", shot["action"].rstrip(".") + ".", f"Camera: {shot['camera']}."]
    if shot.get("line"):
        sp = story["characters"][shot["speaker"]]
        parts.append(f'{sp["name"]}, in {sp["voice"]}, says: "{shot["line"]}" Only {sp["name"]} speaks.')
    else:
        parts.append("No one speaks.")
    if shot.get("sound"):
        parts.append(f"Sound: {shot['sound']}.")
    parts.append(f"{story['clip_audio']} Keep every face, hairstyle and outfit exactly as in Image 1. "
                 f"Natural, subtle motion. No on-screen text.")
    return " ".join(parts)


# ---------------------------------------------------------------- the pipeline
class Film:
    def __init__(self, story: dict, provider, ledger: Ledger, manifest: Manifest, workers: int):
        self.story, self.p, self.ledger, self.m, self.workers = story, provider, ledger, manifest, workers
        self.shots = {s["id"]: s for s in story["shots"]}

    # one API call, with its receipt
    def call(self, step: str, item: str, model: str, tier: str, est: float, fn, params: dict, pass_=None) -> dict:
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

    def image_cost(self, w: int, h: int, tier: str) -> float:
        return w * h / 1e6 * IMAGE_PER_MP[tier]

    # 1. reference sheets
    def refs(self, redo: set[str]) -> list[str]:
        tier = self.story["image_tier"]
        todo = [it for it in ref_items(self.story) if it[0] in redo or not self.m.get("refs", it[0])]
        if not todo:
            log("refs: all present")
            return []
        log(f"refs: {len(todo)} to draw")

        def one(it):
            key, kind, prompt, (w, h) = it
            prev = self.m.get("refs", key)
            attempt = (prev["attempt"] + 1) if prev else 0
            seed = seed_for(self.story, key, attempt)
            res = self.call("ref", key, IMAGE_MODEL, tier, self.image_cost(w, h, tier),
                            lambda: self.p.image(prompt, w, h, tier, seed), {"width": w, "height": h, "seed": seed})
            path = fetch(res["output"], OUT / "refs" / key.replace(":", "-"))
            jpg = to_jpeg(path)
            self.m.put("refs", key, {"file": str(path.relative_to(OUT)), "jpg": str(jpg.relative_to(OUT)),
                                     "url": res["output"].get("url"), "seed": seed, "attempt": attempt,
                                     "prompt": prompt, "made": now_iso()})
            log(f"  ✓ {key}")

        by_key = {it[0]: it for it in todo}
        return self.parallel(lambda k: one(by_key[k]), list(by_key))

    # 2. first frames
    def keyframes(self, ids: list[str], redo: bool) -> list[str]:
        tier = self.story["image_tier"]
        w, h = self.story["keyframe_size"]
        todo = [i for i in ids if self.shots[i].get("first_frame") != "chain" and (redo or not self.m.get("keyframes", i))]
        if not todo:
            log("keyframes: all present")
            return []
        missing = [k for i in todo for k in shot_refs(self.shots[i]) if not self.m.get("refs", k)]
        if missing:
            raise RuntimeError(f"reference sheets missing: {', '.join(sorted(set(missing)))} (run: refs)")
        log(f"keyframes: {len(todo)} to draw")

        def one(sid):
            shot = self.shots[sid]
            prompt = keyframe_prompt(self.story, shot)
            images = [data_uri(OUT / self.m.get("refs", k)["jpg"]) for k in shot_refs(shot)]
            prev = self.m.get("keyframes", sid)
            attempt = (prev["attempt"] + 1) if prev else 0
            seed = seed_for(self.story, f"keyframe:{sid}", attempt)
            res = self.call("keyframe", sid, EDIT_MODEL, tier, self.image_cost(w, h, tier),
                            lambda: self.p.edit(prompt, images, w, h, tier, seed),
                            {"width": w, "height": h, "seed": seed, "refs": shot_refs(shot)})
            path = fetch(res["output"], OUT / "keyframes" / sid)
            jpg = to_jpeg(path)
            self.m.put("keyframes", sid, {"file": str(path.relative_to(OUT)), "jpg": str(jpg.relative_to(OUT)),
                                          "url": res["output"].get("url"), "seed": seed, "attempt": attempt,
                                          "prompt": prompt, "refs": shot_refs(shot), "made": now_iso()})
            log(f"  ✓ {sid}")

        return self.parallel(one, todo)

    # 3. clips
    def clips(self, pass_: str, ids: list[str], redo: bool) -> list[str]:
        cfg = PASSES[pass_]
        todo = [i for i in ids if redo or not self.m.get("clips", i, pass_)]
        if not todo:
            log(f"clips ({pass_}): all present")
            return []
        free = [i for i in todo if self.shots[i].get("first_frame") != "chain"]
        chained = [i for i in todo if self.shots[i].get("first_frame") == "chain"]
        log(f"clips ({pass_}, {cfg['tier']}, {cfg['short_edge']}p): {len(todo)} to render")
        failed = self.parallel(lambda sid: self.clip(pass_, sid, self.m.get("keyframes", sid)), free)
        for sid in chained:   # in story order: each one starts from the last frame of the shot before it
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
        images = [data_uri(last)] + [data_uri(OUT / self.m.get("refs", k)["jpg"]) for k in shot_refs(shot)
                                     if k.startswith("character:")]
        seed = seed_for(self.story, f"chain:{sid}", 0)
        res = self.call("chain", sid, EDIT_MODEL, tier, self.image_cost(w, h, tier),
                        lambda: self.p.edit(chain_prompt(self.story, shot), images, w, h, tier, seed),
                        {"width": w, "height": h, "seed": seed, "from": prev_id}, pass_=pass_)
        path = fetch(res["output"], OUT / "keyframes" / f"{sid}-chain-{pass_}")
        jpg = to_jpeg(path)
        entry = {"file": str(path.relative_to(OUT)), "jpg": str(jpg.relative_to(OUT)), "url": res["output"].get("url"),
                 "seed": seed, "attempt": 0, "prompt": chain_prompt(self.story, shot), "chained_from": prev_id,
                 "made": now_iso()}
        self.m.put("keyframes", f"{sid}@{pass_}", entry)
        return entry

    def clip(self, pass_: str, sid: str, first: dict | None) -> None:
        if not first or not first.get("url"):
            raise RuntimeError(f"{sid} has no first frame with a URL (run: keyframes)")
        cfg = PASSES[pass_]
        shot = self.shots[sid]
        prompt = clip_prompt(self.story, shot)
        dur = float(shot.get("duration", 6))
        prev = self.m.get("clips", sid, pass_)
        attempt = (prev["attempt"] + 1) if prev else 0
        seed = seed_for(self.story, f"clip:{sid}", attempt)
        est = VIDEO_PER_S[(cfg["tier"], cfg["short_edge"])] * dur
        res = self.call("clip", sid, VIDEO_MODEL, cfg["tier"], est,
                        lambda: self.p.video(prompt, first["url"], dur, cfg["short_edge"], self.story["aspect_ratio"],
                                             cfg["tier"], seed),
                        {"duration": dur, "short_edge": cfg["short_edge"], "seed": seed}, pass_=pass_)
        path = fetch(res["output"], OUT / "clips" / pass_ / sid)
        self.m.put("clips", sid, {"file": str(path.relative_to(OUT)), "url": res["output"].get("url"), "seed": seed,
                                  "attempt": attempt, "prompt": prompt, "made": now_iso()}, pass_)
        log(f"  ✓ {sid}  ({pass_})")

    # 4. the film
    def assemble(self, pass_: str, music: Path | None = None) -> Path:
        W, H = self.story["output_size"]
        missing = [s["id"] for s in self.story["shots"] if not self.m.get("clips", s["id"], pass_)]
        if missing:
            raise RuntimeError(f"no {pass_} clip yet for {', '.join(missing)}")
        segdir = OUT / "film" / f"_{pass_}"
        segdir.mkdir(parents=True, exist_ok=True)
        title = self.story["title"]
        segs = [text_card(segdir / "00-title.mp4", W, H, 2.4, [
            (title.upper(), "title", 88, "h*0.43"),
            (f"a {round(self.film_seconds())}-second micro-drama", "text", 30, "h*0.43+120")])]
        srt, t = [], 2.4
        for n, shot in enumerate(self.story["shots"], 1):
            src = OUT / self.m.get("clips", shot["id"], pass_)["file"]
            dst = segdir / f"{n:02d}-{shot['id']}.mp4"
            self.normalize(src, dst, W, H, shot.get("line"))
            d = duration_of(dst)
            if shot.get("line"):
                srt.append((t, t + d, shot["line"]))
            segs.append(dst)
            t += d
        stats = self.stats(pass_)
        segs.append(text_card(segdir / "99-end.mp4", W, H, 3.6, [
            (title.upper(), "title", 64, "h*0.36"),
            (stats["line1"], "text", 30, "h*0.36+110"),
            (stats["line2"], "text", 30, "h*0.36+160"),
            ("FLUX 3 Klein Edit + MiniMax H3 Fast", "text", 26, "h*0.36+250"),
            ("on Nunchux Modelverse", "text", 26, "h*0.36+292")]))
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
        (film.with_suffix(".srt")).write_text("".join(
            f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{line}\n\n" for i, (a, b, line) in enumerate(srt, 1)),
            encoding="utf-8")
        self.m.data["films"][pass_] = {"file": str(film.relative_to(OUT)), "seconds": round(duration_of(film), 2),
                                       "built": now_iso(), **{k: v for k, v in stats.items() if not k.startswith("line")}}
        self.m.save()
        log(f"film: {film}  ({duration_of(film):.1f} s)")
        return film

    def normalize(self, src: Path, dst: Path, W: int, H: int, line: str | None) -> None:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
              f"setsar=1,fps=24")
        if line:
            vf += "," + drawtext(text_file("\n".join(textwrap.wrap(line, 30))), "text", 38, "h-text_h-220", box=True)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
        if has_audio(src):
            maps = ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            maps = ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        run(cmd + ["-vf", vf + ",format=yuv420p", *maps, *ENCODE, str(dst)])

    def film_seconds(self) -> float:
        return sum(float(s.get("duration", 6)) for s in self.story["shots"])

    def stats(self, pass_: str) -> dict:
        rows = [r for r in self.ledger.rows() if r.get("status") == "ok"]
        all_in = sum(r["est_cost"] for r in rows)
        this_cut = sum(r["est_cost"] for r in rows if r["step"] in ("ref", "keyframe")
                       or (r["step"] in ("clip", "chain") and r.get("pass") == pass_))
        # the render time of the last full run of this pass (one that rendered most of the shots), not of a re-roll
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
                "line1": f"{dry}{len(self.story['shots'])} shots · {round(self.film_seconds())} s",
                "line2": f"${all_in:.2f} all-in · {when}"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


# ---------------------------------------------------------------- commands
def plan(story: dict, budget: float) -> None:
    w, h = story["keyframe_size"]
    tier = story["image_tier"]
    refs = ref_items(story)
    ref_cost = sum(sw * sh / 1e6 * IMAGE_PER_MP[tier] for _, _, _, (sw, sh) in refs)
    kf_cost = len(story["shots"]) * w * h / 1e6 * IMAGE_PER_MP[tier]
    secs = sum(float(s.get("duration", 6)) for s in story["shots"])
    print(f"{story['title']}: {len(story['shots'])} shots, {secs:.0f} s, {story['aspect_ratio']}\n")
    for s in story["shots"]:
        line = f'{story["characters"][s["speaker"]]["name"]}: "{s["line"]}"' if s.get("line") else "(no dialogue)"
        print(f"  {s['id']}  {s['location']:<9} {','.join(s.get('characters', [])):<10} {s['shot']:<22} {line}")
    print(f"\nestimated cost (nunchux.ai/pricing, 2026-11-23):")
    print(f"  {len(refs)} reference sheets        ${ref_cost:.2f}")
    print(f"  {len(story['shots'])} keyframes             ${kf_cost:.2f}")
    for name, cfg in PASSES.items():
        c = VIDEO_PER_S[(cfg["tier"], cfg["short_edge"])] * secs
        print(f"  clips, {name:<5} ({cfg['short_edge']}p, {cfg['tier']})  ${c:.2f}")
    draft = VIDEO_PER_S[(PASSES['draft']['tier'], 480)] * secs
    final = VIDEO_PER_S[(PASSES['final']['tier'], 768)] * secs
    print(f"\n  one draft + one final, before re-rolls: ${ref_cost + kf_cost + draft + final:.2f}   (budget ${budget:.2f})")


def main(argv=None) -> int:
    global OUT
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "refs", "keyframes", "clips", "assemble", "all", "credits"])
    ap.add_argument("--pass", dest="pass_", choices=list(PASSES), default="draft")
    ap.add_argument("--shots", default="", help="comma-separated shot ids (default: all)")
    ap.add_argument("--redo", nargs="?", const="*", default="",
                    help="draw again with a new seed: the selected shots, or for refs a list like character:mei")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("BUDGET_USD", "8")))
    ap.add_argument("--workers", type=int, default=4, help="requests at a time (the free tier allows 5)")
    ap.add_argument("--music", type=Path, help="an audio file to lay under the film at low volume")
    ap.add_argument("--story", type=Path, default=ROOT / "story.json")
    ap.add_argument("--dry-run", action="store_true", help="placeholder media, no API key, no cost")
    a = ap.parse_args(argv)

    story = json.loads(a.story.read_text(encoding="utf-8"))
    if a.command == "plan":
        plan(story, a.budget)
        return 0
    OUT = ROOT / ("out-dryrun" if a.dry_run else "out")
    OUT.mkdir(parents=True, exist_ok=True)
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
    ledger = Ledger(OUT / "receipts.jsonl", a.budget)
    film = Film(story, provider, ledger, Manifest(OUT / "manifest.json"), a.workers)
    ids = [s for s in a.shots.split(",") if s] or [s["id"] for s in story["shots"]]
    unknown = [s for s in ids if s not in film.shots]
    if unknown:
        print(f"unknown shot ids: {', '.join(unknown)}", file=sys.stderr)
        return 2
    # --redo alone: everything selected; --redo character:mei,location:cafe: those reference sheets
    redo_shots = bool(a.redo)
    redo_refs = {k for k in a.redo.split(",") if ":" in k}
    if a.redo == "*" and a.command == "refs":
        redo_refs = {it[0] for it in ref_items(story)}
    font("text"), font("title")   # copy the fonts once, before any threads start
    log(f"spent so far (estimated): ${ledger.spent:.2f} of ${a.budget:.2f}" + ("  [dry run]" if a.dry_run else ""))

    failed: list[str] = []
    t0 = time.time()
    try:
        if a.command == "refs":
            failed = film.refs(redo_refs)
        elif a.command == "keyframes":
            failed = film.keyframes(ids, redo_shots)
        elif a.command == "clips":
            failed = film.clips(a.pass_, ids, redo_shots)
        elif a.command == "assemble":
            film.assemble(a.pass_, a.music)
        elif a.command == "all":
            run_row = {"pass": a.pass_, "started": now_iso(), "credits_before": provider.credits()}
            ok_clips = lambda: sum(1 for r in ledger.rows() if r["step"] == "clip" and r.get("pass") == a.pass_  # noqa: E731
                                   and r.get("status") == "ok")
            before = ok_clips()
            failed = film.refs(set()) or film.keyframes(ids, False) or film.clips(a.pass_, ids, redo_shots)
            run_row["render_seconds"] = round(time.time() - t0, 1)
            run_row["clips_rendered"] = ok_clips() - before
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
    except (BudgetExceeded, RuntimeError, ApiError) as e:
        log(f"stopped: {e}")
        return 1
    log(f"done in {time.time() - t0:.0f} s · spent so far (estimated): ${ledger.spent:.2f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
