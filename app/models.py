"""Pydantic 模型：换表计划请求/响应结构。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_DEVICES = 12
MAX_BATCH_SIZE = 3


class Requirement(BaseModel):
    """一条保通要求：从 entry_router 进入、目的为 dst_prefix 的流量必须从 exit_node 离开。"""

    entry_router: str = Field(..., min_length=1, description="入口路由器名称")
    dst_prefix: str = Field(..., description="目的 IPv4 网段，如 10.0.0.0/24")
    exit_node: str = Field(..., min_length=1, description="指定出口（下一跳路由器或直连出口名）")

    @field_validator("dst_prefix")
    @classmethod
    def _validate_prefix(cls, v: str) -> str:
        from .ipcore import parse_prefix  # 延迟导入避免循环依赖

        parse_prefix(v)  # 抛 ValueError 即 422
        return v


class RouteTable(BaseModel):
    """单台路由器的静态路由表：目标网段 -> 下一跳。"""

    routes: dict[str, str] = Field(default_factory=dict)

    @field_validator("routes")
    @classmethod
    def _validate_routes(cls, v: dict[str, str]) -> dict[str, str]:
        from .ipcore import parse_prefix

        for prefix in v:
            parse_prefix(prefix)
        return v


class PlanRequest(BaseModel):
    """换表计划请求。

    old/new_tables 的 key 为路由器名称，只允许出现在需要换表的设备上（最多 12 台）。
    topology 描述路由器之间的直连邻接，用于逐跳仿真与环路检测；不填则只校验
    “入口路由器首跳是否为指定出口”。
    """

    old_tables: dict[str, RouteTable]
    new_tables: dict[str, RouteTable]
    requirements: list[Requirement]
    topology: dict[str, list[str]] | None = None
    max_batch_size: int = Field(default=MAX_BATCH_SIZE, ge=1, le=MAX_BATCH_SIZE)

    @field_validator("requirements")
    @classmethod
    def _non_empty(cls, v: list[Requirement]) -> list[Requirement]:
        if not v:
            raise ValueError("至少需要一条保通要求")
        return v

    @model_validator(mode="after")
    def _validate_device_sets(self) -> PlanRequest:
        old, new = self.old_tables, self.new_tables
        if set(old) != set(new):
            raise ValueError("new_tables 与 old_tables 的设备集合必须一致")
        if not (1 <= len(old) <= MAX_DEVICES):
            raise ValueError(f"换表设备数必须在 1..{MAX_DEVICES} 之间")
        return self
