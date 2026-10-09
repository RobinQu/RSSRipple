"""应用层唯一键字节护栏。

PostgreSQL btree 索引单行上限约 2704 字节；组成唯一键的 VARCHAR 列允许的
字符数（如 VARCHAR(1024)）在全 CJK（UTF-8 三字节）时会超限，INSERT 直接
报 btree 错误。写入路径在 ORM 层先拒绝超长值（ValueError，由调用方映射
为 422），避免裸数据库错误。
"""

MAX_UNIQUE_KEY_BYTES = 2000
"""唯一键各列 UTF-8 字节总预算（btree 上限约 2704，预留内部开销余量）。"""


def utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def check_unique_key_bytes(column: str, value: str | None, *, reserved_bytes: int) -> None:
    """Reject a value that would overflow the PostgreSQL btree row limit.

    ``reserved_bytes`` accounts for the other columns of the composite unique
    key plus index-entry overhead, so the effective budget for ``value`` is
    ``MAX_UNIQUE_KEY_BYTES - reserved_bytes``.
    """
    if value is None:
        return
    budget = MAX_UNIQUE_KEY_BYTES - reserved_bytes
    size = utf8_size(value)
    if size > budget:
        raise ValueError(
            f"{column} 超长：UTF-8 编码后 {size} 字节，超出唯一键字节预算 "
            f"{budget}（总预算 {MAX_UNIQUE_KEY_BYTES}，已为其它键列与索引开销预留 "
            f"{reserved_bytes} 字节）；请缩短该值"
        )
