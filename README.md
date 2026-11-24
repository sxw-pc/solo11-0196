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

## 乘客紧急报警的并发派遣

`taxiproof/safety_alert.py`、`taxiproof/safety_response.py`、`taxiproof/safety_capture.py` 与 `fixtures/safety-incident.json` 实现可追溯的紧急响应，核验命令：

```powershell
python -B -m pytest -q -p no:cacheprovider
```

- 乘客紧急按钮未经可信人工回访撤销前始终有效；司机单方 `driver_ack` 只入日志，不能关单（`actionable` 仍为真）。
- `plan_responses` 对全部未关闭报警跨报警、候选位置与人员时间窗联合规划：承诺的到场时限按每个候选位置的最坏 ETA 衡量（首援 15 分钟），不取报告坐标平均；同一人员在同一窗口只能承担一条不可中断首援任务，远程联系单独记录、不算到场。
- 人员资格同时校验在班标志、覆盖规划时刻的班次窗口、`license_until`/`permit_until` 与首援资质，过期班次无法借 `on_duty` 绕过；`authorized_trips` 按行程做位置披露的默认拒绝授权。
- 无解时不输出看似可行的单点计划：返回每条报警的逐人员可行性证据并升级 `external_emergency_services`。
- `ResponseCapture` 以只追加日志保存派遣、撤销、到场、远程联系与升级；迟到的可信定位或 ETA 修正只重算未到场任务，撤派保留 `assignment_cancelled` 与指名旧响应员的 `handover` 证据，不静默覆盖。`public_status` 仅暴露已受理、响应区间与复核版本，不含候选位置。
