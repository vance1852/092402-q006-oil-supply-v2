# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 油田、储罐、终端与炼厂设施建档，线路保存日能力、在途时间和损耗规则；
- 线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 库存批次保留油品、牌号、数量、单位成本和接收时间，可计算加权库存成本；
- 托运提名支持载荷级幂等、优先级分配、库存扣减和在途交接；
- 供应情景保存价格变化、线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

现场准入子域位于 `robot_trials` 包，负责油田巡检机器人的设备构建登记、不可变试验协议、观测分片导入、异常观测复核、统计任务租约、准入决定和审计报告。该子域不连接机器人硬件，只处理已经结构化的试验记录。

汽油指导价子域位于 `fuel_pricing` 包，面向税费、加工价差、调整周期和最低变动门槛按政策日期变化的运营场景，绝不以今日规则重算历史决定：

- 调价规则按版本走「草稿 → 提交复核 → 批准（待定时生效）→ 定时生效 → 退役」流程，生效半开区间 `[effective_from, retired_at)` 强制不重叠；
- 每个批准版本的完整规则快照（税费组件、加工价差、周期锚点、报价窗口工作日数、汇率、地板/天花板价、基准价）与内容哈希一同固化；
- 决定的输入快照逐日报价锁定登记时刻最新的来源修订（`quote_id`/`source_revision`），窗口缺日报错；
- 计算全程 Decimal：指导价 ROUND_HALF_UP 四舍五入到分，不足最低变动门槛或触及地板/天花板价时本周期**暂缓**，未生效差额全额**结转下周期**累计；
- 运营人员可 `preview` 某日结果而不落账；正式 `publish` 生成不可变决定（每牌号每周期日唯一），同键或同输入重复发布返回同一记录；
- 后续报价更正不会静默改写历史，只会把直接受影响决定（窗口内报价被取代）与 carry 链下游决定标记为 `input_superseded`，并生成一条 open 状态的重算建议；以历史规则快照重算只产出对比预览，是否采纳由风险岗位处理。

## 目录

- `src/oil_supply/`：报价、设施、线路、库存、提名、供应情景、HTTP API 与离线验收；
- `src/fuel_pricing/`：汽油指导价政策版本、确定性计算、不可变决定、修订影响与定时生效任务；
- `src/robot_trials/`：油田巡检机器人试验与统计准入；
- `fixtures/`：现场准入演示协议和结构化观测；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

在依赖已经准备好的容器中安装：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m oil_supply.acceptance --workspace .
```

该命令会在内存数据库中登记六个交易日的布伦特报价，创建油田、终端和输送线路，完成库存入账、提名分配、发运及供应情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

现场准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

汽油指导价子域的离线验收：

```bash
PYTHONPATH=src python3 -m fuel_pricing.acceptance --workspace .
```

该命令贯通政策两版（增值税率与门槛按日期切换）、两个调价周期（第一周期不足门槛暂缓并结转，第二周期累计跨门槛下调）、预览不落账、重复发布返回同一记录、政策滚动生效、以及 01-14 报价更正后历史决定原样保留、只标记并生成重算建议的完整流程。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m oil_supply.api --database oil_supply.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、设施、线路、停运事件、库存批次、提名、能力分配、发运、供应情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

### 汽油指导价接口

```bash
PYTHONPATH=src python3 -m fuel_pricing.api --database fuel_pricing.sqlite3 --host 127.0.0.1 --port 8090
```

所有路径以 `/pricing` 前缀开头，同样通过 `X-Actor-Id` 识别操作者。角色与权限：

| 角色 | 权限 |
| --- | --- |
| planner | 报价登记、规则草稿/提交/退役、决定预览与发布 |
| reviewer | 规则批准、定时生效执行 |
| risk | 重算建议预览与处理、报表查询 |
| auditor | 报表与审计链只读 |

主要接口：

- `POST /pricing/rules` 创建规则草稿；`PUT /pricing/rules/{id}/{version}` 修改草稿；`POST .../submit`、`.../review`、`.../retire` 驱动草稿→复核→批准→退役；`POST /pricing/rules/activate` 触发定时生效/退役（幂等）；
- `POST /pricing/quotes` 按交易日登记报价，同名修订通过新的 `source_revision` 追加，不覆盖历史；
- `POST /pricing/decisions/preview` 预览，不写任何业务表；`POST /pricing/decisions` 发布不可变决定，重复发布（同幂等键或同输入）返回同一记录（`replayed: true`，HTTP 200）；
- `GET /pricing/decisions`、`GET /pricing/decisions/{id}`、`GET /pricing/carry`、`GET /pricing/impacts`、`GET /pricing/recalc-suggestions` 查询决定、结转台账、修订影响与重算建议；
- `POST /pricing/recalc-suggestions/{id}` 以历史规则快照重算对比；`POST /pricing/recalc-suggestions/{id}/resolve` 处理建议。

### 定时生效任务

```bash
PYTHONPATH=src python3 -m fuel_pricing.scheduler --database fuel_pricing.sqlite3
```

供外部调度器每个工作日早间调用：将到生效日的已批准版本置为生效、将到退役日的生效版本置为退役，幂等且自动以保留用户 `system-scheduler` 写入审计链。可用 `--as-of YYYY-MM-DD` 在测试中模拟日期。

