#!/usr/bin/env python3
"""The making-of page: the film, every shot with its prompts, and the receipt of every API call.

    python3 build_page.py                         # the default story, final cut if there is one, else the draft
    python3 build_page.py --story stories/rain-check.json --pass draft
    python3 build_page.py --dry-run               # from out-dryrun/<story>/ into site-dryrun/

site/ is a static folder: drop it on Netlify Drop, `npx vercel deploy site --prod`, or GitHub Pages.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_STORY = ROOT / "stories" / "rental-booking-paradox.json"
PRICES_READ = "2026-11-23"


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr.strip()[-600:]}")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def money(x: float) -> str:
    return f"${x:.2f}" if x >= 0.1 else f"${x:.3f}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--story", type=Path, default=Path(os.environ.get("STORY", DEFAULT_STORY)))
    ap.add_argument("--pass", dest="pass_", choices=["draft", "final"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    story = json.loads(a.story.read_text(encoding="utf-8"))
    out = ROOT / ("out-dryrun" if a.dry_run else "out") / slug(story["title"])
    site = ROOT / ("site-dryrun" if a.dry_run else "site")
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (out / "receipts.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    films = man.get("films", {})
    pass_ = a.pass_ or ("final" if "final" in films else "draft")
    if pass_ not in films:
        raise SystemExit(f"no {pass_} film yet: run  python3 make_film.py all --pass {pass_}")
    film = films[pass_]
    W, H = story["output_size"]
    landscape = W > H
    thumb_w = 480 if landscape else 432

    media = site / "media"
    if media.exists():
        shutil.rmtree(media)
    for sub in ("shots", "clips", "refs"):
        (media / sub).mkdir(parents=True, exist_ok=True)

    shutil.copy(out / film["file"], media / "film.mp4")
    first_kf = man["keyframes"][story["shots"][0]["id"]]
    run(["ffmpeg", "-y", "-v", "error", "-i", str(out / first_kf["jpg"]), "-vf", f"scale={W}:-2", "-q:v", "3",
         str(media / "poster.jpg")])
    srt = (out / film["file"]).with_suffix(".srt")
    if srt.exists():
        shutil.copy(srt, media / "film.srt")

    # reference sheets
    refs_html = []
    for key, entry in man["refs"].items():
        kind, name = key.split(":")
        label = (story["characters"][name]["name"] if kind == "character" else
                 story["props"][name]["name"] if kind == "prop" else story["locations"][name].get("name", name))
        dest = media / "refs" / f"{kind}-{name}.jpg"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(out / entry["jpg"]), "-vf", "scale=640:-2", "-q:v", "4", str(dest)])
        refs_html.append(f'<figure class="ref"><img src="media/refs/{dest.name}" alt="{esc(label)} reference" '
                         f'loading="lazy"><figcaption><b>{esc(label)}</b><span>{esc(kind)}</span></figcaption></figure>')

    ok = [r for r in rows if r.get("status") == "ok"]

    def receipt(step, item, p=None):
        hits = [r for r in ok if r["step"] == step and r["item"] == item and (p is None or r.get("pass") == p)]
        return hits[-1] if hits else None

    cards = []
    for n, shot in enumerate(story["shots"], 1):
        sid = shot["id"]
        kf = man["keyframes"].get(f"{sid}@{pass_}") or man["keyframes"].get(sid)
        last = man["keyframes"].get(f"{sid}:last")
        clip = man["clips"].get(pass_, {}).get(sid)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(out / kf["jpg"]), "-vf", f"scale={thumb_w}:-2", "-q:v", "4",
             str(media / "shots" / f"{sid}.jpg")])
        run(["ffmpeg", "-y", "-v", "error", "-i", str(out / clip["file"]), "-vf", f"scale={thumb_w}:-2", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             str(media / "clips" / f"{sid}.mp4")])
        rk = receipt("chain", sid, pass_) or receipt("keyframe", sid)
        rl = receipt("lastframe", sid)
        rc = receipt("clip", sid, pass_)
        who = ", ".join(story["characters"][c]["name"] for c in shot.get("characters", [])
                        if not story["characters"][c].get("vo")) or "no one on screen"
        if shot.get("line"):
            sp = story["characters"][shot["speaker"]]
            line = (f'<p class="line"><span>{esc(sp["name"])}{" (V.O.)" if sp.get("vo") else ""}</span> '
                    f'“{esc(shot["line"])}”</p>')
        else:
            line = '<p class="line quiet">No dialogue</p>'
        cost = lambda r: f'{r["seconds"]:.1f} s · {money(r["est_cost"])}' if r else "—"  # noqa: E731
        frame_cost = cost(rk) + (f' (+ last frame {cost(rl)})' if rl else "")
        prompts = f"<p><b>First frame</b> {esc(kf['prompt'])}</p>"
        if last:
            prompts += f"<p><b>Last frame</b> {esc(last['prompt'])}</p>"
        prompts += f"<p><b>Clip</b> {esc(clip['prompt'])}</p>"
        cards.append(f"""
      <article class="shot">
        <button class="frame" type="button" aria-label="Play shot {n}">
          <img src="media/shots/{sid}.jpg" alt="First frame of shot {n}" loading="lazy">
          <video src="media/clips/{sid}.mp4" muted loop playsinline preload="none"></video>
          <span class="num">{n:02d} · {esc(shot.get('scene', ''))}</span>
        </button>
        <div class="meta">
          <p class="kind">{esc(shot['shot'])} · {esc(shot['camera'])} · {shot.get('duration', 6)} s</p>
          <p class="who">{esc(who)}</p>
          {line}
          <dl>
            <div><dt>Frames</dt><dd>{frame_cost}</dd></div>
            <div><dt>Clip</dt><dd>{cost(rc)}</dd></div>
          </dl>
          <details><summary>Prompts</summary>{prompts}</details>
        </div>
      </article>""")

    all_in = sum(r["est_cost"] for r in ok)
    cut_rows = [r for r in ok if r["step"] in ("ref", "keyframe", "lastframe")
                or (r["step"] in ("clip", "chain") and r.get("pass") == pass_)]
    this_cut = sum(r["est_cost"] for r in cut_rows)
    clip_secs = [r["seconds"] for r in ok if r["step"] == "clip" and r.get("pass") == pass_]
    kf_secs = [r["seconds"] for r in ok if r["step"] == "keyframe"]
    render = film.get("render_seconds")
    tiers = sorted({r["tier"] for r in cut_rows if r["step"] == "clip"})
    cast = sum(1 for c in story["characters"].values() if not c.get("vo"))
    stats = [
        (f"{round(film['seconds'])} s", f"{len(story['shots'])} shots, a cast of {cast}"),
        (money(all_in), "all-in, drafts and re-rolls included"),
        (f"{render / 60:.1f} min" if render else "—", f"to render the {pass_} cut"),
        (f"{statistics.median(clip_secs):.0f} s" if clip_secs else "—", "median per clip"),
        (f"{statistics.median(kf_secs):.1f} s" if kf_secs else "—", "median per first frame"),
    ]
    stat_html = "".join(f'<div class="stat"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for v, k in stats)
    receipt_rows = "".join(
        f'<tr class="{"err" if r.get("status") != "ok" else ""}"><td>{esc(r["ts"][11:19])}</td><td>{esc(r["step"])}</td>'
        f'<td>{esc(r["item"])}</td><td>{esc(r.get("pass") or "")}</td><td>{esc(r["model"])}</td><td>{esc(r["tier"])}</td>'
        f'<td class="num">{r.get("seconds", 0):.1f}</td>'
        f'<td class="num">{money(r["est_cost"]) if r.get("status") == "ok" else esc(r.get("status"))}</td></tr>'
        for r in rows)

    words = story["title"].split()
    title_html = esc(" ".join(words[:-1])) + (" " if len(words) > 1 else "") + f"<span>{esc(words[-1])}</span>"
    credits = story.get("credits", {})
    byline = ("Made by " + credits["author"] + (f" for {credits['made_for']}" if credits.get("made_for") else "")
              if credits.get("author") else "")
    dry_note = ('<p class="dry">Dry run: placeholder media and estimated prices, no API calls were made.</p>'
                if a.dry_run else "")
    page = TEMPLATE.format(
        title=esc(story["title"]), title_html=title_html, logline=esc(story["logline"]), dry_note=dry_note,
        stats=stat_html, refs="".join(refs_html), cards="".join(cards), receipts=receipt_rows, calls=len(rows),
        failed=sum(1 for r in rows if r.get("status") != "ok"), all_in=money(all_in), this_cut=money(this_cut),
        pass_=esc(pass_), tiers=esc(", ".join(tiers)), prices_read=PRICES_READ, repo=esc(credits.get("repo", "#")),
        byline=esc(byline), has_srt="" if srt.exists() else "hidden", layout="landscape" if landscape else "portrait",
        ar=f"{W} / {H}", shot_min="280px" if landscape else "200px")
    (site / "index.html").write_text(page, encoding="utf-8")
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file()) / 1e6
    print(f"wrote {site / 'index.html'}  ({size:.1f} MB with media)")
    return 0


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}: making-of</title>
<meta name="description" content="{logline}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,500..900&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* A dark screening room: near-black ground, one warm accent for the story, teal for the numbers. */
:root {{
  --bg: #0a0b0f; --panel: #12141a; --line: #23262f; --ink: #eceae6; --dim: #9a9ca6; --faint: #6c6f7a;
  --accent: #f0b429; --teal: #3cc4bd; --ar: {ar};
  --display: "Archivo", "Inter", system-ui, sans-serif; --font: "Inter", system-ui, -apple-system, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, Menlo, monospace;
  color-scheme: dark;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.65 var(--font); -webkit-font-smoothing: antialiased;
  padding: 0 max(18px, env(safe-area-inset-left)) 64px max(18px, env(safe-area-inset-right)); }}
.wrap {{ max-width: 1160px; margin: 0 auto; }}
a {{ color: var(--ink); text-decoration-color: var(--accent); text-underline-offset: 3px; }}
:focus-visible {{ outline: 2px solid var(--teal); outline-offset: 3px; border-radius: 6px; }}
.eyebrow {{ font: 600 12px/1.4 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--teal); }}
h1 {{ font-family: var(--display); font-weight: 900; font-stretch: 72%; text-transform: uppercase; font-size: clamp(52px, 9vw, 120px);
  line-height: .88; letter-spacing: -.01em; margin: 14px 0 0; text-wrap: balance; }}
h1 span {{ color: var(--accent); }}
h2 {{ font-family: var(--display); font-weight: 800; font-stretch: 80%; text-transform: uppercase; font-size: clamp(30px, 4.6vw, 46px);
  line-height: 1; margin: 0 0 10px; }}
.hero {{ padding-top: 56px; display: grid; gap: 40px; }}
.portrait .hero {{ grid-template-columns: minmax(0, 1fr) minmax(260px, 360px); align-items: center; }}
.lede {{ font-size: 19px; color: var(--dim); max-width: 38em; margin: 22px 0 0; }}
.dry {{ display: inline-block; margin-top: 16px; font: 500 13px var(--mono); color: var(--bg); background: var(--teal); padding: 4px 10px; border-radius: 6px; }}
.stats {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin-top: 34px; border-top: 1px solid var(--line); }}
.stat {{ padding: 14px 14px 0 0; }}
.stat b {{ display: block; font-family: var(--display); font-stretch: 85%; font-weight: 800; font-size: 30px; color: var(--teal); line-height: 1.1; }}
.stat span {{ display: block; color: var(--dim); font-size: 13px; line-height: 1.4; margin-top: 4px; }}
.screen {{ border-radius: 18px; padding: 10px; background: #050608; border: 1px solid var(--line); box-shadow: 0 30px 80px rgba(0,0,0,.55); }}
.portrait .screen {{ border-radius: 28px; }}
.screen video {{ display: block; width: 100%; aspect-ratio: var(--ar); border-radius: 12px; background: #000; }}
.dl {{ display: flex; gap: 14px; justify-content: center; margin-top: 10px; font: 12px var(--mono); }}
.dl a {{ color: var(--dim); }}
section {{ padding-top: 80px; }}
.sub {{ color: var(--dim); max-width: 46em; margin: 0 0 26px; }}
.steps {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; counter-reset: step; }}
.step {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }}
.step::before {{ counter-increment: step; content: "0" counter(step); font: 600 12px var(--mono); color: var(--accent); }}
.step h3 {{ margin: 6px 0 6px; font-size: 16px; }}
.step p {{ margin: 0; color: var(--dim); font-size: 14px; line-height: 1.55; }}
.step code {{ font: 12px var(--mono); color: var(--teal); }}
.refs {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; margin-top: 18px; }}
.ref {{ margin: 0; }}
.ref img {{ width: 100%; aspect-ratio: 16 / 9; object-fit: cover; border-radius: 10px; display: block; border: 1px solid var(--line); background: #000; }}
.portrait .ref img {{ aspect-ratio: 3 / 4; }}
.ref figcaption {{ display: flex; justify-content: space-between; font-size: 13px; margin-top: 6px; gap: 8px; }}
.ref figcaption span {{ color: var(--faint); font: 12px var(--mono); }}
.shots {{ display: grid; grid-template-columns: repeat(auto-fill, minmax({shot_min}, 1fr)); gap: 18px; }}
.shot {{ min-width: 0; }}
.frame {{ position: relative; display: block; width: 100%; padding: 0; border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
  background: #000; cursor: pointer; aspect-ratio: var(--ar); }}
.frame img, .frame video {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
.frame video {{ opacity: 0; transition: opacity .2s; }}
.frame.playing video {{ opacity: 1; }}
.num {{ position: absolute; left: 10px; top: 8px; font: 600 12px var(--mono); color: #fff; text-shadow: 0 1px 4px #000; }}
.meta {{ padding-top: 10px; font-size: 14px; }}
.meta p {{ margin: 0; }}
.kind {{ font: 12px var(--mono); color: var(--faint); }}
.who {{ color: var(--dim); }}
.line {{ margin-top: 6px !important; }}
.line span {{ color: var(--accent); font-weight: 600; }}
.line.quiet {{ color: var(--faint); }}
dl {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 10px 0 6px; }}
dl div {{ border-top: 1px solid var(--line); padding-top: 6px; }}
dt {{ font: 11px var(--mono); color: var(--faint); text-transform: uppercase; letter-spacing: .05em; }}
dd {{ margin: 0; font: 13px var(--mono); color: var(--teal); }}
details summary {{ cursor: pointer; color: var(--dim); font-size: 13px; }}
details p {{ color: var(--dim); font-size: 13px; line-height: 1.55; margin-top: 8px !important; }}
.table {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; font: 12.5px var(--mono); }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }}
th {{ color: var(--faint); font-weight: 600; }}
td.num, th.num {{ text-align: right; }}
tr.err td {{ color: #e5484d; }}
.totals {{ display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 14px 0 0; color: var(--dim); font-size: 14px; }}
.totals b {{ color: var(--ink); }}
footer {{ margin-top: 80px; padding-top: 20px; border-top: 1px solid var(--line); color: var(--faint); font-size: 13px; }}
@media (max-width: 860px) {{
  .portrait .hero {{ grid-template-columns: 1fr; }}
  .portrait .screen {{ max-width: 340px; }}
  .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .steps {{ grid-template-columns: 1fr 1fr; }}
}}
@media (max-width: 520px) {{ .steps {{ grid-template-columns: 1fr; }} }}
@media (prefers-reduced-motion: reduce) {{ .frame video {{ transition: none; }} }}
</style>
</head>
<body class="{layout}">
<main class="wrap">
  <header class="hero">
    <div>
      <div class="eyebrow">Making-of · one API, the same cast in every shot</div>
      <h1>{title_html}</h1>
      <p class="lede">{logline}</p>
      {dry_note}
      <div class="stats">{stats}</div>
    </div>
    <div>
      <div class="screen"><video src="media/film.mp4" poster="media/poster.jpg" controls playsinline preload="metadata"></video></div>
      <p class="dl"><a href="media/film.mp4" download>Download the film</a><a href="media/film.srt" download {has_srt}>Subtitles</a></p>
    </div>
  </header>

  <section>
    <h2>How the cast stays the same</h2>
    <p class="sub">Video models forget a face between shots. This pipeline never asks them to remember: every shot starts from the same reference sheets.</p>
    <div class="steps">
      <div class="step"><h3>Reference sheets</h3><p>FLUX 3 Image Klein draws each character, prop and place once. <code>/v1/images/generations</code></p></div>
      <div class="step"><h3>First and last frames</h3><p>FLUX 3 Klein Edit composes every shot from those sheets, up to 10 at a time, named Image 1, Image 2… in the prompt; a last frame pins down shots that must end exactly. <code>/v1/images/edits</code></p></div>
      <div class="step"><h3>Clips with sound</h3><p>MiniMax H3 Fast animates each shot from its frames, with the line spoken, the narration and the room tone. <code>/v1/videos/generations</code></p></div>
      <div class="step"><h3>Assembly</h3><p>ffmpeg joins the clips with timed subtitles, a title card and an end card that carries the receipts.</p></div>
    </div>
    <div class="refs">{refs}</div>
  </section>

  <section>
    <h2>Every shot</h2>
    <p class="sub">Hover or tap a frame to play its clip ({pass_} cut, tier {tiers}). Each card shows what its frames and its clip took, and the prompts behind them.</p>
    <div class="shots">{cards}
    </div>
  </section>

  <section>
    <h2>Receipts</h2>
    <p class="sub">Every API call this film took, drafts, re-rolls and failed calls included. Costs are estimates from the public price list (read {prices_read}).</p>
    <div class="table"><table>
      <thead><tr><th>Time</th><th>Step</th><th>Item</th><th>Pass</th><th>Model</th><th>Tier</th><th class="num">Seconds</th><th class="num">Cost</th></tr></thead>
      <tbody>{receipts}</tbody>
    </table></div>
    <p class="totals"><span><b>{calls}</b> calls</span><span><b>{failed}</b> failed</span><span>this cut <b>{this_cut}</b></span><span>all-in <b>{all_in}</b></span></p>
  </section>

  <footer>{byline} · <a href="{repo}">source and pipeline</a> · Story, prompts and edit by a human; pictures, motion and voices by the models named above.</footer>
</main>
<script>
document.querySelectorAll(".frame").forEach((btn) => {{
  const v = btn.querySelector("video");
  const play = () => {{ btn.classList.add("playing"); v.play().catch(() => {{}}); }};
  const stop = () => {{ btn.classList.remove("playing"); v.pause(); }};
  btn.addEventListener("pointerenter", (e) => {{ if (e.pointerType === "mouse") play(); }});
  btn.addEventListener("pointerleave", (e) => {{ if (e.pointerType === "mouse") stop(); }});
  btn.addEventListener("click", () => (btn.classList.contains("playing") ? stop() : play()));
}});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
