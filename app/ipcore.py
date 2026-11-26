"""IPv4 前缀工具、最长前缀匹配、前缀等价类分解与逐跳仿真。

校验不能逐地址穷举：本模块把每条保通要求的目的网段，按所有路由表中出现过的
前缀边界切分成有限个“路由等价类”（canonical 前缀）。同一等价类内任意地址的
最长前缀匹配结果在每台设备上都相同，因此每个等价类只抽一个代表地址验证即可。
"""
from __future__ import annotations

from dataclasses import dataclass, field

Prefix = tuple[int, int]  # (network_addr_int, prefix_length)


class PrefixError(ValueError):
    pass


def parse_prefix(text: str) -> Prefix:
    if not isinstance(text, str) or "/" not in text:
        raise PrefixError(f"非法网段: {text!r}，应为 a.b.c.d/len 形式")
    addr_part, _, len_part = text.partition("/")
    octets = addr_part.split(".")
    if len(octets) != 4:
        raise PrefixError(f"非法 IPv4 地址: {addr_part!r}")
    try:
        addr = 0
        for o in octets:
            v = int(o)
            if not 0 <= v <= 255:
                raise ValueError
            addr = (addr << 8) | v
        length = int(len_part)
        if not 0 <= length <= 32:
            raise ValueError
    except ValueError:
        raise PrefixError(f"非法网段: {text!r}")
    if length == 0:
        net = 0
    else:
        net = addr & (0xFFFFFFFF << (32 - length)) & 0xFFFFFFFF
    if net != addr:
        raise PrefixError(f"网段 {text!r} 主机位非零，是否应为 {ip_str(net)}/{length}?")
    return net, length


def ip_str(addr: int) -> str:
    return ".".join(str((addr >> (8 * i)) & 0xFF) for i in (3, 2, 1, 0))


def prefix_str(p: Prefix) -> str:
    return f"{ip_str(p[0])}/{p[1]}"


def contains(outer: Prefix, inner: Prefix) -> bool:
    """outer 是否包含 inner（含相等）。"""
    on, ol = outer
    inn, il = inner
    if il < ol:
        return False
    if ol == 0:
        return True
    return (inn >> (32 - ol)) == (on >> (32 - ol))


def _children(p: Prefix) -> tuple[Prefix, Prefix]:
    net, length = p
    bit = 1 << (31 - length)
    return (net, length + 1), (net | bit, length + 1)


def decompose(prefix: Prefix, boundary: set[Prefix]) -> list[Prefix]:
    """把 prefix 按 boundary 中的前缀边界切成路由等价类。

    切分结果两两不相交、并集恰为 prefix；每个结果前缀要么自身在 boundary 中，
    要么内部不含任何 boundary 前缀——后者内部 LPM 结果恒定，整段等价。
    """
    if prefix[1] == 32:
        return [prefix]

    inside = [t for t in boundary if contains(prefix, t)]
    if not inside:
        return [prefix]

    out: list[Prefix] = []

    def rec(p: Prefix) -> None:
        # p 自身可能就是边界前缀，但若内部还有更深的边界前缀，仍须继续切分
        deeper = [t for t in inside if t != p and contains(p, t)]
        if not deeper or p[1] == 32:
            out.append(p)
            return
        left, right = _children(p)
        rec(left)
        rec(right)

    rec(prefix)
    return out


class RouteTableView:
    """某一换表状态下全网的路由视图。"""

    def __init__(
        self,
        effective: dict[str, dict[Prefix, str]],
        topology: dict[str, list[str]] | None,
    ):
        self.effective = effective
        self.topology = topology
        self.nodes = set(effective)

    def lpm(self, node: str, addr: int) -> tuple[Prefix | None, str | None]:
        table = self.effective.get(node)
        if table is None:
            return None, None
        best: Prefix | None = None
        for p in table:
            net, length = p
            if (length == 0 or (addr >> (32 - length)) == (net >> (32 - length))) and (
                best is None or length > best[1]
            ):
                best = p
        if best is None:
            return None, None
        return best, table[best]


@dataclass
class Hop:
    node: str
    matched_prefix: str | None
    next_hop: str | None
    note: str | None = None


@dataclass
class Witness:
    """无解证据：在某个换表状态下，对某个目的地址的逐跳路径与失效原因。"""

    switched: list[str]
    requirement_index: int
    dst_address: str
    dst_ec_prefix: str
    entry_router: str
    expected_exit: str
    reason: str  # loop | no_route | wrong_exit | no_link | first_hop_mismatch
    hops: list[Hop] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "switched_devices": self.switched,
            "requirement_index": self.requirement_index,
            "dst_address": self.dst_address,
            "dst_equivalence_class": self.dst_ec_prefix,
            "entry_router": self.entry_router,
            "expected_exit": self.expected_exit,
            "reason": self.reason,
            "hop_by_hop": [
                {
                    "node": h.node,
                    "matched_prefix": h.matched_prefix,
                    "next_hop": h.next_hop,
                    **({"note": h.note} if h.note else {}),
                }
                for h in self.hops
            ],
        }


def simulate(
    view: RouteTableView,
    entry: str,
    addr: int,
    expected_exit: str,
    max_hops: int = 64,
) -> tuple[bool, str | None, list[Hop]]:
    """从 entry 逐跳转发 addr，返回 (是否到达 expected_exit, 失效原因, 逐跳记录)。"""
    hops: list[Hop] = []
    if entry == expected_exit:
        return True, None, hops
    cur = entry
    seen: set[str] = set()
    for _ in range(max_hops):
        if cur in seen:
            # 环路闭合：重复节点已在 hops 中，仅补一条闭合标记
            hops.append(Hop(cur, None, None, note=f"回到已访问节点 {cur}，形成环路"))
            return False, "loop", hops
        seen.add(cur)
        matched, nh = view.lpm(cur, addr)
        if nh is None:
            hops.append(Hop(cur, prefix_str(matched) if matched else None, None))
            return False, "no_route", hops
        hops.append(Hop(cur, prefix_str(matched) if matched else None, nh))
        if nh == expected_exit:
            return True, None, hops
        if nh not in view.nodes:
            # 非路由器名称 => 直连出口/接口名，与期望出口不符即断路到错误出口
            return False, "wrong_exit", hops
        if view.topology is not None and nh not in set(view.topology.get(cur, [])):
            return False, "no_link", hops
        cur = nh
    return False, "loop", hops
