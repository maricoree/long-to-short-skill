# -*- coding: utf-8 -*-
"""Make the captions.srt the rest of the kit needs, out of the video itself.

    python transcribe.py LONG.mp4                    -> LONG.srt
    python transcribe.py LONG.mp4 --model medium
    python transcribe.py LONG.mp4 --lang ru --out caps.srt
    python transcribe.py LONG.mp4 --list-streams
    python transcribe.py LONG.mp4 --stream 3          # force one embedded track

Most people pointing at a long video do not have a subtitle file, and telling
them to go and make one by hand is not an answer.  There are three ways a video
can already carry its words, and they are tried cheapest first:

  1. AN EMBEDDED TEXT SUBTITLE STREAM.  Exact timings, no model, about two
     seconds.  A downloaded .mkv usually has one, sometimes in three languages.
     Using it beats re-deriving it -- but it is said out loud, never done
     quietly, because "where did these captions come from" has to have an
     answer.
  2. faster-whisper.  ``pip install faster-whisper``; CTranslate2, no torch,
     int8 on CPU.  This is the default choice.
  3. openai-whisper.  The reference implementation.  Needs torch, so ~2 GB, but
     it is what people already have if they have anything.

WHY A TRANSCRIPT FROM HERE IS NOT LIKE ONE YOU WERE GIVEN.  A human-authored
subtitle file runs ahead of the speaker by a constant -- that is what "the
captions run fast" almost always turns out to be -- and the kit spends its
first step measuring that offset.  A file made HERE is timed off this video's
own audio, so there is nothing to correct.  Expect ``pick_fragment.py`` to
report about 0.00 and leave ``--cap-shift`` alone.  A weak offset reading on a
self-made transcript is not a bug and not a discovery; it is the expected
answer.

HALLUCINATION IS THE ONE REAL HAZARD.  Whisper invents text where there is no
speech -- music, room tone, silence -- and worse, it loops: one phrase repeated
for a minute, or a subtitle credit it half-remembers from its training data.
Burned into a caption track that is a visible defect on every frame it covers.
Four filters, all reported rather than silent:

  * the model's own ``no_speech_prob`` above :data:`NO_SPEECH`;
  * its own ``avg_logprob`` below :data:`LOGPROB` (set low on purpose -- -1.0
    is an ordinary "unsure" score on real quiet speech, and cutting there
    deletes narration);
  * a whole cue matching :data:`BOILER` -- "thanks for watching", "subtitle
    editor ...".  Matched against the ENTIRE cue, so a video that legitimately
    says the word "subscribe" survives;
  * immediate self-repetition, and a cue that is one word four or more times.

A filter that drops things silently is a filter nobody can debug, so every
drop is printed with its time and its reason.  ``--keep-hallucinations`` turns
all four off, for looking at what was thrown away.

The backends are optional on purpose: this is the only tool in the kit that
needs a package beyond numpy and Pillow, and it is only needed when the source
has no subtitles at all.  Nothing here uploads anything -- a 1 h video is CPU
work on your own machine, not an API call.
"""
import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shortlib as sl                                                    # noqa: E402

# huggingface_hub prints nine lines about symlinks on Windows that read like a
# failure and are not one -- the download works, just less efficiently.  Set
# before the import that triggers it, which is inside load_backend().
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

SR = 16000

# Subtitle codecs ffmpeg can turn into SRT.  What is NOT here matters as much:
# hdmv_pgs_subtitle, dvd_subtitle and dvb_subtitle are bitmaps, and the only
# way to get text out of them is OCR, which is a different project.
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text",
             "subviewer", "microdvd", "eia_608", "hdmv_text_subtitle"}

