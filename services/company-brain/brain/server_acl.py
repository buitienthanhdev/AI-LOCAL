"""Quyền đọc dùng chung cho mọi module (tách riêng để không vòng import)."""
from .config import ADMIN_GROUP


def can_read(acl: str, groups: list[str]) -> bool:
    """acl: danh sách nhóm được đọc, cách nhau bằng dấu phẩy ('all' = mọi người, 'private' = chỉ admin)."""
    return ADMIN_GROUP in groups or bool(set((acl or "private").split(",")) & {*groups, "all"})


def acl_sql(groups: list[str], column: str = "acl") -> tuple[str, list[str]]:
    """Điều kiện SQL tương đương can_read (cho truy vấn lọc ngay trong DB)."""
    if ADMIN_GROUP in groups:
        return "1=1", []
    gs = sorted({*groups, "all"})
    return "(" + " OR ".join(f"(',' || {column} || ',') LIKE ?" for _ in gs) + ")", [f"%,{g},%" for g in gs]
