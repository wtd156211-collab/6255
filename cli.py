#!/usr/bin/env python3
"""前缀补全引擎：词表解析、索引构建与落盘、前缀查询、基准与 HTTP 服务。

只用 Python 标准库。索引布局（索引目录内）：

- manifest.json : 版本、词条数、词表 SHA-256、构建耗时与内存等元信息；
- words.bin     : 全部词按码点升序排列，LF 分隔的 UTF-8 字节流；
- s100.bin      : 与词一一对应的 uint64 数组，值为 S100 = 频率*(100+权重)*(100+L)；
- rank.bin      : 与词一一对应的 uint32 数组，值为该词在全局排名中的名次（0 起）；
- top.bin       : uint32 数组，全局排名前 1000 名的词下标（空前缀直接取用）。

查询：对 words 二分定位前缀区间 [lo, hi)，再在区间内按 rank 取前 K，
定位代价 O(log n)，候选收集 O(m log K)，不遍历整份词表。
"""

import argparse
import array
import bisect
import hashlib
import heapq
import json
import math
import os
import resource
import shutil
import sys
import tempfile
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = 1
MAX_CODEPOINTS = 4096
MAX_K = 1000
TOP_CACHE = 1000  # 空前缀预计算的全局排名条数（= K 上限）
MAX_FREQ = 10 ** 9
MAX_WEIGHT = 10 ** 5


# ---------------------------------------------------------------- 输入解析

def _is_uint(text):
    return bool(text) and all('0' <= ch <= '9' for ch in text)


def _input_error(path, lineno, message):
    sys.stderr.write('%s:%d: %s\n' % (path, lineno, message))
    raise SystemExit(1)


def _check_token(token, what, path, lineno):
    if not token:
        _input_error(path, lineno, '%s为空' % what)
    if len(token) > MAX_CODEPOINTS:
        _input_error(path, lineno, '%s超过 %d 码点' % (what, MAX_CODEPOINTS))
    for ch in token:
        if ch.isspace():
            _input_error(path, lineno, '%s含空白字符' % what)


def parse_words(path):
    """解析词表，返回 {词: (频率, 权重)}，重复词按首次出现计。"""
    entries = {}
    try:
        fh = open(path, 'r', encoding='utf-8-sig', newline='')
    except OSError as exc:
        sys.stderr.write('%s: 无法读取词表: %s\n' % (path, exc))
        raise SystemExit(1)
    with fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip()
            if not line:
                continue
            if line.lstrip().startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) not in (2, 3):
                _input_error(path, lineno, '字段数为 %d，应为 2 或 3' % len(fields))
            word = fields[0]
            _check_token(word, '词', path, lineno)
            if not _is_uint(fields[1]) or not (1 <= int(fields[1]) <= MAX_FREQ):
                _input_error(path, lineno, '频率非法: %r' % fields[1])
            freq = int(fields[1])
            weight = 0
            if len(fields) == 3:
                if not _is_uint(fields[2]) or not (0 <= int(fields[2]) <= MAX_WEIGHT):
                    _input_error(path, lineno, '权重非法: %r' % fields[2])
                weight = int(fields[2])
            if word not in entries:
                entries[word] = (freq, weight)
    return entries


def parse_queries(path):
    """解析查询清单，整体校验通过后返回 [(前缀, K), ...]。"""
    queries = []
    try:
        fh = open(path, 'r', encoding='utf-8-sig', newline='')
    except OSError as exc:
        sys.stderr.write('%s: 无法读取查询清单: %s\n' % (path, exc))
        raise SystemExit(1)
    with fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip()
            if not line:
                continue
            if line.lstrip().startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) != 2:
                _input_error(path, lineno, '字段数为 %d，应为 2' % len(fields))
            prefix, ktext = fields
            if prefix:
                _check_token(prefix, '前缀', path, lineno)
            if not _is_uint(ktext) or not (0 <= int(ktext) <= MAX_K):
                _input_error(path, lineno, 'K 非法: %r' % ktext)
            queries.append((prefix, int(ktext)))
    return queries


# ---------------------------------------------------------------- 索引

