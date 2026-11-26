"""换表批计划求解器。

思路
----
1. 把每条保通要求的目的网段，按全网路由表前缀边界切成有限个路由等价类
   （见 ipcore.decompose）。同一等价类内所有地址在任意设备上的最长前缀匹配
   结果相同，故每类只验一个代表地址，绝不逐地址穷举。
2. 换表状态用位掩码表示（第 i 位 = 设备 i 已切到新表）。状态可行性按位掩码
   缓存，每个状态只仿真一次。
3. 一批切换 S 时，S 内部任意先后顺序都会经过 A∪S 的每个子集，因此转移
   A -> A∪S 合法当且仅当 A∪S 的全部子集都可行。批次大小 ≤ max_batch_size。
4. BFS 按批数最短搜索（同层优先取大批次），得到批数最少的计划；找不到完整
   状态的通路时，输出最小不可行状态的可复现证据（逐跳路径 + 断路/环路原因）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import combinations

from .ipcore import (
    Prefix,
    RouteTableView,
    Witness,
    decompose,
    parse_prefix,
    prefix_str,
    simulate,
)
from .models import PlanRequest, Requirement


class PlanError(ValueError):
    pass


@dataclass
class EC:
    """一个路由等价类：同类内任意地址在任意设备上的 LPM 结果一致。"""

    req_index: int
    prefix: Prefix
    rep_addr: int
    entry: str
    exit_node: str


def _build_tables(req: PlanRequest) -> tuple[list[str], dict, dict]:
    devices = sorted(req.old_tables)
    old: dict[str, dict[Prefix, str]] = {}
    new: dict[str, dict[Prefix, str]] = {}
    for d in devices:
        old[d] = {parse_prefix(p): nh for p, nh in req.old_tables[d].routes.items()}
        new[d] = {parse_prefix(p): nh for p, nh in req.new_tables[d].routes.items()}
    return devices, old, new


def _effective_table(
    device: str, switched: bool, old: dict, new: dict
) -> dict[Prefix, str]:
    return new[device] if switched else old[device]


def _validate_topology(req: PlanRequest, devices: list[str]) -> dict[str, list[str]] | None:
    if req.topology is None:
        return None
    dset = set(devices)
    topo: dict[str, list[str]] = {}
    for node, nbrs in req.topology.items():
        if node not in dset:
            raise PlanError(f"topology 中的节点 {node!r} 不在换表设备列表中")
        for n in nbrs:
            if n not in dset:
                raise PlanError(
                    f"topology 邻接 {node}->{n} 中 {n!r} 不是受管路由器；"
                    f"直连出口应只作为路由下一跳名称出现"
                )
        topo[node] = list(nbrs)
    for d in devices:
        topo.setdefault(d, [])
    return topo


def _build_ecs(
    requirements: list[Requirement],
    devices: list[str],
    old: dict,
    new: dict,
) -> list[EC]:
    boundary: set[Prefix] = set()
    for d in devices:
        boundary.update(old[d])
        boundary.update(new[d])
    ecs: list[EC] = []
    for i, r in enumerate(requirements):
        if r.entry_router not in devices:
            raise PlanError(
                f"第 {i} 条要求的入口路由器 {r.entry_router!r} 不在换表设备列表中"
            )
        dst = parse_prefix(r.dst_prefix)
        for ec in decompose(dst, boundary):
            ecs.append(
                EC(
                    req_index=i,
                    prefix=ec,
                    rep_addr=ec[0],  # 等价类网络地址作为代表地址
                    entry=r.entry_router,
                    exit_node=r.exit_node,
                )
            )
    return ecs


def solve(req: PlanRequest) -> dict:
    devices, old, new = _build_tables(req)
    topo = _validate_topology(req, devices)
    ecs = _build_ecs(req.requirements, devices, old, new)

    n = len(devices)
    full = (1 << n) - 1
    bit = {d: 1 << i for i, d in enumerate(devices)}

    def make_view(mask: int) -> RouteTableView:
        tables = {
            d: _effective_table(d, bool(mask & bit[d]), old, new) for d in devices
        }
        return RouteTableView(tables, topo)

    def find_witness(mask: int) -> Witness:
        """在状态 mask 下找一条不满足的保通要求，构造逐跳证据。"""
        view = make_view(mask)
        for ec in ecs:
            ok, reason, _ = simulate(view, ec.entry, ec.rep_addr, ec.exit_node)
            if ok:
                continue
            _, first_nh = view.lpm(ec.entry, ec.rep_addr)
            _, _, hops = simulate(view, ec.entry, ec.rep_addr, ec.exit_node)
            return Witness(
                switched=[devices[i] for i in range(n) if mask & (1 << i)],
                requirement_index=ec.req_index,
                dst_address=_addr_text(ec.rep_addr, ec.prefix),
                dst_ec_prefix=prefix_str(ec.prefix),
                entry_router=ec.entry,
                expected_exit=ec.exit_node,
                reason=reason or ("first_hop_mismatch" if first_nh != ec.exit_node else "failure"),
                hops=hops,
            )
        raise PlanError("内部错误：该状态实际可行")

    # 每个状态只仿真一次
    feasible: dict[int, bool] = {}

    def is_feasible(mask: int) -> bool:
        if mask in feasible:
            return feasible[mask]
        view = make_view(mask)
        for ec in ecs:
            ok, _, _ = simulate(view, ec.entry, ec.rep_addr, ec.exit_node)
            if not ok:
                feasible[mask] = False
                return False
        feasible[mask] = True
        return True

    # 起点/终点自身不可行 => 直接给证据
    if not is_feasible(0):
        return _unsolved(req, devices, find_witness(0), feasible, ecs, n, checked=len(feasible))
    if not is_feasible(full):
        return _unsolved(req, devices, find_witness(full), feasible, ecs, n, checked=len(feasible))

    # 枚举每个状态可用的批次（大小降序，BFS 同层先尝试大批次）
    def candidate_batches(mask: int) -> list[tuple[int, ...]]:
        remaining = [i for i in range(n) if not (mask & (1 << i))]
        return [
            combo
            for size in range(min(req.max_batch_size, len(remaining)), 0, -1)
            for combo in combinations(remaining, size)
        ]

    def transition_safe(mask: int, combo: tuple[int, ...]) -> bool:
        add = 0
        for i in combo:
            add |= 1 << i
        # 批内任意顺序 => mask|add 的每个含 mask 的子集都必须可行
        sub = add
        while True:
            if not is_feasible(mask | sub):
                return False
            if sub == 0:
                break
            sub = (sub - 1) & add
        return True

    # BFS：按批数最短
    parent: dict[int, tuple[int, tuple[int, ...]]] = {0: (-1, ())}
    q = deque([0])
    reached = False
    while q:
        mask = q.popleft()
        if mask == full:
            reached = True
            break
        for combo in candidate_batches(mask):
            if not transition_safe(mask, combo):
                continue
            add = 0
            for i in combo:
                add |= 1 << i
            nxt = mask | add
            if nxt not in parent:
                parent[nxt] = (mask, combo)
                q.append(nxt)

    if not reached:
        # 选 popcount 最小的不可行状态作为最易复现证据
        bad = min(
            (m for m in range(full + 1) if not is_feasible(m)),
            key=lambda m: m.bit_count(),
        )
        return _unsolved(
            req, devices, find_witness(bad), feasible, ecs, n, checked=len(feasible)
        )

    # 回溯批次
    batches: list[list[str]] = []
    cur = full
    while cur != 0:
        prev, combo = parent[cur]
        batches.append([devices[i] for i in combo])
        cur = prev
    batches.reverse()

    return {
        "status": "ok",
        "device_count": n,
        "batch_count": len(batches),
        "max_batch_size": req.max_batch_size,
        "batches": [
            {"batch": idx + 1, "devices": devs} for idx, devs in enumerate(batches)
        ],
        "equivalence_class_count": len(ecs),
        "states_checked": len(feasible),
        "note": "批次内任意先后换表顺序均满足全部保通要求；每等价类只验证一个代表地址",
    }


def _addr_text(addr: int, ec: Prefix) -> str:
    from .ipcore import ip_str

    # 等价类网络地址作为代表地址（/31、/32 同样合法）
    return ip_str(addr)


def _unsolved(
    req: PlanRequest,
    devices: list[str],
    witness: Witness,
    feasible: dict[int, bool],
    ecs: list[EC],
    n: int,
    checked: int,
) -> dict:
    return {
        "status": "unsat",
        "device_count": n,
        "equivalence_class_count": len(ecs),
        "states_checked": checked,
        "message": "不存在满足全部保通要求的整表切换批次序列",
        "witness": witness.to_dict(),
        "reproduction": [
            f"把以下设备切到新表（其余保持旧表）：{', '.join(witness.switched) or '（无，即全部旧表状态）'}",
            (
                f"从入口 {witness.entry_router} 向地址 {witness.dst_address}"
                f"（等价类 {witness.dst_ec_prefix}，代表第 {witness.requirement_index} 条要求的目的网段）发流"
            ),
            f"按最长前缀匹配逐跳转发即复现 {_reason_cn(witness.reason)}，无法到达指定出口 {witness.expected_exit}",
        ],
    }


def _reason_cn(reason: str) -> str:
    return {
        "loop": "转发环路",
        "no_route": "无匹配路由（断路）",
        "wrong_exit": "从非指定出口离开",
        "no_link": "下一跳与当前设备无直连邻接",
        "first_hop_mismatch": "首跳与指定出口不一致",
    }.get(reason, reason)

