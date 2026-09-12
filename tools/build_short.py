# -*- coding: utf-8 -*-
"""Render a vertical short from a fragment of a long video.

    python build_short.py --src LONG.mp4 --srt caps.srt --t0 146.4 --dur 23.3 \
                          --out short.mp4
    python build_short.py ... --stage1-only    # composition, no captions
    python build_short.py ... --caps-only      # captions onto an existing stage1

WHAT IT RENDERS
    A vertical canvas holding the fragment as a uniform-scaled centre crop
    above the middle, the same fragment cover-scaled to the full frame behind
    it, blurred and multiplied down, and animated captions burned in below.

WHY TWO PASSES
    Per-frame ffmpeg caption graphs (geq/drawtext) are unusably slow, and an
    overlay of forty PNG inputs is slower still.  Pass 1 renders the
    composition to stage1.mp4; pass 2 reads ONE raw RGBA strip off a pipe from
    a Pillow producer and does ONE overlay.  The strip is canvas-wide but only
    a few hundred pixels tall, so the whole pipe is a couple of gigabytes and
    a full render is about fifteen seconds.

GEOMETRY lives in presets/*.json, not in this file -- see shortlib.load_layout
and "Deriving a look" in the README.  Nothing about the source video is
hard-coded: its dimensions are probed, and the fragment window's aspect
decides the crop.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shortlib as sl                                                    # noqa: E402


# ---------------------------------------------------------------------- captions

def clean(t):
    """lower case, and no punctuation except the apostrophe in you're / that's.

    The typographic apostrophe is folded to the ASCII one FIRST, otherwise the
    strip below deletes it along with the commas and full stops.
    """
    t = t.replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def chunk_text(text, limit, min_chars=12):
    """Split into lines of at most ``limit`` characters, by dynamic programming.

    Greedy alone leaves a stub: "...morning hanging" + "out".  Nudging words
    between neighbouring lines only moves the stub somewhere else -- on a last
    cue it turned "of thing" into a two-character line "of".  A DP over the
    whole cue charges a short line quadratically, so stubs cannot form in the
    first place; the small per-line charge stops it splitting just to dodge a
    penalty.
    """
    ws = text.split()
    n = len(ws)
    if not n:
        return []
    INF = float("inf")
    best, nxt = [INF] * (n + 1), [n] * (n + 1)
    best[n] = 0.0
    for i in range(n - 1, -1, -1):
        L = 0
        for j in range(i, n):
            L += len(ws[j]) + (1 if j > i else 0)
            if L > limit:
                break
            pen = 1.0 + (0.0 if L >= min_chars else (min_chars - L) ** 2)
            if pen + best[j + 1] < best[i]:
                best[i], nxt[i] = pen + best[j + 1], j + 1
    out, i = [], 0
    while i < n:
        out.append(" ".join(ws[i:nxt[i]]))
        i = nxt[i]
    return out


def make_phrase_image(text, font, cap):
    """white text + black outline, over two layered soft black shadows."""
    ss, font_px, pad = cap["ss"], cap["font_px"], cap["pad"]
    p = int(round(pad * ss))
    wpx = int(font.getlength(text)) + 2 * p
    hpx = int(font_px * ss * 1.35) + 2 * p
    base = Image.new("RGBA", (wpx, hpx), (0, 0, 0, 0))
    ImageDraw.Draw(base).text(
        (wpx // 2, p), text, font=font, anchor="ma",
        fill=(255, 255, 255, 255),
        stroke_width=max(1, int(round(cap["stroke_px"] * ss))),
        stroke_fill=(0, 0, 0, 255))

    a = base.getchannel("A")
    out = Image.new("RGBA", (wpx, hpx), (0, 0, 0, 0))
    for blur_px, off_px, gain in cap["shadows"]:
        sh = a.filter(ImageFilter.GaussianBlur(int(round(blur_px * ss)))).point(
            lambda v, g=gain: int(v * g))
        blk = [Image.new("L", (wpx, hpx), 0) for _ in range(3)]
        out.alpha_composite(Image.merge("RGBA", blk + [sh]),
                            (0, int(round(off_px * ss))))
    out.alpha_composite(base, (0, 0))

    # Pillow's "ascender" anchor sits above the real ink, and the blur sets the
    # image top above that again.  Crop to the alpha bbox and report how far
    # down the ink actually starts, so placement is measured, not assumed --
    # otherwise the caption lands low, which is a defect you end up chasing by
    # eye across several renders.
    ink_top = base.getchannel("A").getbbox()[1]
    bb = out.getchannel("A").getbbox()
    return out.crop(bb), ink_top - bb[1]


def build_phrases(ctx):
    """-> [(text, start_s, end_s, PIL image, ink_offset)] for the whole fragment."""
    lay, cap = ctx["lay"], ctx["lay"]["captions"]
    t0, dur, shift = ctx["t0"], ctx["dur"], ctx["cap_shift"]
    t_end = t0 + dur
    cues = [(a + shift, b + shift, t) for a, b, t in sl.read_srt(ctx["srt"])]
    font = ImageFont.truetype(ctx["font"], int(round(cap["font_px"] * cap["ss"])))

    flat = []                                   # [text, start, end]
    for s, e, text in cues:
        # A cue that STARTS before the fragment does is one we cut into: its
        # first words are not on screen.  DROP it rather than clipping it to
        # the fragment start, which produced 0.02 s stubs of the previous
        # sentence on frame 0.
        if not text or e < t0 or s < t0 - 0.001 or s > t_end:
            continue
        e = min(e, t_end)
        parts = chunk_text(clean(text), cap["chars"])
        # apportion the cue's span by characters, so a line appears about when
        # it is spoken rather than all at once at the cue start.
        lens = [len(p) for p in parts]
        tot = float(sum(lens)) or 1.0
        acc = 0
        for p, L in zip(parts, lens):
            flat.append([p, s + (acc / tot) * (e - s), None])
            acc += L

    if not flat:
        sl.die("no captions fall inside %.2f..%.2f s.\n"
               "  Check --t0/--dur, and the SRT offset from pick_fragment.py."
               % (t0, t_end))

    # A lone short line once got a 0.23 s window ("you") -- a flash you cannot
    # read.  MIN_HOLD is a floor on the shortest window, NOT a uniform cadence:
    # set too high, every line comes out exactly MIN_HOLD long, the track
    # becomes a rigid grid, it drifts off the narration and pushes the first
    # line before the fragment even starts.
    #
    # So the floor is only valid when it is FEASIBLE: n_lines * floor <= DUR.
    # Rather than let a too-high floor silently win over the sync, say so.
    hold = cap["min_hold"]
    need = len(flat) * hold
    if need > dur:
        print("  !! min_hold %.2f x %d lines = %.2f s, but only %.2f s of "
              "fragment.\n     The floor will beat the sync and the captions "
              "will drift off the narration.\n     Lower --min-hold to about "
              "%.2f, or use a longer fragment."
              % (hold, len(flat), need, dur, dur / float(len(flat))))

    for i in range(1, len(flat)):
        flat[i][1] = max(flat[i][1], flat[i - 1][1] + hold)
    if flat[-1][1] + hold > t_end:
        flat[-1][1] = t_end - hold
        for i in range(len(flat) - 2, -1, -1):
            flat[i][1] = max(t0, min(flat[i][1], flat[i + 1][1] - hold))

    for i in range(len(flat) - 1):
        flat[i][2] = flat[i + 1][1]
    flat[-1][2] = t_end

    made = [make_phrase_image(p, font, cap) for p, _, _ in flat]
    ss = cap["ss"]
    out = []
    for (p, s, e), (im, ink) in zip(flat, made):
        if ss > 1:
            im = im.resize((max(1, im.width // ss), max(1, im.height // ss)),
                           Image.LANCZOS)
        out.append((p, s, e, im, ink // ss))
    return out


def lut(k):
    return [min(255, int(v * k)) for v in range(256)]


def emit_captions(proc, ctx):
    """Produce the RGBA strip for every frame and write it down the pipe."""
    lay = ctx["lay"]
    cap = lay["captions"]
    W = lay["canvas"][0]
    nf = ctx["nf"]
    phrases = build_phrases(ctx)
    lens = [len(p) for p, _, _, _, _ in phrases]
    print("captions: %d lines, %d-%d chars, median %d"
          % (len(lens), min(lens), max(lens), sorted(lens)[len(lens) // 2]))
    over = [p for p in lens if p > cap["chars"]]
    if over:
        print("  !! %d line(s) over the %d-char limit" % (len(over), cap["chars"]))

    luts = {}

    def place(strip, im, x, y, k):
        if k <= 0.004:
            return
        if k >= 0.996:
            strip.alpha_composite(im, (x, y))
            return
        key = max(1, int(round(k * 64)))
        if key not in luts:
            luts[key] = lut(key / 64.0)
        sc = im.copy()
        sc.putalpha(im.getchannel("A").point(luts[key]))
        strip.alpha_composite(sc, (x, y))

    blank = Image.new("RGBA", (W, cap["strip_h"]), (0, 0, 0, 0))
    for f in range(nf):
        t = ctx["t0"] + f / float(lay["fps"])
        strip = blank.copy()
        for text, s, e, im, ink in phrases:
            if t < s or t > e:
                continue
            k = min(min(1.0, (t - s) / cap["fade"]), min(1.0, (e - t) / cap["fade"]))
            u = min(1.0, (t - s) / cap["rise_t"])
            dy = int(round(cap["rise"] * (1.0 - (1.0 - (1.0 - u) ** 2))))
            place(strip, im, int(cap["mid"]) - im.width // 2,
                  int(cap["top"]) - ink + dy, k)
        proc.stdin.write(strip.tobytes())
    proc.stdin.close()


# ------------------------------------------------------------------------ render

def stage1(ctx):
    """Compose background + fragment window into stage1.mp4."""
    lay, src, out = ctx["lay"], ctx["src"], ctx["stage1"]
    W, H = lay["canvas"]
    fps, nf = lay["fps"], ctx["nf"]
    mx, my, mw, mh = sl.main_box(lay)
    cx, cy, cw, ch = sl.source_crop(lay, ctx["src_w"], ctx["src_h"])
    cov_w, cov_h = sl.cover_size(lay, ctx["src_w"], ctx["src_h"])
    dark, blur = lay["bg"]["darken"], lay["bg"]["blur"]
    small_w, small_h = W // 4, H // 4

    # The darkening is a black layer at `darken` opacity, i.e. a MULTIPLY:
    # out = in * darken, written colorchannelmixer.  `eq=brightness=-0.16`
    # looks equivalent and is SUBTRACTIVE -- 41 levels off every channel -- and
    # on night scenes sitting at 20-50 it crushed the whole background to a
    # measured 0-8, i.e. pure black.  When the background looks too dark, this
    # is the bug to check: measure the output's mean against the source, it
    # should come out at roughly `darken`.
    vf = (
        "[0:v]split=2[a][b];"
        "[a]scale=%d:%d,crop=%d:%d:%d:0,scale=%d:%d,gblur=sigma=%g,"
        "colorchannelmixer=rr=%g:gg=%g:bb=%g,scale=%d:%d:flags=bicubic,setsar=1[bg];"
        "[b]crop=%d:%d:%d:%d,scale=%d:%d,setsar=1[mn];"
        "[bg][mn]overlay=%d:%d:format=auto,format=yuv420p[out]"
        % (cov_w, cov_h, W, cov_h, (cov_w - W) // 2, small_w, small_h, blur,
           dark, dark, dark, W, H,
           cw, ch, cx, cy, mw, mh, mx, my))
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats",
           "-ss", "%.3f" % ctx["t0"], "-t", "%.3f" % ctx["dur"], "-i", src,
           "-filter_complex", vf, "-map", "[out]", "-map", "0:a",
           "-r", str(fps), "-fps_mode", "cfr", "-frames:v", str(nf),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "15",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", out]
    print("pass 1/2  composition -> %s" % out)
    subprocess.run(cmd, check=True)


def caps(ctx):
    """Overlay the caption strip onto stage1 and write the deliverable."""
    lay = ctx["lay"]
    W = lay["canvas"][0]
    cap = lay["captions"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats",
           "-i", ctx["stage1"],
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (W, cap["strip_h"]),
           "-r", str(lay["fps"]), "-i", "-",
           "-filter_complex",
           "[0:v][1:v]overlay=0:%d:format=auto,format=yuv420p[out]" % cap["strip_y"],
           "-map", "[out]", "-map", "0:a",
           "-frames:v", str(ctx["nf"]), "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "15", "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", ctx["out"]]
    print("pass 2/2  captions -> %s" % ctx["out"])
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        emit_captions(p, ctx)
    except BrokenPipeError:
        pass                                  # ffmpeg died; its stderr says why
    if p.wait() != 0:
        raise SystemExit("pass 2 failed -- see the ffmpeg output above")


# -------------------------------------------------------------------------- main

def parse():
    ap = argparse.ArgumentParser(
        description="Render a vertical short from a fragment of a long video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Geometry comes from presets/*.json; see the README.")
    ap.add_argument("--src", required=True, help="the long video")
    ap.add_argument("--out", required=True, help="the MP4 to write")
    ap.add_argument("--t0", type=float, required=True,
                    help="fragment start, seconds")
    ap.add_argument("--dur", type=float, required=True,
                    help="fragment length, seconds")
    ap.add_argument("--srt", help="subtitles; required unless --stage1-only")
    ap.add_argument("--preset", help="layout preset file, or a name in presets/")
    ap.add_argument("--canvas", help="canvas WxH; must match the preset's aspect")
    ap.add_argument("--font", help="path to a .ttf/.otf display face")
    ap.add_argument("--cap-shift", type=float, default=0.0, dest="cap_shift",
                    help="seconds the SRT runs ahead of the audio (see "
                         "pick_fragment.py)")
    ap.add_argument("--chars", type=int, help="max characters per caption line")
    ap.add_argument("--font-px", type=int, dest="font_px", help="caption size")
    ap.add_argument("--min-hold", type=float, dest="min_hold",
                    help="minimum seconds a caption line stays on screen")
    ap.add_argument("--job", help="working directory for stage1.mp4 "
                                  "[default: a temp dir]")
    ap.add_argument("--stage1-only", action="store_true",
                    help="composition only, no captions")
    ap.add_argument("--caps-only", action="store_true",
                    help="burn captions onto an existing --job/stage1.mp4")
    return ap.parse_args()


def main():
    a = parse()
    if not os.path.exists(a.src):
        sl.die("source not found: %s" % a.src)
    if not a.stage1_only and not a.srt:
        sl.die("--srt is required (or use --stage1-only)")

    lay = sl.load_layout(a.preset, a.canvas)
    cap = lay["captions"]
    if a.chars:
        cap["chars"] = a.chars
    if a.font_px:
        cap["font_px"] = a.font_px
    if a.min_hold is not None:
        cap["min_hold"] = a.min_hold

    src_w, src_h, src_fps, src_dur = sl.probe(a.src)
    if a.t0 + a.dur > src_dur > 0:
        print("  note: %.2f..%.2f s runs past the end of the source (%.2f s)"
              % (a.t0, a.t0 + a.dur, src_dur))

    job = a.job or os.path.join(tempfile.gettempdir(), "long-to-short")
    os.makedirs(job, exist_ok=True)

    ctx = {
        "lay": lay, "src": a.src, "srt": a.srt, "out": a.out, "job": job,
        "t0": a.t0, "dur": a.dur, "cap_shift": a.cap_shift,
        "src_w": src_w, "src_h": src_h,
        "stage1": os.path.join(job, "stage1.mp4"),
        "nf": int(round(a.dur * lay["fps"])),
        "font": None,
    }

    W, H = lay["canvas"]
    mx, my, mw, mh = sl.main_box(lay)
    cx, cy, cw, ch = sl.source_crop(lay, src_w, src_h)
    print("source   %dx%d @ %.3f fps, %.1f s" % (src_w, src_h, src_fps, src_dur))
    print("layout   %s  %dx%d @ %d fps" % (lay["name"], W, H, lay["fps"]))
    print("window   %dx%d at (%d,%d)  <- source crop %dx%d at (%d,%d)"
          % (mw, mh, mx, my, cw, ch, cx, cy))
    print("fragment %.2f .. %.2f s  (%.2f s, %d frames)"
          % (a.t0, a.t0 + a.dur, a.dur, ctx["nf"]))

    if not a.stage1_only:
        # Resolved before the render, not after: a missing font should cost a
        # second, not the thirty-five seconds pass 1 takes.
        ctx["font"] = sl.resolve_font(a.font or lay.get("font"))

    if not a.caps_only:
        stage1(ctx)
    if not a.stage1_only:
        caps(ctx)
        print("-> %s  (%.1f MB)" % (a.out, os.path.getsize(a.out) / 1e6))


if __name__ == "__main__":
    sl.setup_stdout()
    main()
