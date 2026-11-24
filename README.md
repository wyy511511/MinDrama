# MinDrama

Micro-dramas with the same cast in every shot, generated end to end through one API
([Nunchux Modelverse](https://www.nunchux.ai/modelverse)): reference sheets, first and last frames, clips with
dialogue, narration and sound, then the film, with a receipt for every API call.

The hard part of AI video is not one good clip. It is shot 17 still looking like shot 1. This pipeline never asks a
model to remember a face: every shot is drawn from the same reference sheets.

## How the cast stays the same

| Step | Model | What it does |
|---|---|---|
| Reference sheets | FLUX 3 Image Klein | A turnaround sheet per character, a three-view sheet per prop, an empty plate per location |
| First and last frames | FLUX 3 Klein Edit | Composes each shot from up to 10 of those sheets, named Image 1, Image 2… in the prompt; a last frame pins down shots that must end exactly (a number that changes, a car that stops) |
| Clips | MiniMax H3 Fast | Animates each shot from its frames, 4 to 15 seconds, with the line spoken, off-screen narration and the room tone |
| Assembly | ffmpeg | Subtitles timed sentence by sentence, a title card, and an end card that carries the receipts |

A shot can also start from the previous shot's last frame (`"first_frame": "chain"`): the frame is redrawn
against the reference sheets first, so drift does not add up from shot to shot.

## Stories

- `stories/rental-booking-paradox.json` (default): *The Rental Booking Paradox*, episode 1. 16:9, 20 shots,
  three scenes, a cast of three plus an off-screen narrator.
- `stories/rain-check.json`: *Rain Check*, a 60-second vertical romance, 10 shots.

A story holds its characters (`look`, `outfit`, `voice`, or `"vo": true` for a voice that never appears), props,
locations, and shots: `frame` (the first frame), optional `last_frame` (what changes by the end), `action`,
`camera`, `speaker` and `line` (one speaker per shot), `delivery`, `sound`, `text` (the only readable text allowed
in the frame), `duration` (4 to 15 s), an optional `wardrobe` change and an optional `cut` to trim the clip.

## Run it

Python 3.10+ and ffmpeg (`brew install ffmpeg`); no pip packages. Put `NUNCHUX_API_KEY` in `.env`
(`cp .env.example .env`).

```sh
python3 make_film.py plan                     # shot list, timing checks and the cost estimate
python3 make_film.py requests                 # every request body a run would send (out/<story>/requests.preview.json)
python3 make_film.py credits                  # balance and limits
python3 make_film.py refs                     # reference sheets
python3 make_film.py keyframes                # first and last frames
python3 make_film.py voices                   # one draft clip per voice, to listen first
python3 make_film.py clips --pass draft       # every clip at 480p on the Radical Value tier
python3 make_film.py assemble --pass draft
python3 make_film.py review                   # out/<story>/review.html: every image, prompt and clip, with sound
python3 make_film.py all --pass final         # 768p on the Radical Speed tier, then the film
python3 build_page.py                         # the making-of page, in site/
```

Re-roll anything with a new seed: `--shots e1s04,e1s11 --redo`, or `refs --redo character:clerk`. Finished items
are kept, so any step can be run again. `--story stories/rain-check.json` picks another story.

Try it with no key and no cost first: `python3 make_film.py all --dry-run && python3 build_page.py --dry-run` draws
placeholder media into `out-dryrun/` and `site-dryrun/`.

Every call is appended to `out/<story>/receipts.jsonl` with its latency and estimated cost, and a run stops before
the estimated spend would pass `--budget` (default $8, or `BUDGET_USD` in `.env`).

## Cost

Estimated from the public price list on 23 Nov 2026, for *The Rental Booking Paradox* (112 s of clips):

| Item | Estimate |
|---|---:|
| 9 reference sheets | $0.06 |
| 20 first frames + 2 last frames | $0.12 |
| Clips, draft (480p, Radical Value) | $2.02 |
| Clips, final (768p, Radical Speed) | $3.36 |

## Deploy the making-of page

`site/` is a static folder: drop it on [Netlify Drop](https://app.netlify.com/drop), run
`npx vercel deploy site --prod`, or serve it with GitHub Pages.

## Notes

- The image and video endpoints take no negative prompt and no guidance scale, so every "don't" is written into
  the prompt as a plain sentence.
- The video endpoint takes frames only as public https URLs, so the pipeline passes the URLs the image API returns
  (kept for 7 days) and downloads every clip as soon as it arrives.
- Check each model's license and the provider's terms before any commercial use; MiniMax H3 is under its own
  community license.
- The storyboard fields (shot size, camera, line) and last-frame chaining follow
  [LocalMiniDrama](https://github.com/xuanyustudio/LocalMiniDrama) (MIT).

## License

MIT
