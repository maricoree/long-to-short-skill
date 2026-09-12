---
name: long-to-short
description: Cut a 15-30 second vertical short out of a long video plus its SRT: a layout preset decides the composition, captions are burned in one short phrase at a time, and the title, description, hashtags and tags come out of the same run. Ships a default look and can measure another from a reference screenshot or an existing short. Use when the user points at a long video and subtitles and asks for a short / short-form clip / вертикальный шортс.
---

# Make a short out of a long video

Input: a long video and its `captions.srt`.
Output: **a finished MP4**, plus the upload metadata (title, description,
hashtags, tags) — which is part of the deliverable, not an afterthought.

The look is fixed by a **layout preset**, not by taste in the moment. One ships
with the kit; a reference — a screenshot of the target frame, or a short to
match — replaces it (see *Deriving a look*). Settle which, once, and then every
fragment after that is a one-line render.

```
1080x1920 canvas                          presets/default.json
  y    0 .. 1920    the fragment cover-scaled, blurred, multiplied down
  y  238 .. 1198    the fragment, centre-cropped, above the middle
  y 1360 ..         animated captions, centred, 80 px display face
```

## Hard rules

- **Deliver a file, not a report.** The MP4 on disk is the deliverable, and so
  is the metadata block. Keep the prose to a few lines.
- **Never ship a short you have not looked at.** Cut first, then look at
  frames — but look. Grab a strip of frames from the actual export and *read* it.
- **Do not stretch anything.** Scale uniformly, then crop. The fragment window
  and the background are two crops of the same frame; only the background is
  darkened.
- **Do not invent captions, effects or composition.** The captions come from
  the SRT; the style comes from the preset.
- **Settle the look once, before the first render — never a second time.** A
  reference settles it silently; with no reference, ask one question (step 0).
  A render costs ~35 s, so asking once is cheaper than being wrong, and asking
  twice is only friction.
- **Never reuse a fragment that has already been published.** Finished pieces
  get named by their opening phrase — pass them all to `--used` and let the
  tool exclude them.
- Preserve the source's audio. Export H.264 + AAC, `-pix_fmt yuv420p`,
  `+faststart`.
- **The description is not a transcript.** See *Metadata* below. This is a rule
  worth stating explicitly, because the natural first draft is the narration
  with two words changed, and it adds nothing.

### Deliberately not automated

NCC / image correlation / ideal-geometry search / automatic parameter
optimisation / long comparison loops / per-frame comparison of a whole clip /
re-analysing timecodes already found / generating new images / hunting the
internet for originals / writing a new verification script per doubt.

Every one of these was tried and cost more than it returned. When the answer is
already in front of you — a frame you can look at, a number the tool printed —
look at it. Do not build a machine to look at it for you.

## The toolbox — run these, do not write your own

| tool | answers | cost |
|---|---|---|
| `tools/pick_fragment.py <video> <srt> [--used …]` | the SRT's offset, the silence gaps, and every unused 15-30 s window with its opening phrase | ~10 s |
| `tools/pick_fragment.py <video> <srt> --check T0 DUR` | are both ends in a silence, are any cues cut into, how many caption lines | ~10 s |
| `tools/build_short.py --src … --t0 … --dur … --out …` | the finished MP4 (two passes: composition, then captions) | ~35 s |
| `tools/meta.py --title … --desc-file … --tags … --srt …` | the metadata block, field limits, and the echo check | <1 s |

## Procedure

### 0. Settle the look (one question, once)

Everything visible except the caption *wording* comes from the preset, so this
is decided before anything is rendered — but it is one question, not a
questionnaire, and never a second time.

- **If the user gave a reference** — a screenshot of the target frame, or a
  short they want to match — that *is* the answer. Derive the preset from it
  (see *Deriving a look*) and render. Do not ask.