NO_SPEECH = 0.85        # drop a segment the model is this sure has no speech
LOGPROB = -1.50         # ...or this unsure of.  Deliberately far below -1.0.
BOILER = [
    r"продолжение следует",
    r"субтитры.{0,60}",
    r"редактор субтитров.{0,60}",
    r"корректор.{0,60}",
    r"(перевод|озвучка).{0,40}(студи|канал|man|men).{0,60}",
    r"thanks?( you)? for watching.{0,60}",
    r"(please )?(like and )?subscribe.{0,60}",
    r"subtitles? (by|made by|created by).{0,60}",
    r"(transcribed|transcription) by.{0,60}",
    r"amara\.org.{0,60}",
    r"(subtitles?|captions?) provided by.{0,60}",
]
END_PUNCT = ".!?…。！？"

# Roughly how much slower than `small` a model is on the same machine, and how
# many minutes `small` int8 needs per minute of audio on a CPU.  Both are used
# only to answer "how long is this going to take" before a long silent wait --
# and answered as a RANGE, because the spread across CPUs is larger than the
# spread across models and a confident single number would be a lie.
MODEL_COST = {"tiny": 1.0, "tiny.en": 1.0, "base": 1.25, "base.en": 1.25,
              "small": 1.0, "small.en": 1.0, "medium": 3.0, "medium.en": 3.0,
              "large": 8.0, "large-v2": 8.0, "large-v3": 8.0, "turbo": 2.0}

# `tiny` at 1.0 and `base` at 1.25 look like typos and are the opposite: they
# are the measurement.  The sizes below were assumed from parameter counts for
# a long time and parameter counts do not predict this at all.  Measured on a
# 6-core desktop CPU, int8, beam 5, same audio, twice, in both orders:
#
#     20 s    tiny <1 s   base  4 s   small <1 s
#     60 s    tiny  8 s   base 16 s   small 10 s
#     397 s   tiny 71 s   base 90 s   small 72 s
#
# `base` is the SLOWEST of the three at every length -- a weaker decoder
# rambles further before it emits EOT, so beam search runs longer, and it loses
# more than its smaller encoder saves.  `tiny` is not faster than `small`
# either.  So "use a smaller model to save time" is simply false here, and the
# tiers that exist are about download size and accuracy, not about speed.

# Measured on one desktop CPU (small/int8: 9.0 s for 20.2 s of audio = 0.45
# min per min; tiny: 30 s for 397 s = 0.08, which is the 0.2 ratio above
# holding up).  The first draft of this table said (0.12, 0.35) -- i.e. that
# `small` runs FASTER than tiny -- and duly told a user to expect 1-2 minutes
# for a job that took nine seconds.  An estimate that is 10x out is worse than
# no estimate, because it talks people out of the run they were about to start.
# Wide on purpose: a slow laptop is 2-3x this, and the top of the range is the
# number that stops someone giving up on a long file.
CPU_MIN_PER_MIN = (0.25, 1.10)

# Bytes to fetch the first time a model is used, from the HuggingFace API
# (blobs=true).  Only used to warn about a wait the tool would otherwise be
# silent about -- see weights_ready() for why that silence is the problem.
MODEL_MB = {"tiny": 75, "tiny.en": 75, "base": 141, "base.en": 141,
            "small": 464, "small.en": 464, "medium": 1460, "medium.en": 1460,
            "large": 2948, "large-v2": 2948, "large-v3": 2948, "turbo": 1547}


def hub_cache():
    """-> the directory huggingface_hub keeps models in."""
    d = os.environ.get("HF_HUB_CACHE")
    if d:
        return d
    base = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface")
    return os.path.join(base, "hub")


def hf_ready(model):
    """-> True when the CT2 weights are complete in the HuggingFace cache.

    The test is the ``.bin``, not "the directory has files".  A killed download
    leaves config.json, tokenizer.json and vocabulary.txt sitting in
    ``snapshots/`` -- they are tiny and arrive in a second -- while the weights
    itself is still a zero-byte ``.incomplete`` in ``blobs/``.  Checking for any
    file at all therefore calls a half-fetched model ready, which is the exact
    case this whole function exists to announce.  Measured on a real interrupted
    `medium` fetch: three files present, no ``model.bin``.
    """
    d = hub_cache()
    if not os.path.isdir(d):
        return False
    tail = model.split("/")[-1]
    for name in os.listdir(d):
        if not (name.startswith("models--") and name.endswith("-" + tail)):
            continue
        snap = os.path.join(d, name, "snapshots")
        if not os.path.isdir(snap):
            continue
        for rev in os.listdir(snap):
            for f in os.listdir(os.path.join(snap, rev)):
                if f.endswith(".bin"):
                    try:                       # follows the link into blobs/
                        if os.path.getsize(os.path.join(snap, rev, f)) > 0:
                            return True
                    except OSError:
                        pass
    return False


