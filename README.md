# 纸坊污水站加药与达标排放系统

纯 Python 3 标准库实现(`http.server` + `sqlite3`,WAL 模式),零第三方依赖。
数据持久化在 `data/wastewater.db`,重启后状态完整恢复。

## 运行

```bash
python3 server.py          # 默认 http://127.0.0.1:3039
PORT=8080 WTP_DB=/tmp/x.db python3 server.py
```

浏览器打开 `http://127.0.0.1:3039`。演示账号(密码均为 `123456`):

| 账号 | 姓名 | 角色 | 权限 |
|---|---|---|---|
| `admin` | 系统管理员 | ADMIN | 全部(仍受"投加/复检不同人"约束) |
| `leader01` | 王班长 | LEADER | 交班、接班、紧急停排 |
| `op01` / `op02` | 李投加 / 赵副操 | OPERATOR | 开单、投加、开阀、关阀、停排 |
| `qc01` | 陈复检 | INSPECTOR | 复检 |

## 测试

```bash
python3 -m unittest discover -s tests -v    # 22 个用例
```

## 业务规则与实现

| 需求 | 实现 | 验证测试 |
|---|---|---|
| 班组完成交接后才能接班 | 班次状态机 `ON_DUTY→HANDED_OVER→CLOSED`,部分唯一索引保证全站仅一个活动班次;无在岗班次时业务操作一律 409 | `TestShiftHandover` |
| 进水/出水超量程或格式错误整单拒绝 | 统一校验(缺失/非数字/NaN/越界 → 422 + 错误明细),校验通过前不落任何数据 | `TestWaterQualityValidation` |
| 按当前批准配方和水质区间计算投加量 | 全站唯一 `APPROVED` 配方(部分唯一索引),配方行按 COD/SS 区间 `[low,high)` 给投加率,`投加量=率×水量/1000`,区间未覆盖 → 409 | `TestDosingCalculation` |
| 库存不足不得出库 | 投加执行时条件更新 `UPDATE ... WHERE stock_qty>=?`,不足则抛错回滚 | `TestDosingAndStock` |
| 投加人与复检人不能同人 | 复检时比对 `dosed_by` 与当前用户,相同 → 409(ADMIN 也不例外) | `TestPermissions` |
| 复检不合格自动停排 | 复检事务内:若不合格且存在 OPEN 批次,同事务置批次 `STOPPED`、订单 `STOPPED` | `TestDischargeRules` |
| 未复核不得开阀 | 仅 `RECHECKED` 状态可开阀,其余 409 | `TestDischargeRules` |
| 排口同一时刻只能有一个批次 | 部分唯一索引 `ux_open_batch_per_outlet` + 事务内预检 | `TestDischargeRules` |
| 重复请求返回原结果 | `Idempotency-Key`(请求头或 body)与业务写入同事务落库,重放返回原响应并带 `X-Idempotent-Replay: true`;同键不同体 → 409 | `TestIdempotency` |
| 并发开阀只能成功一次 | `BEGIN IMMEDIATE` 串行化写事务 + 唯一索引兜底,8 线程并发仅 1 个 201 | `TestConcurrency` |
| 药耗/库存/水质/排放状态同成功同回滚 | 所有多步写入在单个 `BEGIN IMMEDIATE` 事务内,任一步失败整体 `ROLLBACK` | `test_insufficient_stock_rolls_back_everything` |
| 越权 | 角色→动作白名单,未登录 401、越权 403 | `TestPermissions` |
| 重启恢复 | 全部状态在 SQLite;杀进程重启后班次/库存/开阀/会话/幂等键均恢复 | `TestRestartRecovery` |

## 页面

单页应用五个页签:**交接班**(交班/接班/班次记录)、**加药**(开单自动算量、执行投加出库)、
**复检**(出水三指标录入,不合格自动停排)、**排放**(排口状态、开阀/关阀/紧急停排、幂等重放演示)、
**看板**(库存、批准配方、药耗、库存流水、审计日志)。

## API 一览

```
POST /api/login                      登录
GET  /api/state                      总览(班次/库存/配方/排口/单据/批次/审计)
POST /api/shifts/handover            交班(LEADER)
POST /api/shifts/takeover            接班(LEADER)
POST /api/dosing-orders              开单(OPERATOR,幂等)
POST /api/dosing-orders/{id}/dose    投加出库(OPERATOR,幂等)
POST /api/dosing-orders/{id}/recheck 复检(INSPECTOR,幂等)
POST /api/discharge/open             开阀(OPERATOR,幂等)
POST /api/discharge/{batch}/close    关阀(OPERATOR)
POST /api/discharge/stop             紧急停排(OPERATOR/LEADER)
POST /api/chemicals/{code}/restock   入库(ADMIN)
GET  /api/dosing-orders/{id}         单据详情
GET  /api/public-info                账号列表(免登录)
```

内置配方 `V2026.09`:PAC 按进水 COD `[0,150)/[150,300)/[300,∞)` → `60/90/120 g/m³`;
PAM 按进水 SS `[0,200)/[200,400)/[400,∞)` → `1.0/1.5/2.0 g/m³`。
达标限值:COD≤50、SS≤10、pH 6–9。
