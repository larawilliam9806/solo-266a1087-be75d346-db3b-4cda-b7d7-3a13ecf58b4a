"""求解器与 API 测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ipcore import decompose, parse_prefix
from app.main import app
from app.models import PlanRequest, Requirement, RouteTable
from app.planner import solve

client = TestClient(app)


# ---------- 等价类分解 ----------

def test_decompose_no_boundary_returns_whole_prefix():
    out = decompose(parse_prefix("10.0.0.0/24"), set())
    assert out == [parse_prefix("10.0.0.0/24")]


def test_decompose_splits_on_nested_boundaries_and_covers_all_addresses():
    dst = parse_prefix("10.0.0.0/24")
    boundary = {parse_prefix("10.0.0.128/25"), parse_prefix("10.0.0.128/26")}
    pieces = decompose(dst, boundary)
    # /24 先切成两个 /25；其中 10.0.0.128/25 内含更深的 /26 边界，再切成两个 /26
    assert parse_prefix("10.0.0.0/25") in pieces
    assert parse_prefix("10.0.0.128/26") in pieces
    assert parse_prefix("10.0.0.192/26") in pieces
    assert parse_prefix("10.0.0.128/25") not in pieces
    # 并集恰好覆盖整个 /24，且两两不相交
    total = sum(2 ** (32 - p[1]) for p in pieces)
    assert total == 256


def test_decompose_boundary_outside_prefix_ignored():
    out = decompose(parse_prefix("10.0.0.0/24"), {parse_prefix("192.168.0.0/16")})
    assert out == [parse_prefix("10.0.0.0/24")]


# ---------- 求解器：可行场景 ----------

def _req_simple():
    # 两台设备 A、B；要求 A 进入 10.1.0.0/16 必须从出口 ISP1 离开。
    # 旧态：A 直接把 10.1/16 送 ISP1。新态：A 把该网段改指 B，由 B 统一送 ISP1。
    # 若先换 B：B 表变化对 A 无影响，A 仍直送 ISP1 —— 安全；
    # 若先换 A：A 改指 B，而 B 旧表没有 10.1/16，落到默认路由的错误出口 —— 断路。
    # 故整批 {A,B} 不合法，最短 2 批，且 B 必须先于 A。
    old = {
        "A": RouteTable(routes={"10.1.0.0/16": "ISP1", "0.0.0.0/0": "B"}),
        "B": RouteTable(routes={"0.0.0.0/0": "WRONG_EXIT"}),
    }
    new = {
        "A": RouteTable(routes={"10.1.0.0/16": "B", "0.0.0.0/0": "B"}),
        "B": RouteTable(routes={"10.1.0.0/16": "ISP1", "0.0.0.0/0": "WRONG_EXIT"}),
    }
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.1.0.0/16", exit_node="ISP1"),
        ],
    )
    return req


def test_solve_two_batch_when_order_matters():
    res = solve(_req_simple())
    assert res["status"] == "ok"
    # 整批 {A,B} 因“先换 A”这一顺序失败，故最短为 2 批
    assert res["batch_count"] == 2
    flat = [d for b in res["batches"] for d in b["devices"]]
    assert sorted(flat) == ["A", "B"]
    # A 不能出现在第一批（先换 A 即断），B 必须先行
    assert "A" not in res["batches"][0]["devices"]
    assert res["batches"][0]["devices"] == ["B"]


def test_solve_single_batch_when_all_subsets_safe():
    old = {
        "A": RouteTable(routes={"10.0.0.0/16": "B"}),
        "B": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
    }
    new = {
        "A": RouteTable(routes={"10.0.0.0/16": "B"}),
        "B": RouteTable(routes={"10.0.0.0/16": "ISP1", "10.0.1.0/24": "ISP1"}),
    }
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "ok" and res["batch_count"] == 1
    assert sorted(res["batches"][0]["devices"]) == ["A", "B"]


def test_batch_size_limit_respected():
    devices = [f"R{i}" for i in range(7)]
    old = {d: RouteTable(routes={"10.0.0.0/8": "ISP1"}) for d in devices}
    new = {d: RouteTable(routes={"10.0.0.0/8": "ISP1"}) for d in devices}
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router=d, dst_prefix="10.0.0.0/8", exit_node="ISP1")
            for d in devices
        ],
        max_batch_size=3,
    )
    res = solve(req)
    assert res["status"] == "ok"
    # 7 台、每批最多 3 台 => 至少 3 批
    assert res["batch_count"] == 3
    assert all(len(b["devices"]) <= 3 for b in res["batches"])


def test_longest_prefix_match_drives_equivalence_classes():
    # B 新表把 10.0.1.0/24 指向 ISP2，其余 /16 仍走 ISP1。
    # 要求覆盖整个 /16 到 ISP1 => B 切换后 /24 等价类必然违反。
    old = {
        "A": RouteTable(routes={"0.0.0.0/0": "B"}),
        "B": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
    }
    new = {
        "A": RouteTable(routes={"0.0.0.0/0": "B"}),
        "B": RouteTable(routes={
            "10.0.0.0/16": "ISP1",
            "10.0.1.0/24": "ISP2",
        }),
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
    # 证据必须落在被更长前缀劫持的 /24 等价类内，而不是随便挑个地址
    assert w["dst_equivalence_class"] == "10.0.1.0/24"
    assert w["reason"] == "wrong_exit"
    assert w["expected_exit"] == "ISP1"


# ---------- 求解器：无解证据（断路 / 环路） ----------

def test_unsat_no_route_witness():
    old = {"A": RouteTable(routes={"10.0.0.0/16": "ISP1"})}
    new = {"A": RouteTable(routes={"172.16.0.0/12": "ISP1"})}  # 新表无 10/16 路由
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "unsat"
    assert res["witness"]["reason"] == "no_route"
    assert res["witness"]["switched_devices"] == ["A"]
    assert "reproduction" in res and len(res["reproduction"]) == 3


def test_unsat_loop_witness_has_hop_by_hop():
    # 新表 A<->B 对指，且无默认出口 => 环路
    old = {
        "A": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
        "B": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
    }
    new = {
        "A": RouteTable(routes={"10.0.0.0/16": "B"}),
        "B": RouteTable(routes={"10.0.0.0/16": "A"}),
    }
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        topology={"A": ["B"], "B": ["A"]},
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "unsat"
    assert res["witness"]["reason"] == "loop"
    nodes = [h["node"] for h in res["witness"]["hop_by_hop"]]
    assert nodes[0] == "A"
    # 闭合标记节点重复出现
    assert len(nodes) != len(set(nodes))


def test_unsat_at_initial_state_reported_directly():
    # 旧表自身就不满足要求 => 状态 0 即失败，应立即给证据而非搜索
    old = {"A": RouteTable(routes={"10.0.0.0/16": "ISP_OTHER"})}
    new = {"A": RouteTable(routes={"10.0.0.0/16": "ISP1"})}
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "unsat"
    assert res["witness"]["switched_devices"] == []


def test_unsat_no_link_when_topology_given():
    # A 新表把流量送给 C，但拓扑里 A 与 C 不直连 => no_link
    old = {
        "A": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
        "B": RouteTable(routes={}),
        "C": RouteTable(routes={}),
    }
    new = {
        "A": RouteTable(routes={"10.0.0.0/16": "C"}),
        "B": RouteTable(routes={}),
        "C": RouteTable(routes={"10.0.0.0/16": "ISP1"}),
    }
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        topology={"A": ["B"], "B": ["A", "C"], "C": ["B"]},
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "unsat"
    assert res["witness"]["reason"] == "no_link"


# ---------- 规模上限 ----------

def test_twelve_devices_accepted_and_solved():
    devices = [f"R{i}" for i in range(12)]
    old = {d: RouteTable(routes={"10.0.0.0/8": "ISP1"}) for d in devices}
    new = {d: RouteTable(routes={"10.0.0.0/8": "ISP1"}) for d in devices}
    req = PlanRequest(
        old_tables=old,
        new_tables=new,
        requirements=[
            Requirement(entry_router="R0", dst_prefix="10.0.0.0/8", exit_node="ISP1"),
        ],
    )
    res = solve(req)
    assert res["status"] == "ok"
    assert res["device_count"] == 12
    assert res["batch_count"] == 4  # ceil(12/3)


def test_more_than_twelve_devices_rejected():
    from pydantic import ValidationError

    devices = [f"R{i}" for i in range(13)]
    with pytest.raises(ValidationError):
        PlanRequest(
            old_tables={d: RouteTable(routes={}) for d in devices},
            new_tables={d: RouteTable(routes={}) for d in devices},
            requirements=[
                Requirement(entry_router="R0", dst_prefix="10.0.0.0/8", exit_node="X"),
            ],
        )


def test_model_validator_runs_regardless_of_field_order():
    """model_validator 后置校验，不依赖 old/new_tables 字段的到达顺序。"""
    from pydantic import ValidationError

    # 设备集合不一致
    with pytest.raises(ValidationError):
        PlanRequest(
            old_tables={"A": RouteTable(routes={})},
            new_tables={"B": RouteTable(routes={})},
            requirements=[
                Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="X"),
            ],
        )
    # 正常构造不报错
    ok = PlanRequest(
        old_tables={"A": RouteTable(routes={})},
        new_tables={"A": RouteTable(routes={})},
        requirements=[
            Requirement(entry_router="A", dst_prefix="10.0.0.0/16", exit_node="X"),
        ],
    )
    assert set(ok.old_tables) == {"A"}


# ---------- API ----------

def test_api_health_and_constraints():
    assert client.get("/health").json() == {"status": "ok"}
    c = client.get("/constraints").json()
    assert c["max_devices"] == 12 and c["max_batch_size"] == 3


def test_api_plan_endpoint_ok_and_unsat():
    payload = {
        "old_tables": {
            "A": {"routes": {"10.1.0.0/16": "ISP1", "0.0.0.0/0": "B"}},
            "B": {"routes": {"0.0.0.0/0": "WRONG_EXIT"}},
        },
        "new_tables": {
            "A": {"routes": {"10.1.0.0/16": "B", "0.0.0.0/0": "B"}},
            "B": {"routes": {"10.1.0.0/16": "ISP1", "0.0.0.0/0": "WRONG_EXIT"}},
        },
        "requirements": [
            {"entry_router": "A", "dst_prefix": "10.1.0.0/16", "exit_node": "ISP1"}
        ],
    }
    r = client.post("/plan", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["batch_count"] == 2

    # 无解请求
    payload["new_tables"]["B"]["routes"] = {"172.16.0.0/12": "ISP1"}
    r2 = client.post("/plan", json=payload)
    assert r2.status_code == 200 and r2.json()["status"] == "unsat"
    assert "witness" in r2.json()


def test_api_rejects_bad_prefix():
    r = client.post(
        "/plan",
        json={
            "old_tables": {"A": {"routes": {}}},
            "new_tables": {"A": {"routes": {}}},
            "requirements": [
                {"entry_router": "A", "dst_prefix": "10.0.0.1/24", "exit_node": "X"}
            ],
        },
    )
    assert r.status_code == 422


def test_api_rejects_mismatched_device_sets():
    r = client.post(
        "/plan",
        json={
            "old_tables": {"A": {"routes": {}}},
            "new_tables": {"B": {"routes": {}}},
            "requirements": [],
        },
    )
    assert r.status_code == 422


def test_api_plan_error_returns_400():
    r = client.post(
        "/plan",
        json={
            "old_tables": {"A": {"routes": {}}},
            "new_tables": {"A": {"routes": {}}},
            "requirements": [
                {"entry_router": "GHOST", "dst_prefix": "10.0.0.0/16", "exit_node": "X"}
            ],
        },
    )
    assert r.status_code == 400 and r.json()["status"] == "error"
