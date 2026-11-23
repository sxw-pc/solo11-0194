# 校车乘降监护采证台

离线采录学生乘车权益、线路车辆调度、乘降监护交接三套证据。导入原始 JSON 后，SQLite 保留文件哈希、事件、各业务实体、当前裁定及每次修订历史。迟到证据会从全部原始事件重算当前状态。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m schoolride demo --db demo.sqlite
python -m schoolride audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独采录可使用 `roster-import`、`route-import`、`handoff-import`，均传入 `--db` 和 `--file`。`fixtures/*-ok.json` 与 `fixtures/*-bad.json` 可分别复现正常和迟到异常；`report` 输出当前决策、证据 ID、实体状态与来源摘要。同哈希重放幂等，同事件 ID 异内容会使整个导入事务回滚。
