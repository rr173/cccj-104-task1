"""语义化版本比较：'3.10.1' -> (3, 10, 1)，非数字后缀截断，长度不齐补零。"""
from __future__ import annotations


def parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in v.strip().split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def version_gte(a: str, b: str) -> bool:
    x, y = _pad(parse_version(a), parse_version(b))
    return x >= y


def version_eq(a: str, b: str) -> bool:
    x, y = _pad(parse_version(a), parse_version(b))
    return x == y
