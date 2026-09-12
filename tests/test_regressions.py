import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_short as build
import shortlib as sl
import pick_fragment as pick
import meta


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.srt = self.root / "captions.srt"

    def test_numeric_srt_roundtrip(self):
        cues = [(1, 2, "2026"), (3, 4, "42")]
        sl.write_srt(cues, str(self.srt))
        self.assertEqual(sl.read_srt(str(self.srt)), cues)
        self.srt.write_bytes(b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:02,000\r\nprice\r\n2026\r\n")
        self.assertEqual(sl.read_srt(str(self.srt))[0][2], "price 2026")

    def test_long_tokens_preserve_text_and_limit(self):
        for text in ["hello abcdefghijklmnop world end", "сверхпроизводительность", "漢字" * 30]:
            parts = build.chunk_text(text, 15)
            self.assertTrue(all(0 < len(p) <= 15 for p in parts))
            self.assertEqual("".join(parts).replace(" ", ""), text.replace(" ", ""))
        with self.assertRaises(SystemExit):
            build.chunk_text("test", 0)

    def test_caption_cue_boundaries_and_short_holds(self):
        sl.write_srt([(1, 2, "first words"), (8, 8.2, "second words with extra text")], str(self.srt))
        ctx = {"lay": sl.load_layout(), "t0": 0, "dur": 10, "cap_shift": 0,
               "srt": str(self.srt), "font": "mock"}
        with patch.object(build.ImageFont, "truetype"), patch.object(build, "make_phrase_image", return_value=(Image.new("RGBA", (10, 10)), 0)):
            phrases = build.build_phrases(ctx)
        self.assertEqual(phrases[0][1:3], (1, 2))
        for _, start, end, *_ in phrases[1:]:
            self.assertGreaterEqual(start, 8 - 1e-9)
            self.assertLessEqual(end, 8.2)
            self.assertGreater(end, start)

    def run_picker(self, gaps):
        sl.write_srt([(1, 2, "first words"), (8, 9, "second words")], str(self.srt))
        output = io.StringIO()
        with patch.object(sys, "argv", ["pick", "fake", str(self.srt), "--check", "0", "3"]), patch.object(pick, "envelope", return_value=(np.arange(1000) * .01, np.zeros(1000))), patch.object(pick, "gaps_from", return_value=gaps), contextlib.redirect_stdout(output):
            pick.main()
        return output.getvalue()

    def test_no_evidence_keeps_zero_offset(self):
        output = self.run_picker([])
        self.assertIn("not ONE cue", output)
        self.assertIn("1.00  first words", output)

    def test_weak_evidence_keeps_zero_offset(self):
        output = self.run_picker([(0, 1.3), (7, 8.3)])
        self.assertIn("NO clear constant offset", output)
        self.assertIn("1.00  first words", output)

    def test_hashtags_written_without_duplicates(self):
        out = self.root / "meta.txt"
        with patch.object(sys, "argv", ["meta", "--title", "title", "--desc", "body #test", "--hashtags", "test demo demo", "--out", str(out)]):
            self.assertEqual(meta.main(), 0)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(text.count("#test"), 1)
        self.assertEqual(text.count("#demo"), 1)

    def test_portrait_background_crop(self):
        ctx = {"lay": sl.load_layout(), "src": "fake", "stage1": "out", "nf": 60,
               "src_w": 720, "src_h": 1600, "t0": 0, "dur": 1}
        with patch.object(build.subprocess, "run") as run:
            build.stage1(ctx)
        cmd = run.call_args.args[0]
        self.assertIn("scale=1080:2400,crop=1080:1920:0:240", cmd[cmd.index("-filter_complex") + 1])

    def test_job_lock_blocks_concurrent_writer(self):
        with build.job_lock(str(self.root)):
            with self.assertRaises(SystemExit):
                with build.job_lock(str(self.root)):
                    self.fail("second writer acquired lock")
        self.assertFalse((self.root / ".render.lock").exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_render_and_reuse_validation(self):
        src = self.root / "source.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=180x400:rate=24", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
        sl.write_srt([(.1, .6, "2026"), (1.2, 1.8, "hello world")], str(self.srt))
        job = self.root / "job"
        out = self.root / "short.mp4"
        cmd = [sys.executable, str(ROOT / "tools/build_short.py"), "--src", str(src), "--srt", str(self.srt), "--t0", "0", "--dur", "2", "--canvas", "270x480", "--job", str(job), "--out", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        w, h, fps, dur = sl.probe(str(out))
        self.assertEqual((w, h, fps), (270, 480, 60))
        self.assertAlmostEqual(dur, 2, places=1)
        subprocess.run(cmd + ["--caps-only", "--cap-shift", "0.05"], check=True, capture_output=True)
        changed = cmd.copy()
        changed[changed.index("--dur") + 1] = "1"
        result = subprocess.run(changed + ["--caps-only"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        # A different source must not reuse the composition either.
        other = self.root / "other.mp4"
        shutil.copyfile(src, other)
        changed = cmd.copy()
        changed[changed.index("--src") + 1] = str(other)
        result = subprocess.run(changed + ["--caps-only"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
