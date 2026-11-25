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

## 路面热点回源复核（road-spike-replay）

`fixtures/road-spike-replay.json` 起于三份报文与已外发的 `ANN-R17` confirmed 公告：`FLEET-41` 与 `VENDOR-882` 是同一 CAB-7 探头 `ACC-7#E-901` 的两次转发，`CAB-9-NEG` 为独立阴性。`taxiproof/suspension.py` 分三层去重——`report_id` 重放、规范包哈希、物理见证键 `(车辆, 探头, 源事件)` 与跨代理源图；只有第三层给见证票，因此重票折成一票，而数值完全相同的不同车辆不会被哈希误并。

`taxiproof/road_capture.py` 做 HMAC-SHA256 报文/证据验签、多跳回源链校验、WGS84↔本地米制/链距换算（另附 GCJ-02），并用 SQLite 事务保存报文、证据、物理源状态、版本化公告、路格状态与市政维修派工。`taxiproof/roadmap.py` 以「轨迹误差＋里程计偏移＋路侧锚点/市政测量」的可证区间按路格投票，相邻路格的震动不会并成同一坑点；晚到校准只重算相交路格。

两条授权核验路线（各自独立库，均保留原三包与 ANN-R17 v1）：

- `A_probe_fault`：有效 E-901 车端回源签章＋事故前松动支架维修单 → 物理源标记 `faulty_probe`，R-17 `confirmed→unverified/待核`，旧 `MNT-R17-01` 派工单撤销，不形成两车见证派工。
- `B_independent_replay`：E-901 双回源链＋独立 CAB-10 同路格复驶＋RS-07 路侧锚点/市政测量 → 原两包仍折一票，CAB-7/CAB-10 为两个独立正向见证，校正区间落入 R-18，发布 `confirmed` 新公告并生成 `MNT-R18-CAB7-CAB10` 维修派工，旧单标记 `superseded`。

```bash
python -B -m pytest -q -p no:cacheprovider
```

该域只产出可发布道路损坏位置、独立见证范围与待核清单/市政维修依据，不涉及收费、乘客服务、司机工时、出租车派车或行车录像。