- **If they did not**, ask once, with three concrete answers rather than an
  open "what do you want":
  1. **the shipped default** — say what it actually is in a few words: the
     fragment centre-cropped above the middle, over a blurred darkened copy of
     itself, with captions in the lower third, white with a heavy black stroke,
     lower case, one short phrase at a time;
  2. **a reference** they can send — a screenshot or a link to a short they like;
  3. **explicit numbers** — `--canvas`, `--chars`, `--font-px`, `--font`, or a
     preset file.

Then render with whatever they chose, and **do not ask again for the rest of
the session**: the answer carries over to every further fragment. If they pick
(1) because they do not care, that is a real answer — do not re-litigate it on
the next short.

### 1. Pick the fragment (2 min)

```bash
python tools/pick_fragment.py LONG.mp4 caps.srt --used "opening phrase one" "phrase two" --top 25
```

Read three things off it:

- **the SRT offset.** Captions that "run fast" are usually not taste, it is a
  constant offset: the cue file is authored ahead of the speaker. A cue start
  *should* land in a silence, so the shift that puts most of them there is the
  offset. The tool prints the best offset, what offset 0.00 scores, and — when
  several offsets score the same — how wide that plateau is, because a cue
  starts when speech starts, i.e. at the *end* of the preceding silence, so the
  plateau runs from the true offset downwards and its upper edge is the
  estimate. The absolute fraction is not the test (many cue boundaries are
  mid-sentence wraps and land in speech even on a perfect file); the **margin
  over offset 0.00** is. Pass the answer as `--cap-shift`.
- **the windows**, densest first, each with the phrase it opens on. Choose one
  on content — 15-30 s, one coherent idea, and something to *see* (a shot that
  matches the narration beat for beat).
- **the exclusions** — anything passed to `--used` is already cut out.

Then check the pick before rendering anything:

```bash
python tools/pick_fragment.py LONG.mp4 caps.srt --check 146.40 23.30
```

Both ends must report a silence. An end in speech is a word cut in half, and
**no numeric check on the render will see it**. Note the warning about a cue
that starts before the window: the renderer DROPS that cue rather than clipping
it, because clipping produced 0.02 s stubs of the previous sentence on frame 0.

### 2. Build it (1 min of typing, 35 s of rendering)

```bash
python tools/build_short.py --src LONG.mp4 --srt caps.srt \
       --t0 146.40 --dur 23.30 --cap-shift 1.00 --out short.mp4
```

Geometry comes from `presets/default.json`. `--preset NAME` picks another,
`--canvas WxH` scales the whole look uniformly, `--font` pins a typeface, and
`--chars` / `--font-px` / `--min-hold` adjust the captions. `--stage1-only`
writes the composition without captions; `--caps-only` re-burns captions onto
an existing stage1 — use it for every caption-only fix (a shift, a hold, a
re-split), it is 15 s instead of 35.

### 3. Look at it

```bash
ffmpeg -v error -i OUT.mp4 -vf "select='eq(n\,0)+eq(n\,40)+eq(n\,200)+eq(n\,600)+eq(n\,1000)+eq(n\,1397)',scale=270:-1,tile=6x1" -frames:v 1 -fps_mode passthrough look.png
```

**`-fps_mode passthrough` is not optional.** Without it ffmpeg CFR-pads the
`select` output and you review the same frame six times while believing you
checked six.

Read `look.png` and confirm: the captions are on screen, in order, in lower
case, not clipped, and the background is a picture rather than black. Then check
the first frame specifically — it is where a cut-into cue shows up.

To check the background was darkened rather than crushed, compare a band of the
background against the same band of the source: it should come out at roughly
the preset's `darken`.

```bash
ffmpeg -v error -ss 5 -i OUT.mp4 -vf "crop=1080:200:0:0,scale=1:1" -frames:v 1 -f rawvideo -pix_fmt gray - | xxd -p
```

### 4. Metadata (2 min)

```bash
python tools/meta.py --title "…" --desc-file desc.txt --tags "…" --srt caps.srt --t0 146.40 --dur 23.30 --out metadata.txt
```

