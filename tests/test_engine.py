import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.index import IndexReader, build_index  # noqa: E402
from engine.scoring import format_score, score100  # noqa: E402
from engine.wordsio import InputError, parse_queries, parse_words  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")
WORDS_DIR = os.path.join(SAMPLES, "words")
QUERIES_DIR = os.path.join(SAMPLES, "queries")

EXPECTED_COUNTS = {
    "basic": 14,
    "ranking": 13,
    "equal-freq": 34,
    "unicode": 16,
    "dense": 800,
    "long": 13,
    "big": 100000,
}


class ScoringTest(unittest.TestCase):
    def test_score_formula_and_integer_format(self):
        self.assertEqual(score100(120, 5, 5), 1323000)
        self.assertEqual(format_score(1323000), "13230.00")
        self.assertEqual(format_score(457320), "4573.20")
        self.assertEqual(format_score(5), "0.05")
        self.assertEqual(format_score(0), "0.00")

    def test_length_bonus(self):
        # 同频率权重，更长的词分更高
        self.assertGreater(
            score100(10, 0, 5), score100(10, 0, 4)
        )


class ParsingTest(unittest.TestCase):
    def parse(self, text):
        return parse_words(text.encode("utf-8"), "mem")

    def test_fields_default_weight_comment_dup(self):
        entries = self.parse(
            "# 注释\napple\t10\napple\t99\t9\nbanana\t1\t2\n"
        )
        self.assertEqual(entries, [("apple", 10, 0), ("banana", 1, 2)])

    def test_bom_crlf_blank(self):
        entries = parse_words(b"\xef\xbb\xbfapple\t10\r\n\r\n  # c\r\n", "mem")
        self.assertEqual(entries, [("apple", 10, 0)])

    def test_illegal_lines(self):
        bad = [
            "a\t10\tx\ty\n",          # 字段数
            "a\t10\n \tb\t1\n",       # 词含空白（第二行）
            "a\t0\n",                 # 频率下界
            "a\t10000000000\n",       # 频率上界
            "a\t10\t-1\n",            # 权重负
            "a\t10\t100001\n",        # 权重上界
            "a b\t10\n",              # 词含空格
            "a\t1x\n",                # 频率非数字
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(InputError):
                    self.parse(text)

    def test_word_length_limit(self):
        with self.assertRaises(InputError):
            self.parse("a" * 4097 + "\t1\n")
        entries = self.parse("a" * 4096 + "\t1\n")
        self.assertEqual(len(entries[0][0]), 4096)

    def test_empty_word_table(self):
        self.assertEqual(self.parse("# only comment\n\n"), [])

    def test_query_validation(self):
        qs = parse_queries(b"\t10\napp\t3\n", "mem")
        self.assertEqual(qs, [("", 10), ("app", 3)])
        for raw in [b"app\tx\n", b"app\t-1\n", b"app\t1001\n", b"a p\t1\n",
                    b"app\n", b"app\t1\t2\n"]:
            with self.subTest(raw=raw):
                with self.assertRaises(InputError):
                    parse_queries(raw, "mem")


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pptest-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name):
        path = os.path.join(WORDS_DIR, f"{name}.txt")
        raw = open(path, "rb").read()
        dest = os.path.join(self.tmp, f"idx-{name}")
        manifest = build_index(
            parse_words(raw, path), hashlib.sha256(raw).hexdigest(), dest
        )
        return dest, manifest

    def test_manifest_counts(self):
        for name, count in EXPECTED_COUNTS.items():
            with self.subTest(name=name):
                _dest, manifest = self.build(name)
                self.assertEqual(manifest["entry_count"], count)
                self.assertEqual(manifest["version"], 1)
                self.assertIn("source_sha256", manifest)

    def test_empty_index(self):
        dest = os.path.join(self.tmp, "empty")
        build_index([], "0" * 64, dest)
        reader = IndexReader(dest)
        self.assertEqual(reader.entry_count, 0)
        self.assertEqual(reader.complete("", 10), [])
        self.assertEqual(reader.complete("a", 10), [])

    def test_total_order_tie_breaking(self):
        dest, _ = self.build("equal-freq")
        reader = IndexReader(dest)
        rows = reader.complete("t", 35)
        words = [w for w, _ in rows]
        self.assertEqual(words, [f"t{i:03d}" for i in range(1, 31)])

    def test_ranking_sample(self):
        dest, _ = self.build("ranking")
        reader = IndexReader(dest)
        self.assertEqual(
            reader.complete("w", 5),
            [("w1234", 420000), ("w123", 416000), ("w12", 412000),
             ("w1", 408000)],
        )
        self.assertEqual(
            [w for w, _ in reader.complete("tie", 2)], ["tie-a", "tie-b"]
        )

    def test_empty_prefix_uses_built_ranking(self):
        dest, _ = self.build("basic")
        reader = IndexReader(dest)
        rows = reader.complete("", 3)
        self.assertEqual(
            [w for w, _ in rows], ["上海", "apple", "apply"]
        )

    def test_k_zero(self):
        dest, _ = self.build("basic")
        reader = IndexReader(dest)
        self.assertEqual(reader.complete("app", 0), [])

    def test_unicode_distinctions(self):
        dest, _ = self.build("unicode")
        reader = IndexReader(dest)
        self.assertEqual(
            [w for w, _ in reader.complete("A", 5)], ["App", "APP"]
        )
        self.assertEqual(reader.complete("CAF", 3), [])
        self.assertEqual(
            [w for w, _ in reader.complete("🙂", 5)][:1], ["🙂"]
        )

    def test_deterministic_two_builds(self):
        dest1, _ = self.build("big")
        dest2, _ = self.build("big")
        r1, r2 = IndexReader(dest1), IndexReader(dest2)
        qs = parse_queries(
            open(os.path.join(QUERIES_DIR, "big.txt"), "rb").read(), "big"
        )
        for prefix, k in qs:
            self.assertEqual(r1.complete(prefix, k), r2.complete(prefix, k))

    def test_index_relocatable_and_words_independent(self):
        dest, _ = self.build("basic")
        moved = os.path.join(self.tmp, "moved")
        shutil.copytree(dest, moved)
        reader = IndexReader(moved)
        self.assertEqual(len(reader.complete("app", 10)), 5)
        # manifest 可用即索引可用
        self.assertTrue(os.path.isfile(os.path.join(moved, "manifest.json")))

    def test_bad_version_and_missing_manifest(self):
        dest, _ = self.build("basic")
        mp = os.path.join(dest, "manifest.json")
        data = json.load(open(mp, encoding="utf-8"))
        data["version"] = 99
        with open(mp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with self.assertRaises(Exception):
            IndexReader(dest)
        with self.assertRaises(Exception):
            IndexReader(self.tmp)


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ppcli-")
        cls.var = os.path.join(cls.tmp, "idx")
        os.makedirs(cls.var)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "cli.py"), *args],
            capture_output=True,
        )
        self.assertEqual(
            proc.returncode, expect,
            msg=f"args={args}\nstderr={proc.stderr.decode('utf-8', 'replace')}",
        )
        return proc.stdout

    def test_end_to_end_all_samples_byte_exact(self):
        for name in EXPECTED_COUNTS:
            idx = os.path.join(self.var, name)
            self.run_cli(
                "build", "--words", os.path.join(WORDS_DIR, f"{name}.txt"),
                "--index", idx,
            )
            out = self.run_cli(
                "query", "--index", idx,
                "--queries", os.path.join(QUERIES_DIR, f"{name}.txt"),
            )
            expected = open(
                os.path.join(QUERIES_DIR, f"{name}.expected.txt"), "rb"
            ).read()
            self.assertEqual(out, expected, msg=f"sample {name}")

    def test_invalid_queries_no_output_exit1(self):
        idx = os.path.join(self.var, "basic")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "cli.py"), "query",
             "--index", idx,
             "--queries", os.path.join(QUERIES_DIR, "invalid.txt")],
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")

    def test_missing_index_exit2(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "cli.py"), "query",
             "--index", os.path.join(self.tmp, "nope"),
             "--queries", os.path.join(QUERIES_DIR, "basic.txt")],
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 2)

    def test_bench_json(self):
        big_idx = os.path.join(self.var, "big")
        if not os.path.exists(os.path.join(big_idx, "manifest.json")):
            self.run_cli(
                "build", "--words", os.path.join(WORDS_DIR, "big.txt"),
                "--index", big_idx,
            )
        out = self.run_cli(
            "bench", "--index", os.path.join(self.var, "big"),
            "--queries", os.path.join(QUERIES_DIR, "big.txt"),
            "--repeat", "3",
        )
        payload = json.loads(out.decode("utf-8"))
        self.assertEqual(payload["queries"], 11)
        self.assertEqual(payload["repeat"], 3)
        for key in ("p50_ms", "p95_ms", "p99_ms", "rss_mb"):
            self.assertIn(key, payload)

    def test_build_illegal_words_no_index(self):
        bad = os.path.join(self.tmp, "bad.txt")
        with open(bad, "wb") as fh:
            fh.write("ok\t1\nbroken\tnotanumber\n".encode("utf-8"))
        dest = os.path.join(self.tmp, "bad-idx")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "cli.py"), "build",
             "--words", bad, "--index", dest],
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(dest, "manifest.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
