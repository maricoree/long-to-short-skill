# -*- coding: utf-8 -*-
"""Pick the fragment and sync the subtitles -- one command, no guessing.

    python pick_fragment.py LONG.mp4 caps.srt
    python pick_fragment.py LONG.mp4 caps.srt --used "opening phrase" "..."
    python pick_fragment.py LONG.mp4 caps.srt --check 146.40 23.30

It answers the three questions that come before any render:

  1. HOW FAR AHEAD IS THE SUBTITLE FILE?  Captions that "run fast" are usually
     not a taste complaint, they are a constant offset: the cue file was
     authored ahead of the speaker.  Measured off the audio envelope -- a
     caption boundary should land in a silence, so the shift that puts EVERY
     cue start in one is the offset.  A cheaper probe (nearest speech onset
     after each cue) reads 0.00 for every cue and tells you nothing; only the
     GAPS carry the information.
  2. WHERE CAN THE FRAGMENT START AND END?  Both ends must sit inside a
     silence, or you cut a word in half -- which is what "the video starts
     mid-word" is, and no numeric check on the render will see it.
  3. WHICH FRAGMENTS ARE ALREADY USED?  Finished pieces get named by their
     opening phrase.  Matching is a token-subsequence test over content words,
     so punctuation, casing and line breaks in the cue file cannot hide a
     collision.

Envelope: mono 16 kHz, 10 ms RMS hops, 3-hop smoothing.  Silence is a run
below an ABSOLUTE -40 dBFS for at least --min-gap seconds.  Absolute on
purpose: an adaptive threshold at speech-minus-35 dB collapsed to -56 dBFS on
an 840 s continuously narrated video and found 8 gaps in the whole file --
narration over room tone never drops 35 dB below its own 95th percentile.
Pass --thr for a source that is unusually quiet or unusually loud.
"""
import argparse
import subprocess
import sys

import numpy as np

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shortlib as sl                                                    # noqa: E402

HOP_MS = 10
SR = 16000
THR_DBFS = -40.0


def envelope(path):
    """-> (times, dBFS) at 10 ms hops, mono 16 kHz."""
    b = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1",
                        "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True).stdout
    if not b:
        sl.die("ffmpeg produced no audio for %s -- is it a media file ffmpeg "
               "can read?" % path)
    x = np.frombuffer(b, np.int16).astype(np.float32) / 32768.0
    hop = SR * HOP_MS // 1000
    n = len(x) // hop
    x = x[:n * hop].reshape(n, hop)
    db = 20.0 * np.log10(np.maximum(np.sqrt((x * x).mean(axis=1)), 1e-9))
    return np.arange(n) * HOP_MS / 1000.0, np.convolve(db, np.ones(3) / 3.0,
                                                       mode="same")


def gaps_from(times, db, thr, min_gap):
    quiet = db < thr
    out, i, n = [], 0, len(quiet)
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            a, z = times[i], times[min(j, n - 1)]
            if z - a >= min_gap:
                out.append((a, z))
            i = j
        else:
            i += 1
    return out


def in_gap(t, gaps):
    return any(a <= t <= z for a, z in gaps)


def locate(needle, allw):
    """Find where a remembered phrase sits in the SRT.

    Finished fragments get named from memory, so an exact match fails on
    trivial differences -- "side of zone" against "side of the zone".  Match
    on CONTENT words only (stopwords dropped) and score by recall inside a
    small sliding window; 0.8 recall means it is that phrase.  Returns
    (recall, word_index) into ``allw``.
    """
    nc = sl.content(needle)
    if not nc:
        return 0.0, -1
    # The window must span the phrase as SPOKEN (stopwords included); sizing it
    # by the content-word count made it too narrow and dropped phrases that
    # were plainly there -- 0.67 for a phrase whose every content word is in
    # the SRT.
    win = len(needle) + 3
    best, bi = 0.0, -1
    for i in range(max(1, len(allw) - win)):
        w = set(sl.content(allw[i:i + win]))
        r = sum(1 for wd in nc if wd in w) / float(len(nc))
        if r > best:
            best, bi = r, i
    return best, bi


