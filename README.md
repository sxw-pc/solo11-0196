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

## 场站充电与出车计划

`fixtures/depot-peak.json` 描述 07:00–08:00 早班窗口：合同容量 30 kW，两支 22 kW CCS 枪，CAB-4（T-91 医疗）需 20 kWh、CAB-9（T-92 普通）需 18 kWh，CAB-10 为已充足电的备车。`taxiproof/depot_energy.py` 的 `required` 按充电效率、辅机、电池接受曲线与 SOC 上限把每单折算成站进线电量，并汇总相对合同的能量缺口；`taxiproof/depot_plan.py` 的 `plan` 以 5 分钟时段仿真，输出负荷曲线、每车能量余量、出车派单、未履约订单与最小冲突集合。

`fixtures/depot-auth.json` 采录配电授权与订单替车指令：临时增容仅在授权时段内且批准后生效（事后补单不能使当时超载合法化）；替车须满足司机、时窗、车况与原订单承诺且在承诺时限内授权；桩故障进入冷却期并触发受影响计划重排；取消指令与新出车承诺同样产生新修订版。每次重排生成一个修订版，仅最新版生效，历史版本可回读，任何修订内枪、车、容量不发生重复占用。无授权时基线计划拒绝默默超载：T-92 无法履约，最小冲突集合 {T-91, T-92} 缺口约 13.97 kWh。

```powershell
python -B -m pytest -q -p no:cacheprovider
```