def openai_ready(model):
    """-> True when openai-whisper's own .pt for this model is already down.

    A different cache in a different place: openai-whisper fetches .pt files
    from its own CDN into ``~/.cache/whisper`` and never touches the HF hub, so
    asking hf_ready() about it would answer about a directory that will stay
    empty forever.  It also means the sizes are somebody else's -- about twice
    the CT2 ones, because they are fp16 -- which is why no number is claimed.
    """
    base = (os.environ.get("WHISPER_CACHE_DIR")
            or os.path.join(os.environ.get("XDG_CACHE_HOME")
                            or os.path.join(os.path.expanduser("~"), ".cache"),
                            "whisper"))
    for ext in (".pt", ".pt.zip"):
        p = os.path.join(base, model + ext)
        try:
            if os.path.getsize(p) > 0:
                return True
        except OSError:
            pass
    return False


def weights_ready(model, backend="faster-whisper"):
    """-> True when this model would not need downloading.

    Worth the trouble because the download is INVISIBLE.  huggingface_hub only
    draws its progress bar on a terminal, and this tool is normally run by an
    agent or piped into a log -- so a first run sits silent for as long as the
    fetch takes (measured: ~1 MB/s unauthenticated, so about eight minutes for
    `small`), and neither the person waiting nor the model driving it can tell
    that apart from a hang.  It was not told apart from a hang here, either.
    """
    return (openai_ready(model) if backend == "openai-whisper"
            else hf_ready(model))


def fetch_note(model, backend="faster-whisper"):
    """-> what the loading line should say about the weights, size included."""
    if weights_ready(model, backend):
        return ""
    mb = MODEL_MB.get(model) if backend == "faster-whisper" else None
    return (" -- downloading %.0f MB once, then cached"
            % mb if mb else " -- downloading the weights once, then cached")


def estimate(seconds, model):
    """-> a one-line guess at how long ``model`` needs on a CPU, as a string.

    Round the TOP of the range up and the bottom down: overestimating a short
    job costs the reader nothing, while underestimating a long one is what
    makes someone decide the thing has hung and kill it.
    """
    mins = seconds / 60.0 * MODEL_COST.get(model, 1.0)
    lo = max(1, int(mins * CPU_MIN_PER_MIN[0]))
    hi = max(1, int(mins * CPU_MIN_PER_MIN[1] + 0.999))
    if hi == 1:
        return "under a minute"
    if lo == hi:
        return "expect roughly %d minutes" % hi
    return "expect roughly %d-%d minutes" % (lo, hi)

LANG_FLOOR = 0.70


def lang_note(prob, requested=None):
    """-> a warning when the detected language looks like a guess, else "".

    Whisper picks the language from the opening window alone, and when it picks
    wrong it does not degrade the transcript -- it REPLACES it.  Measured here
    on 397 s of clean English narration: `base` called it Latvian at p=0.40 and
    `tiny` called it Russian at p=0.69, and both returned Latin/Cyrillic
    transliteration of English sounds, cue after cue, in fluent-looking text.
    Nothing downstream can tell that apart from a real transcript, and
    pick_fragment.py will happily measure a "constant offset" in it.

    The confidence is the only warning there is, and it is printed once, in a
    line that otherwise looks routine.  Below the floor it is not a close call
    -- it is the detector shrugging -- so say so where the reader is already
    looking.  A false alarm costs one sentence; a missed one costs the file.
    """
    if requested or prob is None or prob >= LANG_FLOOR:
        return ""
    return ("  ^ %.2f is the detector guessing, not deciding.  If the text below "
            "is not\n    the language you expected, stop now and re-run with "
            "--lang XX --force." % prob)


