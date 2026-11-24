#!/usr/bin/env python3
"""A review round as one self-contained page: the asset tables, and every line and prompt diffed against the
version that was reviewed.

    python3 review.py --round 2 --since context/review_v1   # a snapshot folder: story.json + keyframes/
    python3 review.py --round 1 --since HEAD                # or a commit: the story and assets/ in it

Images and draft clips are embedded, so the file opens anywhere, a phone included. Additions are orange,
deletions struck through. Review items come from the "review" lists in the story file: who found the problem
(self-check or the reviewer), whether self-check caught it, the reviewer's note, the fix, whether it can be fixed in
post, and its status.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import make_film as mf  # noqa: E402


def e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def diff(old: str | None, new: str | None) -> str:
    """Word-level diff: unchanged text plain, deletions struck through, additions orange."""
    old, new = old or "", new or ""
    if old == new:
        return e(new)
    a, b = re.findall(r"\S+\s*", old), re.findall(r"\S+\s*", new)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(e("".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{e(''.join(a[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{e(''.join(b[j1:j2]))}</ins>")
    return "".join(out)


class Baseline:
    """The reviewed version: a snapshot folder (story.json, keyframes/) or a git commit (the story, assets/<story>/)."""

    def __init__(self, since: str, story_path: Path, slug: str):
        self.dir = (ROOT / since) if (ROOT / since).is_dir() else None
        self.ref, self.slug = since, slug
        self.story_rel = story_path.resolve().relative_to(ROOT).as_posix()

    def story(self) -> dict:
        raw = (self.dir / "story.json").read_bytes() if self.dir else self._git(self.story_rel)
        return json.loads(raw) if raw else {"shots": [], "characters": {}, "props": {}, "locations": {}}

    def frame(self, name: str) -> bytes | None:
        if self.dir:
            p = self.dir / "keyframes" / name
            return p.read_bytes() if p.exists() else None
        return self._git(f"assets/{self.slug}/keyframes/{name}")

    def _git(self, path: str) -> bytes | None:
        p = subprocess.run(["git", "-C", str(ROOT), "show", f"{self.ref}:{path}"], capture_output=True)
        return p.stdout if p.returncode == 0 else None


def thumb(data: bytes | None = None, path: Path | None = None, width: int = 360) -> str:
    """A small JPEG as a data URI."""
    if data is None and (path is None or not path.is_file()):
        return ""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.jpg"
        if data is not None:
            src.write_bytes(data)
        else:
            src = path
        dst = Path(d) / "out.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", f"scale={width}:-2", "-q:v", "5", str(dst)],
                       check=True)
        return "data:image/jpeg;base64," + base64.b64encode(dst.read_bytes()).decode()


def video_uri(path: Path) -> str:
    return "data:video/mp4;base64," + base64.b64encode(path.read_bytes()).decode() if path.is_file() else ""


def found_by(x: dict) -> str:
    who = '<span class="b self">Self-check</span>' if x.get("by") == "self" else '<span class="b human">Reviewer</span>'
    manual = ' <span class="b manual">Manual table</span>' if x.get("manual") else ""
    return f'{who}{manual}<div class="sub">round {x.get("round", 0)} · step {e(x.get("step", ""))}</div>'


def caught(x: dict) -> str:
    if x.get("by") == "self":
        return '<span class="b yes">Yes</span>'
    if x.get("self_caught") is False:
        return '<span class="b no">No, missed</span>'
    return '<span class="b no">No</span>'


def cells(entries: list[dict]) -> dict:
    """The review columns of one row; several entries stack in each cell, oldest first."""
    keys = ("issue", "found", "caught", "note", "fix", "post", "status")
    if not entries:
        return {k: '<span class="dim">—</span>' for k in keys}
    col = lambda f: "".join(f'<div class="entry">{f(x)}</div>' for x in entries)  # noqa: E731
    return {
        "issue": col(lambda x: e(x.get("issue"))),
        "found": col(found_by),
        "caught": col(caught),
        "note": col(lambda x: f'<q>{e(x["note"])}</q>' if x.get("note") else '<span class="dim">—</span>'),
        "fix": col(lambda x: e(x.get("fix"))),
        "post": col(lambda x: '<span class="b yes">Yes: trim or crop in FCPX / CapCut</span>' if x.get("post_fixable")
                    else '<span class="dim">No: regenerate</span>'),
        "status": col(lambda x: e(x.get("status"))),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--since", default="HEAD", help="a snapshot folder (story.json, keyframes/) or a commit")
    ap.add_argument("--story", type=Path, default=mf.DEFAULT_STORY)
    a = ap.parse_args(argv)
    mf.load_env()

    story = json.loads(a.story.read_text(encoding="utf-8"))
    slug = mf.slug(story["title"])
    base = Baseline(a.since, a.story, slug)
    old = base.story()
    mf.OUT = ROOT / "out" / slug
    out = mf.OUT
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (out / "receipts.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = [r for r in rows if r.get("status") == "ok"]
    spent_by_item: dict[str, float] = {}
    for r in ok:
        spent_by_item[r["item"]] = spent_by_item.get(r["item"], 0) + r["est_cost"]
    balance = mf.Nunchux(os.environ["NUNCHUX_API_KEY"]).credits() if os.environ.get("NUNCHUX_API_KEY") else {}

    old_shots = {s["id"]: s for s in old["shots"]}
    old_order = {s["id"]: i + 1 for i, s in enumerate(old["shots"])}
    old_ids = [s["id"] for s in old["shots"]]
    new_ids = [s["id"] for s in story["shots"] if s["id"] in old_shots]
    stable = set()
    for op, i1, i2, _, _ in difflib.SequenceMatcher(None, old_ids, new_ids, autojunk=False).get_opcodes():
        if op == "equal":
            stable.update(old_ids[i1:i2])
    moved = set(new_ids) - stable
    common_old = [i for i in old_ids if i in set(new_ids)]

    def neighbours(seq: list[str], i: str) -> tuple:
        k = seq.index(i)
        return (seq[k - 1] if k > 0 else None, seq[k + 1] if k + 1 < len(seq) else None)
    # a swap moves both shots: each has a new shot before it and a new shot after it
    moved |= {i for i in new_ids if all(x != y for x, y in zip(neighbours(common_old, i), neighbours(new_ids, i)))}

    # ---- reference sheets
    ref_rows = []
    for key, kind, _, _ in mf.ref_items(story):
        k, name = key.split(":")
        pool = {"character": story["characters"], "prop": story["props"], "location": story["locations"]}[k]
        old_pool = {"character": old.get("characters", {}), "prop": old.get("props", {}),
                    "location": old.get("locations", {})}[k]
        item, old_item = pool[name], old_pool.get(name, {})
        entry = man["refs"].get(key, {})
        earlier = sorted((out / "refs").glob(f"{key.replace(':', '-')}.a*.jpg"))
        c = cells(item.get("review", []))
        desc = lambda it: it.get("look", "") + ((" Outfit: " + it["outfit"]) if it.get("outfit") else "")  # noqa: E731
        ref_rows.append(f"""<tr>
          <td class="sticky"><b>{e(item.get('name', name))}</b><div class="sub">{e(key)}</div></td>
          <td><img src="{thumb(path=out / entry.get('jpg', '-'), width=320)}" alt=""></td>
          <td class="num">{entry.get('attempt', '—')}<div class="sub">{len(earlier)} earlier</div></td>
          <td class="text">{diff(desc(old_item), desc(item))}</td>
          <td>{c['issue']}</td><td>{c['found']}</td><td>{c['caught']}</td><td>{c['fix']}</td><td>{c['status']}</td>
          <td class="num">${spent_by_item.get(key, 0):.3f}</td></tr>""")

    # ---- shots
    shot_rows, cards, unchanged = [], [], []
    for n, s in enumerate(story["shots"], 1):
        sid = s["id"]
        o = old_shots.get(sid)
        o_order = old_order.get(sid)
        order = f"{n}" if o_order == n else (f"{o_order} → {n}" if o_order else f"new → {n}")
        dur_old, dur_new = (o or {}).get("duration"), s.get("duration", 6)
        dur = f"{dur_new}" if (o is None or dur_old == dur_new) else f"<del>{dur_old}</del> <ins>{dur_new}</ins>"
        speaker = lambda shot: (lambda c: f"{c.get('name', '')}{' (V.O.)' if c.get('vo') else ''}: ")(  # noqa: E731
            story["characters"].get(shot.get("speaker", ""), {})) if shot.get("line") else ""
        line = diff((speaker(o) + (o.get("line") or "")) if o else "", speaker(s) + (s.get("line") or "")) \
            or '<span class="dim">—</span>'
        kf, last = man["keyframes"].get(sid, {}), man["keyframes"].get(f"{sid}:last")
        clip = man["clips"].get("draft", {}).get(sid)
        cur_kf = (out / kf["jpg"]) if kf else None
        cur_last = (out / last["jpg"]) if last else None
        old_kf, old_last = base.frame(f"{sid}.jpg"), base.frame(f"{sid}-last.jpg")
        kf_changed = bool(cur_kf and cur_kf.is_file() and old_kf is not None and old_kf != cur_kf.read_bytes())
        last_changed = bool(cur_last and cur_last.is_file() and old_last is not None and old_last != cur_last.read_bytes())
        p_kf = (mf.keyframe_prompt(old, o) if o else None, mf.keyframe_prompt(story, s))
        p_last = (mf.last_frame_prompt(old, o) if o and o.get("last_frame") else None,
                  mf.last_frame_prompt(story, s) if s.get("last_frame") else None)
        p_clip = (mf.clip_prompt(old, o, bool(o.get("last_frame"))) if o else None,
                  mf.clip_prompt(story, s, bool(s.get("last_frame"))))
        changes = []
        if o is None:
            changes.append("new shot")
        else:
            if sid in moved:
                changes.append("order")
            if o.get("line") != s.get("line"):
                changes.append("line")
            if dur_old != dur_new:
                changes.append("duration")
        if kf_changed:
            changes.append("first frame")
        if last_changed:
            changes.append("last frame")
        if o is not None and (p_kf[0] != p_kf[1] or p_last[0] != p_last[1] or p_clip[0] != p_clip[1]):
            changes.append("prompt")
        version = (f'<span class="b v">v{a.round} · ' + " · ".join(changes) + "</span>") if changes \
            else f'<span class="dim">unchanged since v{a.round - 1}</span>'
        c = cells(s.get("review", []))
        post_note = f'<div class="sub">{e(s["post"])}</div>' if s.get("post") else ""
        kf_img = thumb(path=cur_kf, width=240) if cur_kf else ""
        last_img = thumb(path=cur_last, width=240) if cur_last else ""
        shot_rows.append(f"""<tr>
          <td class="sticky"><b>{e(sid)}</b><div class="sub">order {order}</div></td>
          <td>{e(s.get('scene'))}</td><td>{e(s['shot'])}<div class="sub">{e(s['camera'])}</div></td>
          <td class="num">{dur}</td><td class="text">{line}</td>
          <td>{f'<img src="{kf_img}" alt="">' if kf_img else '—'}</td>
          <td>{f'<img src="{last_img}" alt="">' if last_img else '<span class="dim">—</span>'}</td>
          <td>{'<a href="#c-' + e(sid) + '">▶ draft</a>' if clip else '<span class="dim">—</span>'}</td>
          <td>{version}</td>
          <td>{c['issue']}</td><td>{c['found']}</td><td>{c['caught']}</td><td class="text">{c['note']}</td>
          <td>{c['fix']}</td><td>{c['post']}{post_note}</td><td>{c['status']}</td>
          <td class="num">${spent_by_item.get(sid, 0):.3f}</td></tr>""")

        if not changes and not s.get("review") and not clip:
            unchanged.append(sid)
            continue
        imgs = []
        if kf_changed:
            imgs.append(f'<figure><img src="{thumb(data=old_kf, width=480)}" alt=""><figcaption>v{a.round - 1} first frame</figcaption></figure>')
        if cur_kf and cur_kf.is_file():
            tag = f"v{a.round}" if (kf_changed or o is None) else f"v{a.round - 1} = v{a.round}"
            imgs.append(f'<figure><img src="{thumb(path=cur_kf, width=480)}" alt=""><figcaption>{tag} first frame · attempt {kf.get("attempt")}</figcaption></figure>')
        if last_changed:
            imgs.append(f'<figure><img src="{thumb(data=old_last, width=480)}" alt=""><figcaption>v{a.round - 1} last frame</figcaption></figure>')
        if cur_last and cur_last.is_file():
            tag = f"v{a.round}" if last_changed else f"v{a.round - 1} = v{a.round}"
            imgs.append(f'<figure><img src="{thumb(path=cur_last, width=480)}" alt=""><figcaption>{tag} last frame · attempt {last.get("attempt")}</figcaption></figure>')
        if clip:
            imgs.append(f'<figure id="c-{e(sid)}"><video src="{video_uri(out / clip["file"])}" controls preload="metadata"></video>'
                        f'<figcaption>draft clip, 480p · attempt {clip["attempt"]}</figcaption></figure>')
        prompts = [("First-frame prompt", p_kf)]
        if p_last[0] or p_last[1]:
            prompts.append(("Last-frame prompt", p_last))
        prompts.append(("Clip prompt", p_clip))
        prompt_html = "".join(
            f'<div class="prompt"><h4>{t} <span class="tag">{f"v{a.round - 1} → v{a.round}" if po != pn else "unchanged"}</span></h4>'
            f'<p>{diff(po, pn)}</p></div>' for t, (po, pn) in prompts)
        review_html = "".join(
            f'<div class="entry">{found_by(x)} {caught(x)} <b>{e(x.get("issue"))}</b>'
            + (f'<q>{e(x["note"])}</q>' if x.get("note") else "")
            + f'<div>→ {e(x.get("fix"))} · <i>{e(x.get("status"))}</i>'
            + (' · <span class="b yes">fixable in post</span>' if x.get("post_fixable") else "") + "</div></div>"
            for x in s.get("review", []))
        cards.append(f"""<section class="card" id="s-{e(sid)}">
          <h3>{e(sid)} <span class="sub">order {order} · {e(s.get('scene'))} · {e(s['shot'])} · {dur_new} s</span> {version}</h3>
          <p class="line">{line}</p><div class="review">{review_html}</div>
          <div class="media">{''.join(imgs)}</div>{prompt_html}</section>""")

    all_entries = [x for s in story["shots"] for x in s.get("review", [])] + [
        x for pool in ("characters", "props", "locations") for it in story[pool].values() for x in it.get("review", [])]
    bal = balance.get("credits_remaining")
    page = TEMPLATE.format(
        title=e(story["title"]), round=a.round, prev=a.round - 1, since=e(a.since),
        version=e(story.get("version", f"v{a.round}")), shots_old=len(old["shots"]), shots_new=len(story["shots"]),
        dur_old=f"{sum(float(x.get('duration', 6)) for x in old['shots']):.0f}",
        dur_new=f"{sum(float(x.get('duration', 6)) for x in story['shots']):.0f}",
        n_self=sum(1 for x in all_entries if x.get("by") == "self"),
        n_human=sum(1 for x in all_entries if x.get("by") == "human"),
        n_missed=sum(1 for x in all_entries if x.get("self_caught") is False),
        spent=f"{sum(r['est_cost'] for r in ok):.2f}", balance=(f"${bal:.2f}" if isinstance(bal, (int, float)) else "—"),
        ref_rows="".join(ref_rows), shot_rows="".join(shot_rows), cards="".join(cards),
        unchanged=", ".join(unchanged) or "none")
    dest = out / f"review_v{a.round}.html"
    dest.write_text(page, encoding="utf-8")
    print(f"wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return 0


TEMPLATE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · review v{round}</title>
<style>
:root {{ color-scheme: dark; --bg:#0f1013; --panel:#17191e; --line:#2a2d35; --ink:#ecebe8; --dim:#8d909a; --orange:#ff9f1a; --teal:#3cc4bd; --red:#ef5b5b; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:24px max(16px, env(safe-area-inset-left)) 64px; background:var(--bg); color:var(--ink); font:14px/1.55 -apple-system, system-ui, sans-serif; }}
h1 {{ margin:0 0 4px; font-size:26px; }} h2 {{ margin:40px 0 10px; font-size:20px; }} h3 {{ margin:0 0 8px; font-size:17px; }} h4 {{ margin:12px 0 4px; font-size:13px; color:var(--dim); font-weight:600; }}
.sub {{ color:var(--dim); font-size:12px; font-weight:400; }} .dim {{ color:var(--dim); }}
.summary {{ display:flex; flex-wrap:wrap; gap:8px 24px; margin:12px 0; color:var(--dim); }} .summary b {{ color:var(--ink); }}
.legend {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin:10px 0 0; font-size:13px; }}
ins {{ color:var(--orange); background:rgba(255,159,26,.12); text-decoration:none; border-radius:3px; padding:0 1px; }}
del {{ color:#7d808a; text-decoration:line-through; text-decoration-color:var(--red); }}
.scroll {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--panel); }}
table {{ border-collapse:separate; border-spacing:0; min-width:2050px; font-size:12.5px; }}
th, td {{ text-align:left; vertical-align:top; padding:8px 10px; border-bottom:1px solid var(--line); }}
th {{ position:sticky; top:0; background:#1d2027; color:var(--dim); font-weight:600; white-space:nowrap; z-index:2; }}
td.sticky, th.sticky {{ position:sticky; left:0; background:#1b1d23; z-index:1; }}
td.num {{ white-space:nowrap; font-variant-numeric:tabular-nums; }}
td.text {{ min-width:240px; max-width:340px; }}
td img {{ width:150px; border-radius:6px; display:block; }}
.entry + .entry {{ margin-top:8px; padding-top:8px; border-top:1px dashed var(--line); }}
.b {{ display:inline-block; padding:1px 7px; border-radius:6px; font-size:11.5px; font-weight:600; white-space:nowrap; }}
.b.self {{ background:rgba(60,196,189,.16); color:var(--teal); }} .b.human {{ background:rgba(255,159,26,.16); color:var(--orange); }}
.b.manual {{ background:var(--orange); color:#1a1205; }} .b.yes {{ background:rgba(110,200,120,.16); color:#8fdc8f; }}
.b.no {{ background:rgba(239,91,91,.14); color:#f08a8a; }} .b.v {{ background:rgba(255,159,26,.16); color:var(--orange); }}
q {{ display:block; color:#d9c9a8; font-style:italic; }}
.card {{ margin-top:18px; padding:16px; background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
.card .line {{ margin:4px 0 8px; }} .review .entry {{ margin:6px 0; }}
.media {{ display:flex; flex-wrap:wrap; gap:10px; margin:10px 0; }}
.media figure {{ margin:0; width:min(360px, 100%); }} .media img, .media video {{ width:100%; border-radius:8px; background:#000; display:block; }}
figcaption {{ color:var(--dim); font-size:12px; margin-top:4px; }}
.prompt p {{ margin:0; padding:10px 12px; background:#111317; border:1px solid var(--line); border-radius:8px; font-size:13px; line-height:1.6; }}
.tag {{ font-size:11px; color:var(--dim); border:1px solid var(--line); border-radius:5px; padding:0 6px; margin-left:6px; }}
nav a {{ color:var(--orange); }}
</style></head>
<body>
<h1>{title} · review v{round}</h1>
<div class="sub">{version} · compared with v{prev} (<code>{since}</code>)</div>
<div class="summary">
  <span>Shots <b>{shots_old} → {shots_new}</b></span><span>Clip time <b>{dur_old} s → {dur_new} s</b></span>
  <span>Self-check items <b>{n_self}</b></span><span>Reviewer items <b>{n_human}</b></span><span>Missed by self-check <b>{n_missed}</b></span>
  <span>Spent (est.) <b>${spent}</b></span><span>Balance <b>{balance}</b></span>
</div>
<div class="legend"><span><ins>orange: added in v{round}</ins></span><span><del>struck: removed in v{round}</del></span>
  <span><span class="b self">Self-check</span> found by the pipeline's own review</span><span><span class="b human">Reviewer</span> found in human review</span>
  <span><span class="b yes">Yes</span> / <span class="b no">No, missed</span> whether self-check caught it</span></div>
<nav class="sub" style="margin-top:10px"><a href="#refs">Asset table: reference sheets</a> · <a href="#shots">Asset table: shots</a> · <a href="#changes">Changes and prompt versions</a></nav>

<h2 id="refs">Asset table · Reference sheets</h2>
<div class="scroll"><table>
<thead><tr><th class="sticky">Asset</th><th>Image</th><th>Attempt</th><th>Description (v{prev} → v{round})</th><th>Issue</th><th>Found by</th><th>Self-check caught it?</th><th>Fix</th><th>Status</th><th>Spent (est.)</th></tr></thead>
<tbody>{ref_rows}</tbody></table></div>

<h2 id="shots">Asset table · Shots, in v{round} order</h2>
<div class="scroll"><table>
<thead><tr><th class="sticky">Shot<div class="sub">order v{prev} → v{round}</div></th><th>Scene</th><th>Shot · camera</th><th>Duration (s)</th><th>Line (v{prev} → v{round})</th><th>First frame</th><th>Last frame</th><th>Draft clip</th><th>Version</th><th>Issue</th><th>Found by</th><th>Self-check caught it?</th><th>Reviewer's note</th><th>Fix</th><th>Fixable in post?</th><th>Status</th><th>Spent (est.)</th></tr></thead>
<tbody>{shot_rows}</tbody></table></div>

<h2 id="changes">Changes and prompt versions</h2>
<p class="sub">Shots changed in v{round}, shots with review items, and shots with a draft clip. Unchanged with nothing to review: {unchanged}.</p>
{cards}
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
