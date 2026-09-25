"""索引的构建、落盘与只读查询。

磁盘布局（索引目录，全部相对路径，可整体搬移）：

- ``manifest.json``：UTF-8 JSON。存在即代表索引可用，因此最后原子替换。
- ``words.bin``：8 字节魔数 ``PPIXWRD1`` 后，按码点序拼接的全部词 UTF-8 字节。
- ``index.bin``：8 字节魔数 ``PPIXIDX1`` 后为定长记录（小端，24 字节）：
  ``S100 uint64 | 词字节偏移 uint32 | 频率 uint32 | 权重 uint32
  | UTF-8 字节数 uint16 | 码点数 uint16``，顺序与词码点序一致。
- ``top.bin``：8 字节魔数 ``PPIXTOP1``，随后条目数 uint32，
  再为按全局排名降序排列的条目下标 uint32（至多 1000，构建期算好）。

查询不做前缀展开：排序词表上二分得到候选区间 O(log n)，区间内用大小为 K
的堆取前 K，O(m log K)；内存只有「词表本身 + 定长记录」，没有前缀表/trie。
"""

import hashlib
import heapq
import json
import os
import struct
import time

from .scoring import EMPTY_PREFIX_K, score100

MANIFEST_NAME = "manifest.json"
WORDS_NAME = "words.bin"
INDEX_NAME = "index.bin"
TOP_NAME = "top.bin"

WORDS_MAGIC = b"PPIXWRD1"
INDEX_MAGIC = b"PPIXIDX1"
TOP_MAGIC = b"PPIXTOP1"
INDEX_VERSION = 1

_RECORD = struct.Struct("<QIIIHH")  # s100, off, freq, weight, nbytes, length
_U32 = struct.Struct("<I")
_MAX_CODEPOINT = 0x10FFFF


class IndexError_(Exception):
    """索引不可用或版本不匹配（退出码 2）。"""


class _InvStr:
    """反向字符串：词按码点序的反转比较（用于堆上的全序键）。"""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return self.value == other.value


def _prefix_upper(prefix: str):
    """严格大于所有以 prefix 开头字符串的最小上界；不存在时返回 None。"""
    chars = list(prefix)
    for i in range(len(chars) - 1, -1, -1):
        cp = ord(chars[i])
        if cp < _MAX_CODEPOINT:
            chars[i] = chr(cp + 1)
            return "".join(chars[: i + 1])
    return None