NO_BACKEND = """no transcription backend is installed.%s
  pip install faster-whisper      <- recommended: no torch, int8 on CPU

  (openai-whisper works too -- `pip install openai-whisper` -- but it pulls in
  torch, which is about 2 GB.  Only worth it if you already have torch.)

  Or, if the subtitles exist somewhere else, pass them straight in:
      python tools/pick_fragment.py %s captions.srt"""


# ------------------------------------------------------------------- embedded

def sub_streams(path):
    """-> ffprobe's subtitle streams, or [] if the file has none."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
         "stream=index,codec_name:stream_tags=language,title", "-of", "json",
         path], capture_output=True, text=True)
    if out.returncode != 0:
        sl.die("ffprobe could not read %s\n%s" % (path, out.stderr.strip()[:400]))
    try:
        return json.loads(out.stdout).get("streams", [])
    except ValueError:
        return []


def describe(s):
    tags = s.get("tags") or {}
    lang = tags.get("language") or "??"
    if tags.get("title"):
        lang += " (%s)" % tags["title"]
    return "index %-4s %-18s %s" % (s.get("index"), s.get("codec_name"), lang)


def pick_stream(streams, want):
    """-> (stream, why) for the best text subtitle track, or (None, why not).

    A language is never guessed from the video's own language: the track's own
    tag is the only evidence there is.  When several qualify the first is
    taken and named, so the choice is visible and ``--stream`` can override it.
    """
    text = [s for s in streams if s.get("codec_name") in TEXT_SUBS]
    if want is not None:
        hit = [s for s in streams if s.get("index") == want]
        if not hit:
            sl.die("no subtitle stream with index %d (the file has: %s)"
                   % (want, ", ".join(str(s.get("index")) for s in streams) or "none"))
        if hit[0].get("codec_name") not in TEXT_SUBS:
            sl.die("stream %d is %s -- a bitmap subtitle, not text.  ffmpeg can "
                   "copy it but not read it; only OCR could, and that is a "
                   "different project.  Re-run without --stream to transcribe."
                   % (want, hit[0].get("codec_name")))
        return hit[0], "you asked for it"
    if not text:
        if not streams:
            return None, "the file has no subtitle streams"
        return None, ("the file has %d subtitle stream(s), none of them text (%s)"
                      % (len(streams),
                         ", ".join(sorted({s.get("codec_name") or "?" for s in streams}))))
    return text[0], ("the only text track" if len(text) == 1
                     else "the first of %d text tracks" % len(text))


def extract_stream(path, index, out):
    """ffmpeg -map, then prove it by parsing the result back."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map",
                        "0:%d" % index, "-f", "srt", out],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        sl.die("ffmpeg could not extract stream %d:\n%s"
               % (index, r.stderr.strip()[:400]))
    cues = sl.read_srt(out)
    if not cues:
        sl.die("stream %d extracted to %s but parsed back empty -- the track is "
               "probably a bitmap one wearing a text codec's name." % (index, out))
    return cues


# ---------------------------------------------------------------------- audio

def decode(path, sr=SR):
    """-> mono float32 at ``sr``, straight out of ffmpeg.

    No intermediate WAV: an hour is 230 MB of float32, which is nothing next to
    holding a model in memory, and it keeps the only decoder involved the
    ffmpeg the kit already requires.
    """
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
                        "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        sl.die("ffmpeg produced no audio for %s\n  (is there an audio stream?)\n%s"
               % (path, p.stderr.decode("utf-8", "replace").strip()[:400]))
    return np.frombuffer(p.stdout, "<f4")


def hms(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)


# ------------------------------------------------------------------- backends

