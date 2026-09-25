#!/usr/bin/env python3
"""命令行入口：build / query / bench / serve（仅标准库）。"""

import argparse
import hashlib
import json
import sys
import time

from engine.index import IndexError_, IndexReader, build_index
from engine.scoring import format_score
from engine.wordsio import InputError, parse_queries, parse_words

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_USAGE = 2


def _read_bytes(path: str, what: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise InputError(f"无法读取{what} {path}: {exc}")


def _open_reader(index_dir: str) -> IndexReader:
    try:
        return IndexReader(index_dir)
    except IndexError_ as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def cmd_build(args) -> int:
    raw = _read_bytes(args.words, "词表")
    source_sha = hashlib.sha256(raw).hexdigest()
    try:
        entries = parse_words(raw, args.words)
        manifest = build_index(entries, source_sha, args.index)
    except InputError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    print(
        f"构建完成：{manifest['entry_count']} 条，"
        f"耗时 {manifest['build_ms']} ms，索引 {manifest['index_bytes']} 字节"
    )
    return EXIT_OK


def _load_queries(path: str):
    raw = _read_bytes(path, "查询清单")
    return parse_queries(raw, path)


def _render(queries, reader: IndexReader) -> bytes:
    out = []
    for prefix, k in queries:
        rows = reader.complete(prefix, k)
        if not rows:
            out.append(f"{prefix}\t{k}\t0\t-\t-")
            continue
        for seq, (word, s100) in enumerate(rows, 1):
            out.append(f"{prefix}\t{k}\t{seq}\t{word}\t{format_score(s100)}")
    return ("\n".join(out) + ("\n" if out else "")).encode("utf-8")


def cmd_query(args) -> int:
    reader = _open_reader(args.index)
    try:
        queries = _load_queries(args.queries)
    except InputError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    sys.stdout.buffer.write(_render(queries, reader))
    sys.stdout.buffer.flush()
    return EXIT_OK


def _percentile(sorted_values, pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100.0 * (len(sorted_values) - 1)
    low = int(rank)
    frac = rank - low
    if low + 1 >= len(sorted_values):
        return sorted_values[-1]
    return sorted_values[low] * (1 - frac) + sorted_values[low + 1] * frac


def cmd_bench(args) -> int:
    reader = _open_reader(args.index)
    try:
        queries = _load_queries(args.queries)
    except InputError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.repeat < 1:
        print("错误：--repeat 必须 ≥ 1", file=sys.stderr)
        return EXIT_USAGE

    # 加载后先预热一次（不计入统计），计时只覆盖查询本身。
    for prefix, k in queries:
        reader.complete(prefix, k)

    per_query_ms = [[] for _ in queries]
    for _ in range(args.repeat):
        for qi, (prefix, k) in enumerate(queries):
            started = time.perf_counter()
            reader.complete(prefix, k)
            per_query_ms[qi].append((time.perf_counter() - started) * 1000.0)

    if per_query_ms:
        p50 = max(_percentile(sorted(v), 50) for v in per_query_ms)
        p95 = max(_percentile(sorted(v), 95) for v in per_query_ms)
        p99 = max(_percentile(sorted(v), 99) for v in per_query_ms)
    else:
        p50 = p95 = p99 = 0.0

    try:
        import resource

        rss_mb = round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2
        )
    except Exception:
        rss_mb = None

    sys.stdout.buffer.write(
        (
            json.dumps(
                {
                    "queries": len(queries),
                    "repeat": args.repeat,
                    "p50_ms": round(p50, 4),
                    "p95_ms": round(p95, 4),
                    "p99_ms": round(p99, 4),
                    "rss_mb": rss_mb,
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    return EXIT_OK


def cmd_serve(args) -> int:
    import os

    from engine.server import serve

    reader = _open_reader(args.index)
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    if not os.path.isdir(web_dir):
        print(f"错误：web 目录不存在: {web_dir}", file=sys.stderr)
        return EXIT_USAGE
    httpd = serve(reader, web_dir, args.port)
    host, port = httpd.server_address[:2]
    print(f"服务已启动：http://{host}:{port}/ （Ctrl-C 停止）", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="前缀索引与候选排序")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="从词表构建索引")
    p_build.add_argument("--words", required=True)
    p_build.add_argument("--index", required=True)
    p_build.set_defaults(func=cmd_build)

    p_query = sub.add_parser("query", help="按查询清单输出候选")
    p_query.add_argument("--index", required=True)
    p_query.add_argument("--queries", required=True)
    p_query.set_defaults(func=cmd_query)

    p_bench = sub.add_parser("bench", help="热查询延迟与内存")
    p_bench.add_argument("--index", required=True)
    p_bench.add_argument("--queries", required=True)
    p_bench.add_argument("--repeat", type=int, required=True)
    p_bench.set_defaults(func=cmd_bench)

    p_serve = sub.add_parser("serve", help="HTTP 接口与页面")
    p_serve.add_argument("--index", required=True)
    p_serve.add_argument("--port", type=int, required=True)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
