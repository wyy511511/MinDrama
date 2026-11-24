# Rain Check

A 60-second vertical micro-drama with the same two people in every shot, generated end to end through one API
([Nunchux Modelverse](https://www.nunchux.ai/modelverse)): reference sheets, first frames, clips with dialogue and
sound, then the film, with a receipt for every API call.

The hard part of AI video is not one good clip. It is shot 7 still looking like shot 1. This pipeline never asks a
model to remember a face: every shot is drawn from the same reference sheets.

## How the cast stays the same

| Step | Model | What it does |
|---|---|---|
| 1. Reference sheets | FLUX 3 Image Klein | Draws each character, prop and location once |
| 2. First frames | FLUX 3 Klein Edit | Composes each shot from up to 10 of those sheets, named Image 1, Image 2… in the prompt |
| 3. Clips | MiniMax H3 Fast | Animates each first frame into 6 seconds, with the line spoken and the room tone |
| 4. Assembly | ffmpeg | Subtitles, a title card, and an end card that carries the receipts |

A shot can also start from the previous shot's last frame (`"first_frame": "chain"` in `story.json`): the frame
is redrawn against the reference sheets first, so drift does not add up from shot to shot.

## Run it

Python 3.10+ and ffmpeg (`brew install ffmpeg`); no pip packages.

```sh
cp .env.example .env                                   # add NUNCHUX_API_KEY
python3 make_film.py plan                              # shot list and cost estimate, no API calls
python3 make_film.py all --pass draft                  # 480p on the Radical Value tier
python3 make_film.py clips --pass draft --shots s03,s09 --redo   # re-roll weak shots with new seeds
python3 make_film.py all --pass final                  # 768p on the Radical Speed tier
python3 build_page.py                                  # the making-of page, in site/
```

Try it with no key and no cost first: `python3 make_film.py all --pass draft --dry-run && python3 build_page.py --dry-run`
draws placeholder media into `out-dryrun/` and `site-dryrun/`.

Finished items are kept, so a run that stops can be started again with the same command. Every call is appended to
`out/receipts.jsonl` with its latency and estimated cost, and the run stops before the estimated spend would pass
`--budget` (default $8, or `BUDGET_USD` in `.env`).

## Cost

Estimated from the public price list on 23 Nov 2026, for 10 shots of 6 seconds:

| Item | Estimate |
|---|---:|
| 5 reference sheets + 10 first frames | $0.09 |
| Clips, draft (480p, Radical Value) | $1.08 |
| Clips, final (768p, Radical Speed) | $1.80 |

One draft and one final come to about $3 before re-rolls.

## Write your own story

Everything is in `story.json`: characters (`look`, `outfit`, `voice`), props, locations, and shots (`frame` for the
first frame, `action`, `camera`, `speaker` and `line`, `sound`, `duration`, an optional `wardrobe` change, and
`"first_frame": "chain"`). One speaker per shot keeps the voices steadier.

## Deploy the making-of page

`site/` is a static folder: drop it on [Netlify Drop](https://app.netlify.com/drop), run
`npx vercel deploy site --prod`, or serve it with GitHub Pages.

## Notes

- The video endpoint takes reference images only as public https URLs, so first frames use the URLs the image API
  returns (kept for 7 days); clip URLs expire sooner, so outputs are downloaded as soon as they arrive.
- Check each model's license and the provider's terms before any commercial use; MiniMax H3 is under its own
  community license.
- The storyboard fields (shot size, camera, line) and the last-frame chaining follow
  [LocalMiniDrama](https://github.com/xuanyustudio/LocalMiniDrama) (MIT).

## License

MIT