Exit code 2 means a hard failure. Fix and re-run; it is instant.

## Deriving a look from a reference

The user's screenshot is the canvas at some smaller width. **One factor settles
every edge at once** — do not estimate them one by one, which is how
conflicting guesses happen.

A 1080x1920 canvas shown at 474 px wide is 843 px tall. If the screenshot is 820
px tall it is that canvas with a few px trimmed, so **`canvas_y = shot_y ×
1080/474`** and the same factor for x. Worked example, which is where the
shipped preset's numbers came from:

- the fragment window measured `shot y 104..527, x 5..471` → 1062x963 at
  (11,237) → rounded to **1056x960 at (12,238)**, aspect 1.103. That is **not a
  16:9 fit** — a 16:9 fit at 1056 wide would be 594 tall. It is the source
  scaled to the window's height and **centre-cropped** to its width. The tool
  derives that crop from the window's aspect, so a 4:3 or a vertical source
  works without touching the preset.
- the caption ink rows measured 597..622 → **cap-line top at canvas 1360**.
  Ink height 26 px × 2.2776 ≈ 59 px, and 59 / 0.73 em ≈ **80 px font**.
- only two words were on screen in the reference, which proves the look reveals
  **one short phrase at a time**, not a whole SRT cue.

Reconcile the height two ways before rendering — the arithmetic is cheap, a
re-render is not. Write the result to `presets/<name>.json`; only the keys you
change are needed, they merge over the default.

## Captions

Text rules: **lower case**, **no punctuation** except the apostrophe that
belongs to a word (*you're*, *that's*), and **13-15 characters per line**.

- Fold the typographic apostrophe (`’ ‘ ʼ`) to ASCII **before** stripping
  `[^\w\s']`, or the strip deletes it along with the commas.
- **Split lines by DP, not greedily.** Greedy leaves a stub ("…morning hanging"
  + "out"), and nudging words between neighbouring lines only relocates it — on
  one cue that produced a two-character line "of". A DP over the whole cue
  charging a short line quadratically does not let stubs form.
- **Apportion a cue by characters**, so each line appears about when it is
  spoken.
- A line is visible from its start until the next line starts, with a 0.10 s
  alpha ramp at each end (so nothing ghosts) and a 20 px ease-out rise.
- **Pillow's ascender anchor sits above the real ink.** Crop each phrase image
  to its alpha bbox and carry the ink offset forward, then place at
  `cap_top - ink_top + dy` — otherwise the caption lands low, which is a defect
  you will otherwise chase by eye across several renders.
- Shadow: white text, a 5.5 px black stroke, over two layered soft black
  shadows (16 px blur at 0.55, 5 px blur at 0.75). That is the "beautiful
  shadow" look.

### `min_hold` must be feasible, or it beats the sync

A lone short line once got a 0.23 s window ("you") — an unreadable flash, which
is part of what "the captions run fast" turns out to mean. `min_hold` floors the
shortest window, but **the floor is only valid if `n_lines × min_hold ≤ dur`**:

- 28 lines over 23.3 s at 0.85 s: every line came out *exactly* 0.85 s, the
  track became a rigid grid, it drifted off the narration and pushed the first
  line **before the fragment started** (28 × 0.85 = 23.8 s > 23.3 s, so the
  floor won over the sync). At 0.70 s the holds are 0.70-1.38 s with a worst
  line movement of 0.45 s.

`build_short.py` checks this and says so when it does not hold. Do not just
raise the floor until it "feels" readable — sweep it and look at the drift it
costs.

Note that "more readable lines" and "13-15 characters" fight each other: 364
characters of speech over 23.3 s is ~15.6 chars/s, and it cannot be slowed
without dropping words. Say so once, with the number, rather than silently
trading one against the other.

## Background: darkening is a MULTIPLY, never `eq=brightness`

