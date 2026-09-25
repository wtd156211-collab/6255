"""词表与查询清单的解析（口径见 README 第二、四节）。"""

from .scoring import MAX_FREQ, MAX_K, MAX_WEIGHT, MAX_WORD_CODEPOINTS


class InputError(ValueError):
    """非法输入：错误信息带文件与行号，退出码 1。"""


def _iter_lines(raw: bytes):
    """解码并逐行产出 (行号, 行文本，已去掉行尾空白，不含换行符)。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8")
    return text.split("\n")


def _clean_line(line: str) -> str:
    # CRLF：去掉 CR；再统一去掉行尾空白。行内/行首空白保留以便判非法。
    return line.rstrip()


def _parse_uint(field: str) -> int:
    if not field or not field.isascii() or not field.isdigit():
        raise ValueError("不是纯十进制数字")
    return int(field)


def parse_words(raw: bytes, source: str):
    """解析词表字节，返回 [(word, freq, weight), ...]，保持首次出现顺序。"""
    entries = []
    seen = set()
    lines = _iter_lines(raw)
    for lineno, line in enumerate(lines, 1):
        stripped = _clean_line(line)
        if stripped == "":
            continue
        if stripped.lstrip().startswith("#"):
            continue
        fields = stripped.split("\t")
        if len(fields) not in (2, 3):
            raise InputError(
                f"{source}:{lineno}: 字段数只能是 2 或 3（实际 {len(fields)}）"
            )
        word, freq_s = fields[0], fields[1]
        weight_s = fields[2] if len(fields) == 3 else "0"
        if word == "":
            raise InputError(f"{source}:{lineno}: 词不能为空")
        if any(ch.isspace() for ch in word):
            raise InputError(f"{source}:{lineno}: 词不能含空白字符: {word!r}")
        if len(word) > MAX_WORD_CODEPOINTS:
            raise InputError(
                f"{source}:{lineno}: 词码点数 {len(word)} 超过 {MAX_WORD_CODEPOINTS}"
            )
        try:
            freq = _parse_uint(freq_s)
        except ValueError:
            raise InputError(f"{source}:{lineno}: 非法频率: {freq_s!r}")
        if not 1 <= freq <= MAX_FREQ:
            raise InputError(f"{source}:{lineno}: 频率超出范围 1..10^9: {freq}")
        try:
            weight = _parse_uint(weight_s)
        except ValueError:
            raise InputError(f"{source}:{lineno}: 非法权重: {weight_s!r}")
        if not 0 <= weight <= MAX_WEIGHT:
            raise InputError(f"{source}:{lineno}: 权重超出范围 0..10^5: {weight}")
        if word in seen:
            # 重复词：整行（已通过校验）按首次出现计，直接忽略。
            continue
        seen.add(word)
        entries.append((word, freq, weight))
    return entries


def parse_queries(raw: bytes, source: str):
    """解析查询清单，返回 [(prefix, k), ...]。先整体校验后再由调用方执行。"""
    queries = []
    lines = _iter_lines(raw)
    for lineno, line in enumerate(lines, 1):
        stripped = _clean_line(line)
        if stripped == "":
            continue
        if stripped.lstrip().startswith("#"):
            continue
        fields = stripped.split("\t")
        if len(fields) != 2:
            raise InputError(
                f"{source}:{lineno}: 查询行必须是 '前缀<TAB>K'（实际 {len(fields)} 字段）"
            )
        prefix, k_s = fields
        if any(ch.isspace() for ch in prefix):
            raise InputError(f"{source}:{lineno}: 前缀不能含空白字符: {prefix!r}")
        if len(prefix) > MAX_WORD_CODEPOINTS:
            raise InputError(
                f"{source}:{lineno}: 前缀码点数 {len(prefix)} 超过 "
                f"{MAX_WORD_CODEPOINTS}"
            )
        try:
            k = _parse_uint(k_s)
        except ValueError:
            raise InputError(f"{source}:{lineno}: 非法 K: {k_s!r}")
        if not 0 <= k <= MAX_K:
            raise InputError(f"{source}:{lineno}: K 超出范围 0..1000: {k}")
        queries.append((prefix, k))
    return queries