def load_backend():
    """-> ('faster-whisper'|'openai-whisper', module) or None."""
    try:
        import faster_whisper
        return "faster-whisper", faster_whisper
    except ImportError:
        pass
    try:
        import whisper
        return "openai-whisper", whisper
    except ImportError:
        return None


def resolve_device(dev):
    if dev != "auto":
        return dev
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"


def first_of(gen):
    """-> (the first item or None, a generator that still yields everything)."""
    for s in gen:
        return s, gen
    return None, gen


def open_stream(make_gen, device, compute, what):
    """Start a run, pulling ONE segment inside the try, not just the model.

    ctranslate2 reports a CUDA device whenever the DRIVER has one, which is not
    the same question as "can this model actually run on it" -- the CUDA
    libraries have to be there too, and on Windows they are usually not.  It
    then builds the model happily and dies minutes later, at the first encode,
    with "Library cublas64_12.dll is not found or cannot be loaded".

    So a try around the constructor catches nothing.  This was found the hard
    way: a constructor-only fallback passed its own unit test and still
    crashed on a real RTX 3060.  The first pull is inside the try for exactly
    that reason, and there is a test for it.

    Both run_* helpers are generators, so nothing in them runs until the first
    pull -- which means this one call covers construction and first inference.
    """
    try:
        gen = make_gen(device, compute)
        first, gen = first_of(gen)
        return first, gen, device, compute
    except Exception as e:
        if device == "cpu":
            raise
        print("  %s would not run on %s -- falling back to cpu/int8\n"
              "    (%s: %s)"
              % (what, device, type(e).__name__,
                 str(e).splitlines()[0][:140] if str(e) else ""))
        sys.stdout.flush()
        gen = make_gen("cpu", "int8")
        first, gen = first_of(gen)
        return first, gen, "cpu", "int8"


def run_faster(m, pcm, model, lang, device, compute):
    print("  loading %s (%s/%s)%s"
          % (model, device, compute, fetch_note(model)))
    sys.stdout.flush()
    wm = m.WhisperModel(model, device=device, compute_type=compute)
    segs, info = wm.transcribe(
        pcm, language=lang, beam_size=5, word_timestamps=True,
        condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500})
    print("  language %s (%.2f)" % (info.language, info.language_probability))
    note = lang_note(info.language_probability, lang)
    if note:
        print(note)
    sys.stdout.flush()
    for s in segs:
        yield {"start": s.start, "end": s.end, "text": s.text,
               "no_speech": s.no_speech_prob, "logprob": s.avg_logprob,
               "words": [{"start": w.start, "end": w.end, "word": w.word}
                         for w in (s.words or [])]}


def run_openai(m, pcm, model, lang, device, compute):
    print("  loading %s (%s)%s"
          % (model, device, fetch_note(model, "openai-whisper")))
    sys.stdout.flush()
    wm = m.load_model(model, device=device)
    r = wm.transcribe(pcm, language=lang, word_timestamps=True,
                      condition_on_previous_text=False, verbose=False)
    print("  language %s" % r.get("language", "?"))
    for s in r["segments"]:
        yield {"start": s["start"], "end": s["end"], "text": s.get("text", ""),
               "no_speech": s.get("no_speech_prob", 0.0),
               "logprob": s.get("avg_logprob", 0.0),
               "words": [{"start": w.get("start"), "end": w.get("end"),
                          "word": w.get("word", "")}
                         for w in (s.get("words") or [])]}


# ------------------------------------------------------------------- shaping

def clean(segs, keep_halluc):
    """Split the model's output into (kept, [(t, text, why_dropped)])."""
    kept, dropped = [], []
    for s in segs:
        t = " ".join((s["text"] or "").split())
        if not t:
            continue
        if not keep_halluc:
            ws = sl.words(t)
            why = None
            if s["no_speech"] > NO_SPEECH:
                why = "no speech here (p=%.2f)" % s["no_speech"]
            elif s["logprob"] < LOGPROB:
                why = "the model was guessing (logprob %.2f)" % s["logprob"]
            elif any(re.fullmatch(p, t, re.I) for p in BOILER):
                why = "boilerplate"
            elif kept and sl.content(ws) and \
                    sl.content(ws) == sl.content(sl.words(kept[-1]["text"])):
                why = "repeat of the line before"
            elif len(ws) >= 4 and len(set(ws)) == 1:
                why = "one word, %d times" % len(ws)
            if why:
                dropped.append((s["start"], t, why))
                continue
        kept.append(s)
    return kept, dropped


