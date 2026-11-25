# 校车乘降监护采证台

离线采录学生乘车权益、线路车辆调度、乘降监护交接三套证据。导入原始 JSON 后，SQLite 保留文件哈希、事件、各业务实体、当前裁定及每次修订历史。迟到证据会从全部原始事件重算当前状态。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m schoolride demo --db demo.sqlite
python -m schoolride audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独采录可使用 `roster-import`、`route-import`、`handoff-import`，均传入 `--db` 和 `--file`。`fixtures/*-ok.json` 与 `fixtures/*-bad.json` 可分别复现正常和迟到异常；`report` 输出当前决策、证据 ID、实体状态与来源摘要。同哈希重放幂等，同事件 ID 异内容会使整个导入事务回滚。

演示最后依次导入 `roster-change.json`、`route-change.json`、`handoff-change.json`：已确认的 MUSEUM 改站先使原线路漏站、下车和接领待核，线路补站与新扫描证据到达后恢复完整承诺及保管转移。早上上车仍按原站点，下午下车和接领按已确认的新站点。

## 轮胎轮位实装（tyre fitment）

`fixtures/tyre-fitment.json` 是两辆前轴待装校车与六条实体胎的证据基线。朴素口径
`tyre_catalog.eligible` 只核单胎（规格 / 载重指数 / 4.0 mm 花纹），
`axle_plan.suggest` 每车取仓库顺序头两条，因此种子测试如实记录了缺陷：

- BUS-H（140 km，LI≥150）与 BUS-L（50 km，LI≥140）都拿到 T1/T2，同一对实体胎被双占，双车却同时 `ready`；
- T1 复测补正为 3.5 mm 后，BUS-H 改取 T2/T3，两胎单胎都 eligible，但同轴胎纹差 1.8 mm > 夹具上限 0.5 mm。

`schoolride/tyre_capture.py` 在不改写上述两个“缺陷见证”函数的前提下，补齐轮位实装采录、全局组配与变更报告：

- **成对车轴条件** `axle_check`：车系规格、两胎各自载重指数、相容花纹、同轴胎纹差 ≤ 0.5 mm、当前未隔离，一次成组核销；两个单胎 eligible 不为真推出成对可用。
- **全局唯一实物** `enumerate_allocations` / `build_plans`：跨两车 DFS 枚举实体 ID 不重复的完整合法组配；`FitmentLedger` 保证每条胎任一时刻只在一个库位 / 领出单 / 轮位 / 隔离位。
- **至少两套可比较方案**（管理员比较，而非固定仓库顺序）：

  | 方案 | BUS-H | BUS-L | 留库备胎 | 备胎寿命（高于 4 mm 的余量） | 下一次 LI≥150 车可用 |
  | --- | --- | --- | --- | --- | --- |
  | 甲 A | T1/T2（5.0/5.2） | T5/T6 | T3/T4 | 3.0 / 3.2 mm（更深） | T3/T4 |
  | 乙 B | T3/T4（7.0/7.2） | T5/T6 | T1/T2 | 1.0 / 1.2 mm（更浅） | T1/T2 |

  两套方案当前窗口都让双车放行，但逐车轮位安装证、领用时间、剩余备胎寿命与下一次高载重车库存不同。T1 晚到复测跌到 3.5 mm 后，甲同时触发 `TREAD_DEPTH_A` 与 `TREAD_DELTA`（T1/T2 差 1.7 mm）而失效，乙（T3/T4）仍可完整执行。

- **载重置换反例**：把 T3/T4 给 BUS-L、T5/T6 留给 BUS-H 过不了载重——T5/T6 的 LI=143 < BUS-H 要求的 150（`LOAD_INDEX_A/B`），尽管 T5/T6 同轴差只有 0.1 mm；T3/T4 上 BUS-L 本身合法，错在高要求车被分到低载重胎。
- **迟到证据与占用**：测量复核 append-only，原读数与 `original_tread_mm` 保留；隔离胎即使仍出现在旧 `suggest` / 旧方案里也被 `QUARANTINED_*` 拦下不得放行；领出 / 退回记录实际时间，退回前轮胎处于 checkout/position 占用，隔离退回后进 quarantine 位，仍不可领用。
- **发车锁定与撤销留痕**：`dispatch_bus` 后轮位 / 实物锁定，后续工单重排（`reorder_work_orders`）与任何改派提交都抛 `LockedPositionError`；撤销旧证不删除历史轮位、实物去向、受影响车次与已跑里程，只把车辆移出行车承诺。
- **放行闸门**：`dispatch_commitments` 同时要求每个轮位有效且签收的装配证、当前成对条件仍成立，以及 `rules_clearance` 调用原始 `schoolride.rules.calculate` 得到的检验 / 证照（含座位）有效。
- **可复算持久化**：`TyreStore` / `tyre_capture.connect` 用 `tyre_source_files`（SHA-256 原始字节）+ `tyre_events` 事件溯源；同哈希幂等、同 event_id 异内容整体回滚，`audit()` 在内存库重放全部来源并比较报告。既有 `schoolride.store` 的 roster/route/handoff 哈希重放 audit 不受影响。

为什么“逐条验胎 / 按 ID 先来先得 / 只修同轴差值 / 在旧出车结论上加轮胎布尔位”不够：它们都没有跨两辆车做实体 ID 冲销，也没有把成对兼容、迟到隔离后的存量占用和已发车锁定作为全局约束，因此双占、T2/T3 错配与“隔离胎仍放行”都会漏网。

```bash
python -B -m pytest -q -p no:cacheprovider
```

