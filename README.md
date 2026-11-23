# 出租车计价器运营采证台

离线采集计价器检定及费率、司机车辆班次、行程轨迹与支付申诉。逐段费率、交班占车和迟到证据共同重算应收与退款责任；SQLite 保留来源哈希、业务实体、当前状态和历史结论。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m taxiproof demo --db demo.sqlite
python -m taxiproof audit --db demo.sqlite
python -m unittest discover -s tests -v
```

`meter-import`、`shift-import`、`trip-import` 均需 `--db`、`--file`。正常和失败样例位于 `fixtures`；`report` 列出十项行程决策与原始证据。同哈希重放幂等，同事件 ID 异内容则事务回滚。

行程申诉可附 `complaint` 对象：`reason=driver_identity`、`vehicle`、`observed_driver`、`observed_at`，并提供 `passenger_signature` 与 `reviewer_signature`。两方签认且观察时间落在本次行程内时，身份不符会重开班次归责，冻结该行程结算；后续复核版本可以解除争议。单方或越界观察不会改变班次。