def build_index(entries, source_sha256: str, dest_dir: str):
    """entries: [(word, freq, weight), ...]（保持词表首次出现顺序）。

    成功后索引目录完整可用；失败不在目标位置留下可用索引。
    返回写入的 manifest 字典（含构建耗时等附加字段）。
    """
    started = time.perf_counter()

    ordered = sorted(entries, key=lambda e: e[0])
    n = len(ordered)

    words_blob = bytearray(WORDS_MAGIC)
    records = bytearray(INDEX_MAGIC)
    offset = 0
    s100_list = []
    length_list = []
    for word, freq, weight in ordered:
        encoded = word.encode("utf-8")
        length = len(word)
        s100 = score100(freq, weight, length)
        words_blob.extend(encoded)
        records += _RECORD.pack(s100, offset, freq, weight, len(encoded), length)
        offset += len(encoded)
        s100_list.append(s100)
        length_list.append(length)

    def heap_key(i):
        return (
            s100_list[i],
            ordered[i][1],
            -length_list[i],
            _InvStr(ordered[i][0]),
        )

    top = heapq.nlargest(min(EMPTY_PREFIX_K, n), range(n), key=heap_key)
    top_blob = TOP_MAGIC + _U32.pack(len(top))
    top_blob += b"".join(_U32.pack(i) for i in top)

    index_bytes_total = (
        len(words_blob) + len(records) + len(top_blob) + 200
    )
    manifest = {
        "version": INDEX_VERSION,
        "entry_count": n,
        "source_sha256": source_sha256,
        "index_format": {
            "words_magic": WORDS_MAGIC.decode("ascii"),
            "index_magic": INDEX_MAGIC.decode("ascii"),
            "top_magic": TOP_MAGIC.decode("ascii"),
            "record_size": _RECORD.size,
            "top_k": EMPTY_PREFIX_K,
        },
        "word_bytes": offset,
        "index_bytes": index_bytes_total,
    }

    files = {
        WORDS_NAME: bytes(words_blob),
        INDEX_NAME: bytes(records),
        TOP_NAME: top_blob,
    }

    # 以下开始落盘：所有计算均已完成，写盘阶段不会再因词表内容失败。
    os.makedirs(os.path.dirname(os.path.abspath(dest_dir)) or ".", exist_ok=True)
    parent = os.path.dirname(os.path.abspath(dest_dir))

    if os.path.isdir(dest_dir):
        # 原地更新：每个文件先写临时文件再原子替换，manifest 最后落盘。
        for name, blob in files.items():
            _atomic_write(os.path.join(dest_dir, name), blob)
        keep = set(files) | {MANIFEST_NAME}
        for name in os.listdir(dest_dir):
            if name not in keep:
                try:
                    os.remove(os.path.join(dest_dir, name))
                except OSError:
                    pass
        target_dir = dest_dir
    else:
        tmp_dir = os.path.join(
            parent, f".{os.path.basename(dest_dir)}.tmp-{os.getpid()}"
        )
        if os.path.exists(tmp_dir):
            _rm_tree(tmp_dir)
        os.mkdir(tmp_dir)
        for name, blob in files.items():
            _atomic_write(os.path.join(tmp_dir, name), blob)
        os.replace(tmp_dir, dest_dir)
        target_dir = dest_dir

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    manifest["build_ms"] = round(elapsed_ms, 3)
    try:
        import resource

        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        manifest["build_rss_mb"] = round(rss_kb / 1024.0, 2)
    except Exception:
        manifest["build_rss_mb"] = None
    manifest["index_bytes"] = _dir_size(target_dir)

    _atomic_write(
        os.path.join(target_dir, MANIFEST_NAME),
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return manifest


def _atomic_write(path: str, blob: bytes):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _dir_size(path: str) -> int:
    total = 0
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            total += os.path.getsize(full)
    return total


def _rm_tree(path: str):
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isdir(full):
            _rm_tree(full)
        else:
            os.remove(full)
    os.rmdir(path)


class IndexReader:
    """只读索引：载入后不再触碰词表，目录可搬到任意位置。"""

    def __init__(self, index_dir: str):
        self.dir = index_dir
        manifest_path = os.path.join(index_dir, MANIFEST_NAME)
        try:
            with open(manifest_path, "rb") as fh:
                self.manifest = json.loads(fh.read().decode("utf-8"))
        except FileNotFoundError:
            raise IndexError_(f"索引不可用：缺少 {MANIFEST_NAME}: {index_dir}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IndexError_(f"索引不可用：{manifest_path}: {exc}")
        if self.manifest.get("version") != INDEX_VERSION:
            raise IndexError_(
                f"索引版本不匹配：期望 {INDEX_VERSION}，"
                f"实际 {self.manifest.get('version')!r}"
            )

        self.words_blob = self._read_magic(WORDS_NAME, WORDS_MAGIC)
        rec_blob = self._read_magic(INDEX_NAME, INDEX_MAGIC)
        top_blob = self._read_magic(TOP_NAME, TOP_MAGIC)

        n = self.manifest["entry_count"]
        if len(rec_blob) != n * _RECORD.size:
            raise IndexError_("索引损坏：index.bin 记录数与 manifest 不一致")
        self._rec = rec_blob
        self._n = n

        self.words = [None] * n
        for i in range(n):
            off = _RECORD.unpack_from(rec_blob, i * _RECORD.size)[1]
            nbytes = _RECORD.unpack_from(rec_blob, i * _RECORD.size)[4]
            self.words[i] = self.words_blob[off : off + nbytes].decode("utf-8")

        top_count = _U32.unpack_from(top_blob, 0)[0]
        self._top = [
            _U32.unpack_from(top_blob, 4 + 4 * i)[0] for i in range(top_count)
        ]

    def _read_magic(self, name: str, magic: bytes) -> bytes:
        path = os.path.join(self.dir, name)
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise IndexError_(f"索引不可用：读取 {path} 失败: {exc}")
        if len(blob) < len(magic) or blob[: len(magic)] != magic:
            raise IndexError_(f"索引损坏：{name} 魔数不匹配")
        return blob[len(magic) :]

    @property
    def entry_count(self) -> int:
        return self._n

    def _record(self, i: int):
        s100, _off, freq, _weight, _nbytes, length = _RECORD.unpack_from(
            self._rec, i * _RECORD.size
        )
        return s100, freq, length

    def complete(self, prefix: str, k: int):
        """返回 [(word, s100), ...]，已按 README 的全序排名、至多 k 条。"""
        if k <= 0:
            return []
        if prefix == "":
            return [
                (self.words[i], self._record(i)[0]) for i in self._top[:k]
            ]

        import bisect

        words = self.words
        lo = bisect.bisect_left(words, prefix)
        upper = _prefix_upper(prefix)
        hi = (
            self._n
            if upper is None
            else bisect.bisect_right(words, upper)
        )
        if lo >= hi:
            return []

        word_cache = {}

        def word_at(i):
            w = word_cache.get(i)
            if w is None:
                w = words[i]
                word_cache[i] = w
            return w

        def heap_key(i):
            s100, freq, length = self._record(i)
            return (s100, freq, -length, _InvStr(word_at(i)))

        ranked = heapq.nlargest(k, range(lo, hi), key=heap_key)
        return [(word_at(i), self._record(i)[0]) for i in ranked]
