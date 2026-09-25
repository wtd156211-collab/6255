import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, 'cli.py')
SAMPLES = os.path.join(ROOT, 'samples')

sys.path.insert(0, ROOT)
import cli as engine  # noqa: E402


def run_cli(*argv, cwd=ROOT):
    return subprocess.run(
        [sys.executable, CLI, *argv],
        capture_output=True, cwd=cwd, timeout=120)


class SamplePairsTest(unittest.TestCase):
    """每组样例：build + query 的 stdout 与期望文件逐字节一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _check_pair(self, name):
        words = os.path.join(SAMPLES, 'words', name + '.txt')
        queries = os.path.join(SAMPLES, 'queries', name + '.txt')
        expected = os.path.join(SAMPLES, 'queries', name + '.expected.txt')
        index = os.path.join(self.tmp, 'index-' + name)
        res = run_cli('build', '--words', words, '--index', index)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run_cli('query', '--index', index, '--queries', queries)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(expected, 'rb') as fh:
            self.assertEqual(res.stdout, fh.read(), name)
        return index

    def test_basic(self):
        self._check_pair('basic')

    def test_ranking(self):
        self._check_pair('ranking')

    def test_equal_freq(self):
        self._check_pair('equal-freq')

    def test_unicode(self):
        self._check_pair('unicode')

    def test_dense(self):
        self._check_pair('dense')

    def test_long(self):
        self._check_pair('long')

    def test_big(self):
        index = self._check_pair('big')
        with open(os.path.join(index, 'manifest.json'), encoding='utf-8') as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest['version'], 1)
        self.assertEqual(manifest['entry_count'], 100000)
        self.assertEqual(len(manifest['source_sha256']), 64)
        self.assertEqual(manifest['source_sha256'], manifest['source_sha256'].lower())

    def test_manifest_entry_counts(self):
        expected = {'basic': 14, 'ranking': 13, 'equal-freq': 34,
                    'unicode': 16, 'dense': 800, 'long': 13}
        for name, count in expected.items():
            index = self._check_pair(name)
            with open(os.path.join(index, 'manifest.json'), encoding='utf-8') as fh:
                self.assertEqual(json.load(fh)['entry_count'], count, name)

    def test_invalid_queries_empty_stdout(self):
        index = self._check_pair('basic')
        res = run_cli('query', '--index', index,
                      '--queries', os.path.join(SAMPLES, 'queries', 'invalid.txt'))
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.stdout, b'')

    def test_index_relocation(self):
        index = self._check_pair('basic')
        moved = os.path.join(self.tmp, 'elsewhere', 'index')
        shutil.copytree(index, moved)
        res = run_cli('query', '--index', moved,
                      '--queries', os.path.join(SAMPLES, 'queries', 'basic.txt'))
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(SAMPLES, 'queries', 'basic.expected.txt'), 'rb') as fh:
            self.assertEqual(res.stdout, fh.read())

    def test_missing_index_exit_2(self):
        res = run_cli('query', '--index', os.path.join(self.tmp, 'nope'),
                      '--queries', os.path.join(SAMPLES, 'queries', 'basic.txt'))
        self.assertEqual(res.returncode, 2)

    def test_repeat_build_consistent_results(self):
        words = os.path.join(SAMPLES, 'words', 'ranking.txt')
        queries = os.path.join(SAMPLES, 'queries', 'ranking.txt')
        outputs = []
        for i in range(2):
            index = os.path.join(self.tmp, 'idx%d' % i)
            self.assertEqual(run_cli('build', '--words', words, '--index', index).returncode, 0)
            res = run_cli('query', '--index', index, '--queries', queries)
            outputs.append(res.stdout)
        self.assertEqual(outputs[0], outputs[1])


class InvalidWordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.words = os.path.join(self.tmp, 'words.txt')
        self.index = os.path.join(self.tmp, 'index')

    def _build(self, content):
        with open(self.words, 'wb') as fh:
            fh.write(content)
        return run_cli('build', '--words', self.words, '--index', self.index)

    def test_bad_freq(self):
        res = self._build(b'apple\tx\n')
        self.assertEqual(res.returncode, 1)
        self.assertFalse(os.path.exists(self.index))
        self.assertIn(b'1', res.stderr)  # 行号

    def test_bad_field_count(self):
        res = self._build(b'a\t1\t2\t3\n')
        self.assertEqual(res.returncode, 1)
        self.assertFalse(os.path.exists(self.index))

    def test_word_with_space(self):
        res = self._build('a b\t1\n'.encode())
        self.assertEqual(res.returncode, 1)

    def test_empty_wordlist_ok(self):
        res = self._build(b'# only comments\n\n')
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.index, 'manifest.json'), encoding='utf-8') as fh:
            self.assertEqual(json.load(fh)['entry_count'], 0)

    def test_failed_build_leaves_no_trace(self):
        self.assertEqual(self._build(b'ok\t1\n').returncode, 0)
        res = self._build(b'bad\t0\n')  # 频率 0 非法
        self.assertEqual(res.returncode, 1)
        # 旧索引被保留还是移除均可，但不能留下半成品临时目录
        leftovers = [d for d in os.listdir(self.tmp) if d.startswith('.index-tmp-')]
        self.assertEqual(leftovers, [])


class EngineUnitTest(unittest.TestCase):
    def test_score_text_integer_format(self):
        index = engine.Index([], None, None, None, None)
        index.s100 = [1323000, 457320, 5, 100]
        self.assertEqual(index.score_text(0), '13230.00')
        self.assertEqual(index.score_text(1), '4573.20')
        self.assertEqual(index.score_text(2), '0.05')
        self.assertEqual(index.score_text(3), '1.00')

    def test_prefix_upper_bound(self):
        self.assertEqual(engine._prefix_upper_bound('ab'), 'ac')
        self.assertEqual(engine._prefix_upper_bound('a\U0010FFFF'), 'b')
        self.assertIsNone(engine._prefix_upper_bound('\U0010FFFF'))

    def test_parse_words_bom_crlf_duplicates(self):
        with tempfile.NamedTemporaryFile('wb', suffix='.txt', delete=False) as fh:
            fh.write(b'\xef\xbb\xbf# comment\r\napple\t5\t2\r\napple\t9\t9\n\n')
            path = fh.name
        self.addCleanup(os.unlink, path)
        entries = engine.parse_words(path)
        self.assertEqual(entries, {'apple': (5, 2)})

    def test_parse_queries_validates_all_first(self):
        with tempfile.NamedTemporaryFile('wb', suffix='.txt', delete=False) as fh:
            fh.write(b'a\t1\nb\tx\n')
            path = fh.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(SystemExit) as ctx:
            engine.parse_queries(path)
        self.assertEqual(ctx.exception.code, 1)

    def test_complete_total_order(self):
        words = ['a', 'ab', 'b']
        # a: S100 大但频率小；ab: S100 相同、频率相同 -> 比长度与码点
        entries = {'a': (10, 0), 'ab': (10, 0), 'b': (10, 0)}
        with tempfile.TemporaryDirectory() as tmp:
            wpath = os.path.join(tmp, 'w.txt')
            with open(wpath, 'w', encoding='utf-8') as fh:
                for w, (f, wt) in entries.items():
                    fh.write('%s\t%d\t%d\n' % (w, f, wt))
            engine.build_index(wpath, os.path.join(tmp, 'idx'))
            index = engine.Index.load(os.path.join(tmp, 'idx'))
            ids = index.complete('', 3)
            # a: 10*100*101=101000; ab: 10*100*102=102000; b 同 a 的 S100 但词码点更大
            self.assertEqual([index.words[i] for i in ids], ['ab', 'a', 'b'])
            self.assertEqual(index.complete('', 0), [])
            self.assertEqual(index.complete('zzz', 5), [])


class BenchTest(unittest.TestCase):
    def test_bench_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, 'idx')
            words = os.path.join(SAMPLES, 'words', 'basic.txt')
            queries = os.path.join(SAMPLES, 'queries', 'basic.txt')
            self.assertEqual(run_cli('build', '--words', words, '--index', index).returncode, 0)
            res = run_cli('bench', '--index', index, '--queries', queries, '--repeat', '5')
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout.decode())
            for key in ('queries', 'repeat', 'p50_ms', 'p95_ms', 'p99_ms', 'rss_mb'):
                self.assertIn(key, data)
            self.assertEqual(data['queries'], 11)
            self.assertEqual(data['repeat'], 5)


class ServeTest(unittest.TestCase):
    def test_api_complete_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, 'idx')
            words = os.path.join(SAMPLES, 'words', 'basic.txt')
            self.assertEqual(run_cli('build', '--words', words, '--index', index).returncode, 0)
            proc = subprocess.Popen(
                [sys.executable, CLI, 'serve', '--index', index, '--port', '18923'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen('http://127.0.0.1:18923/api/stats', timeout=1)
                        break
                    except OSError:
                        import time
                        time.sleep(0.05)
                stats = json.load(urllib.request.urlopen(
                    'http://127.0.0.1:18923/api/stats', timeout=5))
                self.assertEqual(stats['entry_count'], 14)
                self.assertIn('build_seconds', stats)
                self.assertIn('build_rss_mb', stats)

                resp = urllib.request.urlopen(
                    'http://127.0.0.1:18923/api/complete?prefix=app&k=3', timeout=5)
                data = json.load(resp)
                self.assertEqual(resp.headers['Content-Type'], 'application/json; charset=utf-8')
                self.assertEqual(data['k'], 3)
                self.assertIn('elapsed_ms', data)
                self.assertEqual(
                    [(c['word'], c['score']) for c in data['candidates']],
                    [('apple', '13230.00'), ('apply', '9450.00'), ('application', '4573.20')])

                # 与命令行输出逐字段一致
                cli_out = subprocess.run(
                    [sys.executable, CLI, 'query', '--index', index,
                     '--queries', os.path.join(SAMPLES, 'queries', 'basic.txt')],
                    capture_output=True).stdout.decode()
                line = next(l for l in cli_out.splitlines() if l.startswith('app\t3\t1\t'))
                self.assertIn('apple\t13230.00', line)

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(
                        'http://127.0.0.1:18923/api/complete?prefix=app&k=x', timeout=5)
                self.assertEqual(ctx.exception.code, 400)
                body = json.loads(ctx.exception.read())
                ctx.exception.close()
                self.assertIn('error', body)

                # 静态页
                resp = urllib.request.urlopen('http://127.0.0.1:18923/', timeout=5)
                self.assertIn(b'<html', resp.read())
            finally:
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