class Index:
    """只读索引：词按码点升序，配套 S100 与全局名次数组。"""

    def __init__(self, words, s100, rank, top, manifest):
        self.words = words
        self.s100 = s100
        self.rank = rank
        self.top = top
        self.manifest = manifest

    @classmethod
    def load(cls, index_dir):
        manifest_path = os.path.join(index_dir, 'manifest.json')
        try:
            with open(manifest_path, 'r', encoding='utf-8') as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            sys.stderr.write('%s: 索引不可用（manifest.json 缺失或损坏）\n' % index_dir)
            raise SystemExit(2)
        if manifest.get('version') != VERSION:
            sys.stderr.write('%s: 索引版本不匹配（期望 %d）\n' % (index_dir, VERSION))
            raise SystemExit(2)
        try:
            with open(os.path.join(index_dir, 'words.bin'), 'rb') as fh:
                blob = fh.read()
            words = blob.decode('utf-8').split('\n') if blob else []
            s100 = array.array('Q')
            rank = array.array('I')
            top = array.array('I')
            with open(os.path.join(index_dir, 's100.bin'), 'rb') as fh:
                s100.frombytes(fh.read())
            with open(os.path.join(index_dir, 'rank.bin'), 'rb') as fh:
                rank.frombytes(fh.read())
            with open(os.path.join(index_dir, 'top.bin'), 'rb') as fh:
                top.frombytes(fh.read())
        except (OSError, ValueError) as exc:
            sys.stderr.write('%s: 索引数据损坏: %s\n' % (index_dir, exc))
            raise SystemExit(2)
        if len(words) != len(s100) or len(words) != len(rank):
            sys.stderr.write('%s: 索引数据不一致\n' % index_dir)
            raise SystemExit(2)
        return cls(words, s100, rank, top, manifest)

    def complete(self, prefix, k):
        """返回按排名升序的词下标列表，至多 k 个。"""
        if k <= 0 or not self.words:
            return []
        if prefix == '':
            return list(self.top[:k])
        words = self.words
        lo = bisect.bisect_left(words, prefix)
        upper = _prefix_upper_bound(prefix)
        hi = len(words) if upper is None else bisect.bisect_left(words, upper)
        if lo >= hi:
            return []
        return heapq.nsmallest(k, range(lo, hi), key=self.rank.__getitem__)

    def score_text(self, i):
        s = self.s100[i]
        return '%d.%02d' % (s // 100, s % 100)


def _prefix_upper_bound(prefix):
    """大于所有以 prefix 开头的字符串的最小串；不存在时返回 None。"""
    i = len(prefix) - 1
    while i >= 0 and ord(prefix[i]) == 0x10FFFF:
        i -= 1
    if i < 0:
        return None
    return prefix[:i] + chr(ord(prefix[i]) + 1)


def build_index(words_path, index_dir):
    started = time.perf_counter()
    entries = parse_words(words_path)

    words = sorted(entries)
    n = len(words)
    s100 = array.array('Q', bytes(8 * n))
    freq_arr = array.array('I', bytes(4 * n))
    for i, word in enumerate(words):
        freq, weight = entries[word]
        freq_arr[i] = freq
        s100[i] = freq * (100 + weight) * (100 + len(word))

    # 全序：S100 降序 → 频率降序 → L 升序 → 词按码点升序
    order = sorted(
        range(n),
        key=lambda i: (-s100[i], -freq_arr[i], len(words[i]), words[i]),
    )
    rank = array.array('I', bytes(4 * n))
    for pos, i in enumerate(order):
        rank[i] = pos
    top = array.array('I', order[:TOP_CACHE])

    with open(words_path, 'rb') as fh:
        source_sha256 = hashlib.sha256(fh.read()).hexdigest()

    build_seconds = time.perf_counter() - started
    manifest = {
        'version': VERSION,
        'entry_count': n,
        'source_sha256': source_sha256,
        'build_seconds': round(build_seconds, 6),
        'build_rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3),
        'files': ['words.bin', 's100.bin', 'rank.bin', 'top.bin'],
    }

    # 原子落盘：先写临时目录，成功后整体替换；失败不留痕迹
    parent = os.path.dirname(os.path.abspath(index_dir)) or '.'
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='.index-tmp-', dir=parent)
    try:
        with open(os.path.join(tmp, 'words.bin'), 'wb') as fh:
            fh.write('\n'.join(words).encode('utf-8'))
        with open(os.path.join(tmp, 's100.bin'), 'wb') as fh:
            fh.write(s100.tobytes())
        with open(os.path.join(tmp, 'rank.bin'), 'wb') as fh:
            fh.write(rank.tobytes())
        with open(os.path.join(tmp, 'top.bin'), 'wb') as fh:
            fh.write(top.tobytes())
        with open(os.path.join(tmp, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
        if os.path.exists(index_dir):
            shutil.rmtree(index_dir)
        os.replace(tmp, index_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest


# ---------------------------------------------------------------- 输出

def render_results(index, queries):
    lines = []
    for prefix, k in queries:
        ids = index.complete(prefix, k)
        if not ids:
            lines.append('%s\t%d\t0\t-\t-' % (prefix, k))
            continue
        for seq, i in enumerate(ids, 1):
            lines.append('%s\t%d\t%d\t%s\t%s' % (
                prefix, k, seq, index.words[i], index.score_text(i)))
    return ('\n'.join(lines) + '\n').encode('utf-8')


# ---------------------------------------------------------------- 子命令

def cmd_build(args):
    manifest = build_index(args.words, args.index)
    sys.stderr.write('索引构建完成: %d 条，耗时 %.3f s -> %s\n' % (
        manifest['entry_count'], manifest['build_seconds'], args.index))
    return 0


def cmd_query(args):
    queries = parse_queries(args.queries)  # 先整体校验，再载入索引执行
    index = Index.load(args.index)
    sys.stdout.buffer.write(render_results(index, queries))
    return 0


def _percentile(sorted_values, q):
    # 最近秩分位
    if not sorted_values:
        return 0.0
    pos = max(0, math.ceil(q / 100 * len(sorted_values)) - 1)
    return sorted_values[pos]


def cmd_bench(args):
    queries = parse_queries(args.queries)
    index = Index.load(args.index)
    # 计时口径：不含索引加载；先跑一轮预热（不计时），再逐条计时 repeat 轮；
    # 每条查询分别取最近秩分位，汇总时取所有查询的最差值。
    for prefix, k in queries:
        index.complete(prefix, k)
    latencies = [[] for _ in queries]
    for _ in range(args.repeat):
        for qi, (prefix, k) in enumerate(queries):
            t0 = time.perf_counter_ns()
            index.complete(prefix, k)
            latencies[qi].append((time.perf_counter_ns() - t0) / 1e6)
    for lat in latencies:
        lat.sort()
    result = {
        'queries': len(queries),
        'repeat': args.repeat,
        'p50_ms': round(max((_percentile(l, 50) for l in latencies), default=0.0), 3),
        'p95_ms': round(max((_percentile(l, 95) for l in latencies), default=0.0), 3),
        'p99_ms': round(max((_percentile(l, 99) for l in latencies), default=0.0), 3),
        'rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3),
    }
    sys.stdout.write(json.dumps(result) + '\n')
    return 0


def cmd_serve(args):
    index = Index.load(args.index)
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=web_dir, **kw)

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/complete':
                self._handle_complete()
            elif path == '/api/stats':
                self._send_json(200, index.manifest)
            else:
                super().do_GET()

        def _handle_complete(self):
            params = parse_qs(urlparse(self.path).query)
            prefix = params.get('prefix', [''])[0]
            ktext = params.get('k', ['10'])[0]
            if not _is_uint(ktext) or not (0 <= int(ktext) <= MAX_K):
                self._send_json(400, {'error': 'k 非法：应为 0..%d 的十进制整数' % MAX_K})
                return
            if len(prefix) > MAX_CODEPOINTS or any(c.isspace() for c in prefix):
                self._send_json(400, {'error': 'prefix 非法：含空白字符或超过 %d 码点' % MAX_CODEPOINTS})
                return
            k = int(ktext)
            t0 = time.perf_counter_ns()
            ids = index.complete(prefix, k)
            elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
            self._send_json(200, {
                'k': k,
                'prefix': prefix,
                'elapsed_ms': round(elapsed_ms, 3),
                'candidates': [
                    {'word': index.words[i], 'score': index.score_text(i)}
                    for i in ids
                ],
            })

        def log_message(self, fmt, *fmt_args):
            sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % fmt_args))

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    sys.stderr.write('HTTP 服务已启动: http://127.0.0.1:%d/\n' % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='前缀补全引擎')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('build', help='构建索引并落盘')
    p.add_argument('--words', required=True)
    p.add_argument('--index', required=True)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser('query', help='按查询清单输出候选')
    p.add_argument('--index', required=True)
    p.add_argument('--queries', required=True)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser('bench', help='基准测试，输出一行 JSON')
    p.add_argument('--index', required=True)
    p.add_argument('--queries', required=True)
    p.add_argument('--repeat', type=int, default=100)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser('serve', help='托管 web/ 与 /api 接口')
    p.add_argument('--index', required=True)
    p.add_argument('--port', type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
