# 出租车计价器运营采证台

离线采集计价器检定及费率、司机车辆班次、行程轨迹与支付申诉。逐段费率、交班占车和迟到证据共同重算应收与退款责任；SQLite 保留来源哈希、业务实体、当前状态和历史结论。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m taxiproof demo --db demo.sqlite
python -m taxiproof audit --db demo.sqlite
python -m unittest discover -s tests -v
```

`meter-import`、`shift-import`、`trip-import` 均需 `--db`、`--file`。正常和失败样例位于 `fixtures`；`report` 列出十项行程决策与原始证据。同哈希重放幂等，同事件 ID 异内容则事务回滚。
