"""性质测试与性能测试（不依赖第三方库，手写随机用例）。"""
from __future__ import annotations

import random
import time

from app.ipcore import RouteTableView, decompose, parse_prefix, prefix_str
from app.models import PlanRequest, Requirement, RouteTable
from app.planner import solve


def _lpm_in_python(table: dict, addr: int):
    best = None
    for (net, length), nh in table.items():
        if (length == 0 or (addr >> (32 - length)) == (net >> (32 - length))) and (
            best is None or length > best[0][1]
        ):
            best = ((net, length), nh)
    return best


def test_equivalence_classes_are_truly_equivalent_randomized():
    """随机生成路由表与目的网段，验证每个等价类内所有地址在所有设备上的 LPM 一致。"""
    rng = random.Random(20261126)
    for _ in range(40):
        devices = [f"R{i}" for i in range(rng.randint(1, 5))]
        tables: dict[str, dict] = {}
        boundary = set()
        for d in devices:
            t = {}
            for _ in range(rng.randint(1, 8)):
                length = rng.randint(0, 30)
                net = rng.randint(0, (1 << 32) - 1) & (0xFFFFFFFF << (32 - length)) & 0xFFFFFFFF
                p = (net, length)
                t[p] = f"nh_{rng.randint(0, 4)}"
                boundary.add(p)
            tables[d] = t
        dst_len = rng.randint(8, 24)
        dst_net = rng.randint(0, (1 << 32) - 1) & (0xFFFFFFFF << (32 - dst_len)) & 0xFFFFFFFF
        dst = (dst_net, dst_len)
        pieces = decompose(dst, boundary)

        covered = 0
        for p in pieces:
            base, plen = p
            size = 2 ** (32 - plen)
            covered += size
            sample_addrs = {base, base + size - 1}
            sample_addrs.add(base + rng.randint(0, size - 1))
            for d in devices:
                results = {_lpm_in_python(tables[d], a) for a in sample_addrs}
                assert len(results) == 1, (
                    f"等价类 {prefix_str(p)} 在 {d} 上 LPM 不一致: {results}"
                )
        assert covered == 2 ** (32 - dst_len)


def _all_subsets(items):
    items = list(items)
    for mask in range(1 << len(items)):
        yield {items[i] for i in range(len(items)) if mask & (1 << i)}


def test_every_batch_is_safe_under_all_internal_orders():
    """对返回计划做独立复核：每个批次的全部内部子集状态都满足全部保通要求。"""
    old = {
        "A": RouteTable(routes={"10.1.0.0/16": "ISP1", "0.0.0.0/0": "B"}),
        "B": RouteTable(routes={"0.0.0.0/0": "WRONG"}),
        "C": RouteTable(routes={"0.0.0.0/0": "WRONG"}),
    }
    new = {
        "A": RouteTable(routes={"10.1.0.0/16": "B", "0.0.0.0/0": "B"}),
        "B": RouteTable(routes={"10.1.0.0/16": "ISP1", "0.0.0.0/0": "WRONG"}),
        "C": RouteTable(routes={"10.1.0.0/16": "ISP1", "0.0.0.0/0": "WRONG"}),
    }
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.1.0.0/16", exit_node="ISP1"),
        ],
        max_batch_size=2,
    )
    res = solve(req)
    assert res["status"] == "ok"

    devices = sorted(old)
    switched: set[str] = set()
    for batch in res["batches"]:
        devs = batch["devices"]
        assert len(devs) <= 2
        for sub in _all_subsets(devs):
            state = switched | sub
            effective = {
                d: (new[d].routes if d in state else old[d].routes)
                for d in devices
            }
            parsed = {
                d: {parse_prefix(p): nh for p, nh in rt.items()}
                for d, rt in effective.items()
            }
            view = RouteTableView(parsed, None)
            # 对该要求的每个等价类代表地址都复核
            boundary = set()
            for d in devices:
                boundary.update(parsed[d])
            for ec in decompose(parse_prefix("10.1.0.0/16"), boundary):
                from app.ipcore import simulate

                ok, reason, _ = simulate(view, "A", ec[0], "ISP1")
                assert ok, f"状态 {sorted(state)} 子批 {sorted(sub)} 失败: {reason}"
        switched |= set(devs)
    assert switched == set(devices)


def test_twelve_devices_runtime_bounded():
    devices = [f"R{i}" for i in range(12)]
    # 制造依赖：Ri 新表把流量交给 R(i+1)，R11 送 ISP1；旧表全部直送 ISP1。
    # 要求各入口都到 ISP1 => 只有按顺序切才安全，检验 4096 状态搜索性能。
    old = {d: RouteTable(routes={"10.0.0.0/8": "ISP1"}) for d in devices}
    new = {}
    for i, d in enumerate(devices):
        nh = devices[i + 1] if i + 1 < len(devices) else "ISP1"
        new[d] = RouteTable(routes={"10.0.0.0/8": nh})
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router=devices[0], dst_prefix="10.0.0.0/8", exit_node="ISP1"),
        ],
    )
    t0 = time.time()
    res = solve(req)
    elapsed = time.time() - t0
    assert res["status"] == "ok"
    assert elapsed < 10.0, f"12 台设备求解耗时 {elapsed:.2f}s 超限"
    assert res["states_checked"] <= 4096


def test_witness_is_reproducible_standalone():
    """用证据里的换表状态独立重建视图，逐跳重放必须得到相同失效结论。"""
    old = {
        "A": RouteTable(routes={"10.0.0.0/16": "B"}),
        "B": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
    }
    new = {
        "A": RouteTable(routes={"10.0.0.0/16": "B"}),
        "B": RouteTable(routes={"10.0.0.0/16": "ISP2"}),
    }
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "unsat"
    w = res["witness"]

    state = set(w["switched_devices"])
    effective = {}
    for d, old_rt in old.items():
        rt = new[d].routes if d in state else old_rt.routes
        effective[d] = {parse_prefix(p): nh for p, nh in rt.items()}
    view = RouteTableView(effective, None)
    addr = int(w["dst_address"].split(".")[0]) << 24  # 简单转 int
    octets = [int(x) for x in w["dst_address"].split(".")]
    addr = sum(o << (8 * (3 - i)) for i, o in enumerate(octets))
    from app.ipcore import simulate

    ok, reason, hops = simulate(view, w["entry_router"], addr, w["expected_exit"])
    assert not ok and reason == w["reason"]
    assert [h.node for h in hops] == [h["node"] for h in w["hop_by_hop"]]
