"""评分与确定性排序口径。

分数按整数运算定义：
    S100 = 频率 × (100 + 权重) × (100 + L)
其中 L 为词的 Unicode 码点数；展示值 S = S100 / 100，固定两位小数，
直接由整数商与余数格式化，不经过浮点。

排序为全序：S100 降序 → 频率降序 → L 升序 → 词按码点升序。
"""

MAX_WORD_CODEPOINTS = 4096
MAX_FREQ = 10**9
MAX_WEIGHT = 10**5
MAX_K = 1000
EMPTY_PREFIX_K = 1000


def score100(freq: int, weight: int, length: int) -> int:
    return freq * (100 + weight) * (100 + length)


def format_score(s100: int) -> str:
    """把整数分 S100 格式化为 S=S100/100 的两位小数字符串。"""
    return f"{s100 // 100}.{s100 % 100:02d}"


def rank_key(s100: int, freq: int, length: int, word: str):
    """越大越靠前的全序键。词序由外层按码点升序单独处理。"""
    return (s100, freq, -length, word)
