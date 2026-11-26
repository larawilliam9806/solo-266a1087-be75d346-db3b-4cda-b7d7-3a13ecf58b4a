# 园区路由器静态路由整表切换排程服务

值班员提交园区路由器的**新旧两份 IPv4 静态路由表**和一组**保通要求**，服务排出
**批数最少**的整表切换下发计划。

## 业务约束

| 约束 | 取值 |
| --- | --- |
| 换表设备数 | 1 ~ 12 台 |
| 切换粒度 | 每台设备**整表原子切换**（旧表 → 新表，不做条目级增删） |
| 每批设备数 | ≤ 3 台（可通过 `max_batch_size` 收紧） |
| 保通要求 | 每条含：入口路由器、目的 IPv4 网段、指定出口 |
| 正确性口径 | 从开始到完成，**无论同批内哪些设备先换表**，目的网段内**每个地址**都必须按当时的**最长前缀匹配（LPM）**从入口逐跳到达指定出口 |
| 校验方式 | **路由等价类**分解，每个等价类只验一个代表地址，**不逐地址穷举** |
| 无解输出 | 可复现的换表状态、目的地址、失效原因（断路/环路/错误出口/无邻接）与逐跳路径 |

## 为什么“批内任意顺序”是可判定的

一批设备 S 在状态 A 上切换时，S 内设备无论按什么顺序换表，过程中经过的状态
恰好是 `A ∪ T`，其中 T 是 S 的任意子集。因此批次 `A → A∪S` 安全，当且仅当
`A∪S` 的全部子集状态都满足所有保通要求。求解器对每个子集状态都做可行性校验。

## 为什么不用逐地址穷举

把每条保通要求的目的网段，按**全网所有路由表里出现过的前缀边界**递归二分，
切成有限个互不相交的“路由等价类”：

- 同一等价类内不存在任何路由表前缀的边界，因此类内任意地址在任意设备上的
  LPM 结果完全相同；
- 每个等价类只抽一个代表地址做逐跳仿真，即可代表该类内全部地址；
- 等价类数量只与前缀条目数有关（≤ 约 2 × 前缀数 × 32），与网段主机数无关，
  `/8` 这样的大网段也不会放大校验成本。

## 求解算法

1. 位掩码表示换表状态（第 i 位 = 设备 i 已用新表），共 `2^n`（n ≤ 12，即 ≤ 4096）个状态；
2. 状态可行性按掩码缓存，每个状态只仿真一次；
3. BFS 在“批次转移图”上按批数搜索最短路径，同层优先尝试大批次；
4. 起点（全旧表）或终点（全新表）本身不可行时立即返回证据；
5. 搜索不可达时，选取 popcount 最小的不可行状态生成最易复现的证据。

## 目录结构

```
app/
  models.py    Pydantic 请求/响应模型与入参校验
  ipcore.py    IPv4 前缀、LPM、等价类分解、逐跳仿真、证据结构
  planner.py   批计划 BFS 求解器
  main.py      FastAPI 路由
tests/         22 个单元/性质/API 测试
examples/      可直接调用的请求样例
Dockerfile, docker-compose.yml, requirements.txt
```

## 启动方式

### docker compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
```

### 本地 Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

- `GET /health` 健康检查
- `GET /constraints` 返回规模与校验口径约束
- `POST /plan` 提交排程请求

### 请求字段

| 字段 | 说明 |
| --- | --- |
| `old_tables` / `new_tables` | 设备名 → 路由表（`{目的网段: 下一跳}`），两边设备集合必须一致 |
| `requirements` | 保通要求数组：`entry_router` / `dst_prefix` / `exit_node` |
| `topology` | 可选，路由器直连邻接；提供后会校验下一跳是否有直连链路。出口名（非路由器）不要写进 topology |
| `max_batch_size` | 可选，1~3，默认 3 |

下一跳取值约定：值为受管路由器名时表示转发到该路由器；否则视为直连出口/接口名
（如 `ISP1`），流量在该跳离开园区。

### 可行响应示例

```bash
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  -d @examples/solvable.json
```

```json
{
  "status": "ok",
  "device_count": 2,
  "batch_count": 2,
  "max_batch_size": 3,
  "batches": [
    {"batch": 1, "devices": ["B"]},
    {"batch": 2, "devices": ["A"]}
  ],
  "equivalence_class_count": 1,
  "states_checked": 4,
  "note": "批次内任意先后换表顺序均满足全部保通要求；每等价类只验证一个代表地址"
}
```

### 无解响应示例（环路）

```bash
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  -d @examples/unsat-loop.json
```

`witness` 给出复现所需的全部信息：

- `switched_devices`：切到新表的设备（其余保持旧表）
- `dst_address` + `dst_equivalence_class`：发流目的地址及其所属等价类
- `reason`：`loop` / `no_route` / `wrong_exit` / `no_link`
- `hop_by_hop`：每跳节点、命中的 LPM 前缀、下一跳；环路附闭合说明
- `reproduction`：三步中文复现指引

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

覆盖：等价类分解的随机性质（类内 LPM 恒等、并集全覆盖）、批次全部内部子集的
独立复核、最短批数、批大小上限、12 台规模性能（< 10s）、断路/环路/错误出口/
无邻接证据、证据可独立重放、FastAPI 端到端与 400/422 校验。