CJK = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _join(parts):
    """Join word tokens, without spaces inside a run of CJK.

    Whisper emits CJK one character to a "word", so joining blindly with a
    space turns a sentence into a column of single characters.  Both sides of
    the gap have to be CJK for the space to be wrong.
    """
    out = ""
    for p in parts:
        if out and not (CJK.search(out[-1]) and CJK.search(p[0])):
            out += " "
        out += p
    return out


def build_cues(kept, gap, max_dur, max_chars):
    """Words -> cues, breaking on a pause, a sentence end, or a hard cap.

    Cue boundaries are not cosmetic: the renderer apportions a cue's lines by
    characters across the cue's own window, so a cue that spans a pause holds
    its first line on screen through the silence.  Breaking after
    sentence-ending punctuation reproduces the shape whisper already chose,
    which is the shape a human subtitler would have chosen too.
    """
    words = []
    for s in kept:
        ws = [w for w in s["words"] if (w["word"] or "").strip()]
        if ws:
            words.extend(ws)
        else:
            # No word timings for this segment -- possible on odd audio.  Keep
            # it whole rather than inventing a per-word distribution.
            words.append({"start": s["start"], "end": s["end"],
                          "word": " ".join((s["text"] or "").split())})
    cues, cur, chars = [], [], 0
    for w in words:
        txt = w["word"].strip()
        if not txt or w["start"] is None or w["end"] is None:
            continue
        if cur:
            prev = cur[-1]["word"].strip()
            if (w["end"] - cur[0]["start"] > max_dur
                    or chars + len(txt) > max_chars
                    or w["start"] - cur[-1]["end"] > gap
                    or (prev and prev[-1] in END_PUNCT)):
                cues.append(cur)
                cur, chars = [], 0
        cur.append(w)
        chars += len(txt) + 1
    if cur:
        cues.append(cur)
    out = []
    for c in cues:
        t = _join([x["word"].strip() for x in c]).strip()
        if t:
            out.append((float(c[0]["start"]), float(c[-1]["end"]), t))
    return out


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Make an SRT for a video that has no subtitles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Reads an embedded text subtitle track if the file has one -- "
               "that is exact and takes seconds.  Only otherwise does it run "
               "speech recognition.")
    ap.add_argument("video")
    ap.add_argument("--out", help="where to write [default: beside the video, "
                                  "same name, .srt]")
    ap.add_argument("--model", default="small",
                    help="whisper model [default: small].  base and tiny "
                         "download less but are NOT faster and are blunter; "
                         "medium is slower and better on hard audio or heavy "
                         "accents")
    ap.add_argument("--lang", help="ISO code of the narration (ru, en, ...).  "
                                   "Auto-detected when omitted")
    ap.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    ap.add_argument("--compute-type", dest="compute_type",
                    help="int8 / float16 / float32 [default: int8 on CPU]")
    ap.add_argument("--stream", type=int,
                    help="force the embedded subtitle stream with this index")
    ap.add_argument("--list-streams", action="store_true",
                    help="show the subtitle streams and exit")
    ap.add_argument("--no-stream", action="store_true",
                    help="ignore embedded subtitles; always run the model")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing output file")
    ap.add_argument("--keep-hallucinations", action="store_true",
                    help="skip the confidence/boilerplate/repeat filters")
    ap.add_argument("--gap", type=float, default=0.60,
                    help="silence that ends a cue, in seconds [default: 0.60]")
    ap.add_argument("--max-cue", type=float, default=7.0, dest="max_cue",
                    help="longest cue in seconds [default: 7.0]")
    ap.add_argument("--max-chars", type=int, default=100, dest="max_chars",
                    help="longest cue in characters [default: 100]")
    a = ap.parse_args()

    if not os.path.exists(a.video):
        sl.die("no such file: %s" % a.video)
    if a.stream is not None and a.no_stream:
        sl.die("--stream and --no-stream contradict each other")
    stem = os.path.splitext(os.path.basename(a.video))[0]
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.video)),
                                stem + ".srt")

    # ---------------------------------------------------------- embedded subs
    streams = sub_streams(a.video)
    if a.list_streams:
        if not streams:
            print("no subtitle streams in %s" % a.video)
            return 0
        print("%d subtitle stream(s) in %s:" % (len(streams), a.video))
        for s in streams:
            print("  %s   %s" % (describe(s),
                                 "text" if s.get("codec_name") in TEXT_SUBS
                                 else "BITMAP -- unusable"))
        return 0

    if streams and not a.no_stream:
        st, why = pick_stream(streams, a.stream)
        if st is not None:
            if os.path.exists(out) and not a.force:
                sl.die("%s already exists -- pass --force to overwrite" % out)
            print("found an embedded subtitle stream (%s, %s) -- extracting it "
                  "rather than transcribing:" % (st.get("codec_name"), why))
            cues = extract_stream(a.video, st.get("index"), out)
            report(out, cues, None, a.video)
            return 0
        print("not using embedded subtitles: %s" % why)
    elif a.stream is not None:
        pick_stream([], a.stream)                       # dies with the indices

    if os.path.exists(out) and not a.force:
        sl.die("%s already exists -- pass --force to overwrite\n"
               "  (an hour of audio is not something to redo by accident)" % out)

    # ------------------------------------------------------------------ model
    got = load_backend()
    if not got:
        # Say whether a subtitle track was there and turned down, or never
        # existed -- "no embedded stream to fall back on" is a different
        # sentence, and a wrong one, when --no-stream is what removed it.
        hint = ""
        if a.no_stream and any(s.get("codec_name") in TEXT_SUBS for s in streams):
            hint = ("\n  (this file DOES have an embedded text subtitle track --\n"
                    "  drop --no-stream and it will be used instead)")
        elif streams:
            hint = ("\n  (its subtitle tracks are %s, none of them text)"
                    % ", ".join(sorted({s.get("codec_name") or "?"
                                        for s in streams})))
        sl.die(NO_BACKEND % (hint, a.video))
    name, mod = got
    device = resolve_device(a.device)
    compute = a.compute_type or ("float16" if device == "cuda" else "int8")

    print("decoding audio ...")
    pcm = decode(a.video)
    dur = len(pcm) / float(SR)
    if dur < 1.0:
        sl.die("only %.1f s of audio in %s" % (dur, a.video))
    print("audio %s | %s %s on %s" % (hms(dur), name, a.model, device))

    if device == "cpu":
        print("  CPU work: %s" % estimate(dur, a.model))
        # The line that used to be here -- "base is about twice as fast, medium
        # about three times slower" -- was wrong in the first half: measured,
        # `base` is the slowest of the three.  There is no faster tier to
        # recommend, so do not invent one.
        print("  %s" % ("medium is slower and better on hard audio; there is no "
                        "faster tier -- base and tiny are blunter, not quicker"
                        if a.model.startswith("small") else
                        "a smaller model is blunter, not faster -- all it saves "
                        "is the download"))
    ready = weights_ready(a.model, name)
    if not ready:
        # The estimate above is only the part that has a progress bar.  The
        # download has none, and it is the longer wait on a first run: `small`
        # is 464 MB at roughly 1 MB/s unauthenticated.  Someone who was told
        # "2 minutes", saw nothing for eight, and killed it has been misled by
        # an accurate number printed next to an omitted one.
        mb = MODEL_MB.get(a.model)
        print("  First run also downloads the model%s, silently -- huggingface_hub"
              % (" (%.0f MB)" % mb if mb else ""))
        print("  hides its progress bar when this is not a terminal.  It is a "
              "one-off;")
        print("  the weights land in %s and stay there." % hub_cache())
    sys.stdout.flush()

    run = run_faster if name == "faster-whisper" else run_openai
    # Two clocks, not one.  open_stream is where the model gets built, and on a
    # first run that means DOWNLOADING it -- `small` is 464 MB at roughly 1 MB/s.
    # With the single clock this used to have, that download was billed to the
    # transcription: a first base run of a 20 s clip printed "done in 0:03:40
    # (0.1x realtime)" for a job whose own pre-flight estimate said "under a
    # minute".  Both numbers came from this file.  The reader is owed the one
    # that is about the audio.
    mark = time.time()
    t0 = mark
    raw, last = [], 0
    try:
        first, segs, device, compute = open_stream(
            lambda d, c: run(mod, pcm, a.model, a.lang, d, c),
            device, compute, a.model)
        warm = time.time() - mark      # model load, and the first segment with it
        t0 = time.time()               # everything below is audio work, not waiting
        stream = itertools.chain([first], segs) if first is not None else segs
        for s in stream:
            raw.append(s)
            pct = int(100 * min(1.0, s["end"] / dur))
            if pct >= last + 10:
                last = pct - pct % 10
                print("  %3d%%  %s / %s  (%.0f s elapsed)"
                      % (last, hms(s["end"]), hms(dur), time.time() - t0))
                sys.stdout.flush()
    except KeyboardInterrupt:
        # Deliberately writes NOTHING.  A half-length SRT looks complete, and
        # every later step would silently ignore the second half of the video.
        print("\ninterrupted -- nothing written (re-run to start over)")
        return 3
    took = time.time() - t0

    kept, dropped = clean(raw, a.keep_hallucinations)
    cues = build_cues(kept, a.gap, a.max_cue, a.max_chars)
    if not cues:
        sl.die("no speech recognised in %s.\n  If the file is music or silence "
               "there is nothing to caption; if it is speech, try --model medium."
               % a.video)
    try:
        sl.write_srt(cues, out)
    except OSError as e:
        sl.die("could not write %s (%s)\n  pass --out to put it somewhere writable"
               % (out, e))

    print("  done in %s (%.1fx realtime)" % (hms(took), dur / max(took, 0.01)))
    if warm > 20:
        # Only worth a line when it is big enough to have confused someone --
        # a cached model loads in a second or two.
        print("  ...plus %.0f s before that on loading the model%s."
              % (warm, " -- the first-run download" if not ready else ""))
    if dropped:
        print("\ndropped %d of %d segments as hallucination -- read these, they "
              "are where the model invented speech:" % (len(dropped), len(raw)))
        for t, txt, why in dropped[:20]:
            print("  %8.2f  %-30s  %s" % (t, txt[:30], why))
        if len(dropped) > 20:
            print("  ... and %d more" % (len(dropped) - 20))
        print("  (--keep-hallucinations writes them out anyway)")
    report(out, cues, dur, a.video)
    return 0


def report(out, cues, dur, video):
    cover = max(e for _, e, _ in cues)
    print("\nwrote %s -- %d cues, %.1f s of %.1f s covered (%.0f%%)"
          % (out, len(cues), cover, dur or cover, 100.0 * cover / (dur or cover)))
    if dur is None:
        print("These came out of the file's own subtitle stream, so they carry "
              "that author's timing -- check the offset as usual.")
    else:
        print("""This SRT was timed off this video's own audio, so there is no offset to
find: expect pick_fragment.py to report about 0.00 and leave --cap-shift at
0.00.  A weak offset reading here is the expected answer, not a discovery.""")
    print("\n  python tools/pick_fragment.py \"%s\" \"%s\" --top 25"
          % (video, out))


if __name__ == "__main__":
    sl.setup_stdout()
    sys.exit(main())
