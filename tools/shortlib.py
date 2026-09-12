# -*- coding: utf-8 -*-
"""Shared helpers for the long-to-short tools.

Three things every tool in this kit needs and none of them should re-implement:

  * ``read_srt``      -- SRT parsing that survives BOMs, CRLF and stray markup.
  * ``resolve_font``  -- find a heavy display face without hard-coding one
                         machine's paths.
  * ``load_layout``   -- the canvas geometry, as a preset file plus a canvas
                         scale, so a look measured on one canvas transfers to
                         another instead of being baked into a script.

Nothing here knows about any particular video.  That is the point.
"""
import json
import os
import re
import sys

# --------------------------------------------------------------------------- io


def setup_stdout():
    """Make ``print`` survive a cp1251/cp866 console.

    A ``print`` of a Cyrillic or accented caption line dies with
    ``UnicodeEncodeError`` on a legacy Windows code page, which looks like a
    crash in the tool rather than a console encoding problem.
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def die(msg):
    raise SystemExit("error: %s" % msg)


# -------------------------------------------------------------------------- srt

_SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def read_srt(path):
    """-> [(start_s, end_s, text)], in file order.

    Tolerant on purpose: subtitle files arrive from a dozen tools and carry
    UTF-8 BOMs, CRLF endings, ``.`` instead of ``,`` in the millisecond field,
    and occasional HTML markup in the body.
    """
    if not os.path.exists(path):
        die("subtitle file not found: %s" % path)
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        txt = f.read()
    cues = []
    for block in re.split(r"\n\s*\n", txt.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        m = _SRT_TIME.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        time_line = next(i for i, line in enumerate(lines) if _SRT_TIME.search(line))
        body = lines[time_line + 1:]
        cues.append((a, b, clean_markup(" ".join(x.strip() for x in body))))
    return cues


def clean_markup(t):
    """Drop the inline tags some subtitle editors leave in the body."""
    t = re.sub(r"<[^>]{1,20}>", "", t)
    t = t.replace("{\\an8}", "").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t).strip()


def read_srt_text(path):
    """The whole subtitle file as one flat string, for echo checks."""
    return " ".join(t for _, _, t in read_srt(path) if t)


def fmt_ts(t):
    """Seconds -> ``HH:MM:SS,mmm``.  Negative clamps to zero."""
    ms = int(round(max(0.0, t) * 1000.0))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(cues, path):
    """Write ``[(start, end, text)]`` as an SRT.

    Round-tripping through :func:`read_srt` is the only guarantee that matters:
    every other tool in the kit reads what this writes, and a subtitle file is
    a format everybody believes they can write until a comma-decimal locale
    turns ``00:00:01,500`` into ``00:00:01.5``.
    """
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for i, (a, b, t) in enumerate(cues, 1):
            f.write("%d\n%s --> %s\n%s\n\n"
                    % (i, fmt_ts(a), fmt_ts(b), " ".join(str(t).split())))


# ------------------------------------------------------------------------- words

STOP = set("""a an the of to and or but is are was were be been am do does did
you your i it its that this these those in on at for with as so if not no just
like what which who how when there they them he she his her we our me my""".split())


def words(s):
    """lower-case word list, punctuation stripped, apostrophes kept."""
    s = s.lower().replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    return re.sub(r"[^\w\s']", " ", s).split()


def content(ws):
    """The words that carry meaning -- stopwords dropped."""
    return [w for w in ws if w not in STOP]


# -------------------------------------------------------------------------- font

# Font directories, most specific first.  A "user" font installed by
# double-clicking lands in the per-user directory on Windows and in
# ~/Library/Fonts on macOS, NOT in the system directory -- looking only at
# C:\Windows\Fonts silently misses it.
FONT_DIRS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.expanduser("~/Library/Fonts"),
    "/Library/Fonts",
    "/System/Library/Fonts",
    os.path.expanduser("~/.local/share/fonts"),
    os.path.expanduser("~/.fonts"),
    "/usr/local/share/fonts",
    "/usr/share/fonts",
]

# Tried in order.  Shorts captions want a heavy, tight, geometric face; these
# are the ones that ship with, or are commonly installed on, the three
# platforms.  Add your own at the front via --font or the preset's "font" key.
FONT_CANDIDATES = [
    "creatodisplay-black.otf",
    "creatodisplay-black.ttf",
    "anton-regular.ttf", "anton.ttf",
    "arial black.ttf", "ariblk.ttf",
    "impact.ttf",
    "archivoblack-regular.otf", "archivoblack.ttf",
    "montserrat-black.ttf", "montserratextra-black.ttf",
    "roboto-black.ttf",
    "inter-black.ttf", "inter_18pt-black.ttf",
    "notosans-black.ttf", "notosans-black.ttc",
    "dejavusans-bold.ttf",
    "liberationsans-bold.ttf",
    "freesansbold.ttf",
    "arial bold.ttf", "arialbd.ttf",
    "helvetica.ttc", "helveticaneue.ttc",
]


def _walk_fonts(d):
    """-> {lowercased basename: full path}, two levels deep."""
    out = {}
    if not os.path.isdir(d):
        return out
    for root, dirs, files in os.walk(d):
        depth = root[len(d):].count(os.sep)
        if depth >= 3:
            dirs[:] = []
        for f in files:
            if f.lower().endswith((".ttf", ".otf", ".ttc", ".ttf.ttc")):
                out.setdefault(f.lower(), os.path.join(root, f))
    return out


def resolve_font(spec=None):
    """Find a display font.  -> absolute path.

    Order: an explicit path, then ``$LONG_TO_SHORT_FONT``, then the candidate
    list above searched across every platform's font directories.  Raises with
    the searched names rather than silently substituting a default -- a
    render that quietly uses the wrong typeface is a render you have to redo.
    """
    spec = spec or os.environ.get("LONG_TO_SHORT_FONT")
    if spec:
        spec = os.path.expandvars(os.path.expanduser(spec))
        if not os.path.exists(spec):
            die("font not found: %s" % spec)
        return spec

    found = {}
    for d in FONT_DIRS:
        for name, path in _walk_fonts(d).items():
            found.setdefault(name, path)
    for cand in FONT_CANDIDATES:
        if cand in found:
            return found[cand]

    die("no display font found.\n"
        "  Pass --font /path/to/Font-Black.otf, or set LONG_TO_SHORT_FONT.\n"
        "  Looked for: %s\n"
        "  In: %s"
        % (", ".join(FONT_CANDIDATES[:6]) + ", ...",
           "; ".join(d for d in FONT_DIRS if os.path.isdir(d))))


# ------------------------------------------------------------------------ layout

# The shipped look: a vertical short whose fragment sits above the middle over
# a blurred, darkened full-frame copy of itself, with animated captions below.
#
# These pixel values were measured off a reference screenshot, not invented --
# see "Deriving a look" in the README for the one-factor method.  Everything
# here is a plain number so a preset can override any of it.
DEFAULT_LAYOUT = {
    "name": "default",
    "canvas": [1080, 1920],
    "fps": 60,
    # The fragment window.  Its aspect decides the source crop: the source is
    # centre-cropped to this aspect and scaled to exactly this box.
    "main": {"x": 12, "y": 238, "w": 1056, "h": 960},
    # The full-frame copy behind it, blurred and multiplied down.
    "bg": {"blur": 6.0, "darken": 0.5},
    "captions": {
        # The strip is the only region pass 2 overlays; keeping it 1080x300
        # instead of a full frame is what makes the overlay cost ~15 s.
        "strip_y": 1300,
        "strip_h": 300,
        "top": 60,          # cap-line top, measured INSIDE the strip
        "mid": 540,         # text centre x
        "chars": 15,        # max characters per line
        "font_px": 80,
        "stroke_px": 5.5,
        "rise": 20.0,       # px a line rises while fading in
        "fade": 0.10,       # s, alpha ramp at each end
        "rise_t": 0.18,     # s, duration of the rise
        "min_hold": 0.70,   # s floor on the shortest line window (see README)
        "ss": 2,            # supersample factor for the glyph render
        "pad": 30,          # transparent margin around the ink, before ss
        "shadows": [[16, 14, 0.55], [5, 6, 0.75]],
    },
    "font": None,           # None -> resolve_font()
}

# Presets are allowed to differ from the default canvas only by a uniform
# scale.  A non-uniform one would stretch the fragment, and "do not stretch
# anything" is the rule the whole layout rests on -- so it is an error, not a
# silent per-axis resize.
ASPECT_TOLERANCE = 0.02


def _default_preset_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, "presets", "default.json")


def load_layout(preset=None, canvas=None):
    """-> a layout dict, scaled to ``canvas`` if one is given.

    ``preset`` may be a path to a JSON file, the name of a file in
    ``presets/``, or None for the built-in default.  Any key present in the
    file is merged over the default, so a preset can be a two-line file that
    only changes ``captions.font_px``.
    """
    lay = json.loads(json.dumps(DEFAULT_LAYOUT))          # deep copy
    path = preset
    if path and not os.path.exists(path):
        cand = os.path.join(os.path.dirname(_default_preset_path()), path)
        if not os.path.exists(cand) and not cand.endswith(".json"):
            cand += ".json"
        if os.path.exists(cand):
            path = cand
    if path:
        if not os.path.exists(path):
            die("preset not found: %s" % preset)
        loaded = json.load(open(path, encoding="utf-8"))
        _merge(lay, loaded)
        lay["name"] = loaded.get("name") or os.path.splitext(
            os.path.basename(path))[0]

    if canvas:
        lay = scale_layout(lay, canvas)
    return lay


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def parse_canvas(s):
    m = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", str(s))
    if not m:
        die("--canvas wants WxH, e.g. 1080x1920 (got %r)" % s)
    return int(m.group(1)), int(m.group(2))


def scale_layout(lay, canvas):
    """Scale every pixel value in a layout to a new canvas.

    Uniform, and only uniform: a preset authored at 1080x1920 taken to a
    1080x1080 canvas would have to squash the fragment, which is exactly the
    defect the layout rules forbid.  Different aspect -> author a preset.
    """
    w, h = parse_canvas(canvas) if not isinstance(canvas, (list, tuple)) else canvas
    lw, lh = lay["canvas"]
    if (w, h) == (lw, lh):
        return lay
    s = w / float(lw)
    if abs(h / float(lh) - s) > ASPECT_TOLERANCE:
        die("canvas %dx%d has a different aspect from the preset's %dx%d.\n"
            "  Scaling uniformly would letterbox it; scaling per-axis would\n"
            "  stretch the fragment.  Author a preset for %dx%d instead."
            % (w, h, lw, lh, w, h))

    def sc(d, keys):
        for k in keys:
            if k in d:
                d[k] = int(round(d[k] * s)) if isinstance(d[k], int) else d[k] * s
    sc(lay["main"], ("x", "y", "w", "h"))
    sc(lay["bg"], ("blur",))
    c = lay["captions"]
    sc(c, ("strip_y", "strip_h", "top", "mid", "font_px", "rise",
           "pad", "stroke_px"))
    c["shadows"] = [[int(round(b * s)), int(round(o * s)), g]
                    for b, o, g in c["shadows"]]
    lay["canvas"] = [w, h]
    return lay


def main_box(lay):
    """(x, y, w, h) of the fragment window, forced even.

    Odd dimensions are legal for x264 but sit badly on the yuv420p chroma
    grid, and an odd cover dimension shifts the background crop by a pixel.
    """
    m = lay["main"]
    return tuple(int(round(m[k])) // 2 * 2 for k in ("x", "y", "w", "h"))


def source_crop(lay, src_w, src_h):
    """-> (x, y, w, h): the centre crop of the source that fills the window.

    Derived, never configured.  The window's aspect decides it, and the crop
    is always centred, so a 16:9 source into a 1.1 window crops the sides --
    it is NOT a 16:9 fit, which would leave bars.  Working it out from the
    window instead of hard-coding one source's numbers is what lets the same
    preset take a 4:3 or a vertical source.
    """
    _, _, mw, mh = main_box(lay)
    target = mw / float(mh)
    if src_w / float(src_h) > target:          # source wider -> crop the sides
        cw = int(round(src_h * target / 2.0)) * 2
        ch = src_h
    else:                                       # source taller -> crop top/bottom
        cw = src_w
        ch = int(round(src_w / target / 2.0)) * 2
    cw, ch = min(cw, src_w), min(ch, src_h)
    return (src_w - cw) // 2, (src_h - ch) // 2, cw, ch


def cover_size(lay, src_w, src_h):
    """-> (w, h) that covers the canvas from the SOURCE'S OWN aspect.

    Never a hard-coded 16:9: that would stretch any other source.  Rounded to
    an even number, because 1920 * 1920/1080 is 3413.33 and taking it as 3413
    instead of 3414 shifts the crop by a pixel and changes every background
    pixel.
    """
    w, h = lay["canvas"]
    if src_w * h >= src_h * w:
        ch = h
        cw = int(round(h * src_w / float(src_h) / 2.0)) * 2
    else:
        cw = w
        ch = int(round(w * src_h / float(src_w) / 2.0)) * 2
    return cw, ch


def probe(path):
    """-> (width, height, fps, duration_s) of a media file, via ffprobe."""
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        die("ffprobe could not read %s\n%s" % (path, out.stderr.strip()))
    vals = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    if len(vals) < 3:
        die("ffprobe returned no video stream for %s" % path)
    w, h = int(vals[0]), int(vals[1])
    num, _, den = vals[2].partition("/")
    fps = float(num) / float(den) if den else float(num)
    dur = float(vals[3]) if len(vals) > 3 and vals[3] != "N/A" else 0.0
    return w, h, fps, dur