def main():
    ap = argparse.ArgumentParser(
        description="Pick a fragment out of a long video and sync its subtitles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run it with no --check first: it prints the SRT offset and "
               "every unused window, densest first.")
    ap.add_argument("video")
    ap.add_argument("srt")
    ap.add_argument("--used", nargs="*", default=[],
                    help="opening phrases of fragments already made")
    ap.add_argument("--min", type=float, default=15.0, help="shortest window")
    ap.add_argument("--max", type=float, default=30.0, help="longest window")
    ap.add_argument("--top", type=int, default=25, help="how many to list")
    ap.add_argument("--sort", choices=("words", "time"), default="words",
                    help="words = most speech first (default), time = in order")
    ap.add_argument("--thr", type=float,
                    help="silence threshold in dBFS [default: %.0f]" % THR_DBFS)
    ap.add_argument("--min-gap", type=float, default=0.22, dest="min_gap",
                    help="shortest run that counts as a silence")
    ap.add_argument("--check", nargs=2, type=float, metavar=("T0", "DUR"),
                    help="check one window instead of listing candidates")
    a = ap.parse_args()

    cues = sl.read_srt(a.srt)
    if not cues:
        sl.die("no cues parsed out of %s -- is it really an SRT?" % a.srt)
    times, db = envelope(a.video)
    dur = float(times[-1])
    speech = float(np.percentile(db, 95))
    thr = a.thr if a.thr is not None else THR_DBFS
    gaps = gaps_from(times, db, thr, a.min_gap)
    print("video %.1f s | %d cues | speech %.0f dBFS | threshold %.0f dBFS | "
          "%d gaps >= %.2f s" % (dur, len(cues), speech, thr, len(gaps), a.min_gap))

    # ---------------------------------------------------------------- 1. offset
    best = []
    for off in np.arange(-2.0, 2.001, 0.05):
        starts = [s + off for s, _, _ in cues if 0 <= s + off <= dur]
        if starts:
            hit = sum(1 for s in starts if in_gap(s, gaps))
            best.append((hit / float(len(starts)), hit, len(starts), float(off)))
    best.sort(reverse=True)
    print("\nSRT offset -- fraction of cue starts landing in a silence:")
    for frac, hit, tot, off in best[:3]:
        print("  %+5.2f s   %d/%d  (%.0f%%)" % (off, hit, tot, 100 * frac))
    # Offsets that all put every cue start in a silence form a plateau, not a
    # point, and the plateau is not centred on the truth: a cue starts when
    # speech starts, i.e. at the END of the preceding silence, so the plateau
    # runs from the true offset down to (truth - gap width).  The UPPER edge is
    # therefore the estimate -- which is what the reverse sort already picks.
    # Print the width so a loose fit is visible rather than implied.
    off = best[0][3] if best else 0.0
    if best and best[0][0] > 0:
        tie = [o for f, h, t, o in best if f >= best[0][0] - 1e-9]
        if len(tie) > 1 and max(tie) - min(tie) > 0.1:
            print("  (%.2f..%.2f all score the same -- the fit is only good to "
                  "about %.2f s; fine-tune by ear)" % (min(tie), max(tie),
                                                       max(tie) - min(tie)))
    base = next((f for f, h, t, o in best if abs(o) < 1e-6), None)
    if base is None:
        b0 = [s for s, _, _ in cues if 0 <= s <= dur]
        base = sum(1 for s in b0 if in_gap(s, gaps)) / float(len(b0)) if b0 else 0.0
    print("  at offset 0.00: %.0f%%" % (100 * base))
    if abs(off) < 0.05:
        print("  -> the subtitles are already in sync; leave --cap-shift at 0.00")
    else:
        print("  -> cues run %+.2f s relative to the audio; pass --cap-shift %+.2f"
              % (off, off))
    # The absolute fraction is NOT the test -- plenty of cue boundaries are
    # mid-sentence wraps and land in speech even on a perfectly synced file.
    # The test is the MARGIN over offset 0.00: a genuinely offset file jumps.
    if off and best and best[0][0] < 1.6 * base + 0.05:
        print("  !! only %.0f%% against %.0f%% at zero -- no clear constant offset.\n"
              "     Leave --cap-shift at 0.00 and sync by ear."
              % (100 * best[0][0], 100 * base))

    # ---------------------------------------------------------------- 2. window
    if a.check:
        t0, d = a.check
        t1 = t0 + d
        print("\ncheck %.2f .. %.2f (%.2f s)" % (t0, t1, d))
        gs = [g for g in gaps if g[0] <= t0 <= g[1]]
        ge = [g for g in gaps if g[0] <= t1 <= g[1]]
        print("  start in silence: %s" % ("%.2f-%.2f" % gs[0] if gs else "NO -- mid-word"))
        print("  end   in silence: %s" % ("%.2f-%.2f" % ge[0] if ge else "NO -- mid-word"))
        sel = [(s + off, e + off, t) for s, e, t in cues if t0 <= s + off < t1]
        chars = sum(len(t) for _, _, t in sel)
        over = [(s + off, t) for s, e, t in cues if t0 - 1.0 < s + off < t0]
        if over:
            print("  !! a cue starts before the window (%.2f s): %r\n"
                  "     it is cut into, and the render will DROP it rather than "
                  "clip it" % (over[0][0], over[0][1][:60]))
        print("  %d cues, %d characters, ~%d caption lines at 15 chars"
              % (len(sel), chars, -(-chars // 15)))
        for s, e, t in sel:
            print("    %7.2f  %s" % (s, t))
        return 0

    # Locate every finished fragment ONCE, as an SRT time, then exclude any
    # candidate window that spans it.  Comparing words against words was
    # fragile; a time is not.
    allw, owner = [], []
    for ci, (s, e, t) in enumerate(cues):
        for w in sl.words(t):
            allw.append(w)
            owner.append(ci)
    used_at, notfound = [], []
    for u in a.used:
        r, wi = locate(sl.words(u), allw)
        if r >= 0.8 and wi >= 0:
            used_at.append((cues[owner[wi]][0] + off, r, u))
        else:
            notfound.append((u, r))
    if used_at:
        print("\nalready made -- excluded from the candidates:")
        for t, r, u in sorted(used_at):
            print("  %7.2f s  (match %.2f)  %s" % (t, r, u[:66]))
    for u, r in notfound:
        print("  ?? no match (best %.2f) for %r -- check the wording" % (r, u[:60]))

    cands = []
    for i, (ga, gz) in enumerate(gaps):
        for gb, gd in gaps[i + 1:]:
            t0, t1 = (ga + gz) / 2.0, (gb + gd) / 2.0
            d = t1 - t0
            if d < a.min or d > a.max:
                continue
            if any(t0 <= ut <= t1 for ut, _, _ in used_at):
                continue
            sel = [(s + off, e + off, t) for s, e, t in cues if t0 <= s + off < t1]
            if sel:
                cands.append((t0, t1, d, sel,
                              sl.words(" ".join(t for _, _, t in sel))))
    # Every gap pair yields a window, so neighbouring pairs yield near-copies
    # (584.79/613.42 and 585.61/613.42 are the same cut).  Collapse anything
    # within 1.5 s of a window already kept, or the list is unreadable.
    cands.sort(key=lambda c: -len(c[4]))
    keep = []
    for c in cands:
        if any(abs(c[0] - k[0]) < 1.5 and abs(c[1] - k[1]) < 1.5 for k in keep):
            continue
        keep.append(c)
    if not keep:
        print("\n!! no candidate windows -- the gaps are too few or too far "
              "apart.\n   Re-run with --thr (try -35 or -45), or widen "
              "--min/--max.")
        return 1
    if a.sort == "time":
        keep.sort(key=lambda c: c[0])
    print("\n%d unused %.0f-%.0f s windows -> %d distinct; showing %d by %s"
          % (len(cands), a.min, a.max, len(keep), min(a.top, len(keep)), a.sort))
    print("%-8s %-8s %-6s %-5s %s" % ("start", "end", "dur", "words", "opens with"))
    for t0, t1, d, sel, w in keep[:a.top]:
        print("%-8.2f %-8.2f %-6.1f %-5d %s"
              % (t0, t1, d, len(w), " ".join(sel[0][2].split()[:9])))
    return 0


if __name__ == "__main__":
    sl.setup_stdout()
    sys.exit(main())
