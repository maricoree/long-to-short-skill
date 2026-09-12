# -*- coding: utf-8 -*-
"""Write the upload metadata for a short, and check it before it goes out.

    python meta.py --title "..." --desc-file desc.txt --tags-file tags.txt \
                   --srt caps.srt --t0 146.40 --dur 23.30 --out metadata.txt

WHY THE ECHO CHECK EXISTS.  The first description written for a short was the
narration with two words changed, and it was rejected in one line: "that is
literally my own words with a couple of changes".  A description that restates
the narration adds nothing -- the viewer just heard it.  So this finds every
word run shared between the description and the fragment's own captions and
fails on it.  That is the single defect this file exists to prevent;
everything else here is field limits.

Pass --t0/--dur to scope the check to the fragment.  Without them it compares
against the whole subtitle file, which is stricter than intended and will
flag a description that overlaps a part of the video you did not even use.

Limits: YouTube tags cap at 500 characters total, Shorts surfaces show about
three hashtags, and only the FIRST LINE of a description is visible before
"...more", so the hook has to be in line one.

Output is `metadata.txt`, ready to paste, plus a check report on stdout.
Exit code 2 means at least one hard failure.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shortlib as sl                                                    # noqa: E402

TAG_LIMIT = 500
DESC_LIMIT = 5000
HOOK_LIMIT = 100


def ngrams(ws, n):
    return set(tuple(ws[i:i + n]) for i in range(len(ws) - n + 1))


def copies(desc, spoken):
    """Shared word runs that mean the description was copied from the narration.

    Four words alone is too loose -- "is one of those" is ordinary English and
    flagged a description that shared nothing else, and a checker that cries
    wolf is one nobody reads.  So: a 5-word run counts, and so does a 4-word
    run carrying two or more non-stopwords, which is what a lifted sentence
    looks like.
    """
    out = ngrams(desc, 5) & ngrams(spoken, 5)
    for g in ngrams(desc, 4) & ngrams(spoken, 4):
        if sum(1 for w in g if w not in sl.STOP) >= 2:
            out.add(g)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(
        description="Write and check the upload metadata for a short.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--desc", help="description text")
    ap.add_argument("--desc-file", dest="desc_file", help="description file")
    ap.add_argument("--tags", help="comma-separated tag string")
    ap.add_argument("--tags-file", dest="tags_file")
    ap.add_argument("--hashtags", default="",
                    help="the ~3 hashtags to surface; defaults to any in the "
                         "description")
    ap.add_argument("--srt", help="subtitles to check the description against")
    ap.add_argument("--t0", type=float, help="fragment start, for a scoped check")
    ap.add_argument("--dur", type=float, help="fragment length, for a scoped check")
    ap.add_argument("--out", default="metadata.txt")
    a = ap.parse_args()

    if a.desc_file:
        desc = open(a.desc_file, encoding="utf-8").read().strip()
    else:
        desc = a.desc or ""
    if a.tags_file:
        tags = open(a.tags_file, encoding="utf-8").read().strip()
    else:
        tags = a.tags or ""
    tags = re.sub(r"\s+", " ", tags.replace("\n", " ")).strip()
    fails, warns = [], []

    # ------------------------------------------------------------- field limits
    if len(tags) > TAG_LIMIT:
        fails.append("tags are %d chars, over the %d limit -- cut %d"
                     % (len(tags), TAG_LIMIT, len(tags) - TAG_LIMIT))
    if len(desc) > DESC_LIMIT:
        fails.append("description is %d chars, over %d" % (len(desc), DESC_LIMIT))
    if not desc.strip():
        warns.append("empty description")

    tags_in_desc = re.findall(r"#\w+", desc)
    hs = [h if h.startswith("#") else "#" + h
          for h in re.split(r"[,\s]+", a.hashtags) if h]
    all_hs = hs or tags_in_desc
    if len(all_hs) > 3:
        warns.append("%d hashtags -- Shorts surfaces show about 3; keep the "
                     "three strongest" % len(all_hs))
    if not all_hs:
        warns.append("no hashtags")

    first = desc.splitlines()[0] if desc.splitlines() else ""
    if len(first) > HOOK_LIMIT:
        warns.append("first line is %d chars; only ~%d show before '...more'"
                     % (len(first), HOOK_LIMIT))

    # ------------------------------------------------------------ the echo test
    if a.srt and os.path.exists(a.srt):
        cues = sl.read_srt(a.srt)
        if a.t0 is not None and a.dur is not None:
            t1 = a.t0 + a.dur
            cues = [c for c in cues if c[0] < t1 and c[1] > a.t0]
            scope = "the fragment (%.2f..%.2f s)" % (a.t0, t1)
        else:
            scope = "the whole subtitle file"
        spoken = " ".join(t for _, _, t in cues)
        dw, sw = sl.words(desc), sl.words(spoken)
        shared = copies(dw, sw)
        if shared:
            fails.append("the description is re-telling the narration -- %d "
                         "run(s) of 4+ words are lifted straight from %s:"
                         % (len(shared), scope))
            for g in shared[:6]:
                fails.append("    \"%s\"" % " ".join(g))
            fails.append("  rewrite it to ADD what the clip does not say: what "
                         "the thing is, why it is remarkable, or a question for "
                         "the comments. Do not retell what the viewer just heard.")
        else:
            sw_set = set(sw)
            over = [w for w in dw if w in sw_set]
            print("echo check: nothing lifted from %s (%d/%d words overlap, "
                  "all incidental)" % (scope, len(over), len(dw)))
    else:
        warns.append("no --srt given, echo check skipped")

    # ------------------------------------------------------------------- report
    print("title       %3d chars" % len(a.title))
    print("description %3d chars, %d lines, %d hashtags"
          % (len(desc), len(desc.splitlines()), len(all_hs)))
    print("tags        %3d/%d chars, %d tags"
          % (len(tags), TAG_LIMIT, len([t for t in tags.split(",") if t.strip()])))
    for w in warns:
        print("  warn: %s" % w)
    for f in fails:
        print("  FAIL: %s" % f)

    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("TITLE\n%s\n\nDESCRIPTION\n%s\n\nTAGS\n%s\n" % (a.title, desc, tags))
    print("-> %s" % os.path.abspath(a.out))
    return 2 if fails else 0


if __name__ == "__main__":
    sl.setup_stdout()
    sys.exit(main())