"50 % opacity black" means `out = in × 0.5`, written
`colorchannelmixer=rr=0.5:gg=0.5:bb=0.5`. `eq=brightness=-0.16` looks
equivalent and is **subtractive** — 41 levels off every channel — and on night
scenes sitting at 20-50 it crushed the whole background to a measured **0-8**,
i.e. pure black. "The background is too dark" was that bug, not taste. Sanity
check the ratio after a render: it should measure close to `darken`.

Cover-scale the background **from the source's own aspect**, never a hard-coded
16:9, and round it to an **even** number: 1920 × 1920/1080 = 3413.33, and taking
3413 instead of 3414 shifts the crop a pixel and changes every background pixel.
The chain is scale → centre crop → downscale to a quarter → `gblur` → multiply →
upscale: blurring a small image is what makes it cheap.

## Render pipeline

Two passes, because per-frame ffmpeg caption graphs are unusably slow and an
overlay of 40 PNG inputs is slower still:

1. **pass 1** — composition: blurred background + fragment window → `stage1.mp4`.
2. **pass 2** — one `overlay=0:<strip_y>`, fed by a Pillow producer writing raw
   RGBA down a pipe (`-f rawvideo -pix_fmt rgba -s 1080x300 -r 60 -i -`). The
   strip is only canvas-wide by a few hundred px tall, so the whole pipe is
   ~1.9 GB and the pass is ~15 s.

## Metadata

Write it every time, even if not asked: title, description, ~3 hashtags, and a
tag block up to **500 characters** (YouTube's cap; count it, don't guess). Only
the **first line of the description** shows before "…more", so the hook goes
there.

**The description must ADD what the clip does not say** — what the thing is, why
it is remarkable, or a question for the comments. `tools/meta.py` fails on any
5-word run — or 4-word run with two non-stopwords — shared with the fragment's
own captions, so a transcript cannot ship by accident. (The check had to be
tightened: a bare 4-gram flagged "is one of those" as plagiarism, and a checker
that cries wolf is one nobody reads. Pass `--t0`/`--dur` so it compares against
the fragment and not the whole subtitle file.)

Three description shapes work, and offering the three is faster than guessing
the tone:

1. **context** — what the thing is and why it matters, e.g. "Most game worlds
   only exist where you're looking. This one doesn't work that way."
2. **compressed** — two sentences, no setup, strongest line first.
3. **question** — ends on something the audience can answer in the comments.

Titles carry the hook, not the topic: *"He Was Alive This Morning. Days Later
You Find His Corpse"* beats *"The A-Life System Explained"*. Write the metadata
in the language of the source's narration, and say once that you can translate
it.

## Environment

`ffmpeg`/`ffprobe` on PATH; Python 3.8+ with `numpy` and `Pillow`; no OpenCV.
Set `PYTHONIOENCODING=utf-8` on a Windows console so a non-ASCII `print` does
not kill the run — the tools try to reconfigure stdout themselves, but it costs
nothing to be sure.

Fonts are found by searching the platform's font directories for a heavy
display face. Override with `--font`, the preset's `"font"` key, or
`$LONG_TO_SHORT_FONT`. Note that a font installed by double-clicking lives in
the **per-user** directory on Windows and `~/Library/Fonts` on macOS, *not* in
the system font directory.

Measuring silence: mono 16 kHz, 10 ms RMS hops, 3-hop smoothing, threshold
**-40 dBFS absolute** over runs of ≥0.22 s. An adaptive threshold (speech minus
35 dB) collapsed to -56 dBFS on an 840 s narrated video and found 8 gaps in the
whole file — continuous narration over room tone never drops 35 dB below its own
95th percentile.

## Export

1080x1920, 60 fps, H.264 `-crf 15 -preset veryfast -pix_fmt yuv420p`, AAC 192k
in pass 1 and `-c:a copy` in pass 2, `-movflags +faststart`. A 23 s short lands
around 65 MB.

Verify the delivered file with `ffprobe` **and** the look sheet — and if you
change the renderer, re-render a known job and compare the stage-1 checksum with
the delivered one; byte-identical output is how you know a refactor changed
nothing.
