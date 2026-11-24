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

## 无障碍联合调度

`fixtures/accessibility-routes.json` 描述晨间两条线路争用全场唯一带轮椅升降装置的 BUS-A：R1（两名普通学生，07:30 固定上车）与 R2（一名轮椅学生，家长批准窗口 07:35–07:55）。普通座位空余不能抵消无障碍资格缺口。

- `schoolride/vehicle_match.py` 结合升降装置/固定点/司机资质、容量与路段窗口生成可行边，并把 `schoolride/rules.py` 的名册/车次权益作为必须通过的门禁；
- `schoolride/route_dispatch.py` 在全部线路与时段上联合回溯搜索，保证车辆与司机服务区间不重叠、空驶时间与交通区间计入接续，输出两条校方授权且均可兑现的排班路线，并保留无法同时履约的最小冲突集；
- `schoolride/dispatch_capture.py` 把计划固化为版本化派车证，迟到交通证据到达时显式撤销基于旧证据的派车证（禁止静默沿用），并生成可比较的计划证据与家长改时通知。

路线 A：BUS-A 给 R2、BUS-B 给 R1，两车按原时间窗接送，无故障备车。路线 B：在 R2 家长同意较晚窗口且空驶上界可信时，BUS-A 先跑 R1 再接 R2（R2 晚 20 分钟于 07:55 上车），BUS-B 保留为故障备车；空驶区间一旦超出家长窗口（交通证据 v2：25 分钟）自动回退路线 A。未获改时同意时不得用调时间消除资源冲突。

```powershell
python -B -m pytest -q -p no:cacheprovider
```

