"""按物品重建保管链的原始扫描。

历史上 :func:`latest` 只按 trip 取最后一条扫描，property_claim 见 handover 即
关单，于是同趟另一件物品（S-B 黑色公文包）的签收被误当成报失物（S-A 蓝色电脑
套）的归还。本模块保留 :func:`latest` 以供旧关单原件回读，但新增
:func:`item_chain` / :func:`custody` 按封签身份链逐件重建保管链，由
property_claim 据此判定单件物品的申领状态。
"""
from .property_identity import build_identity, item_of, receipt_valid

# 扫描种类 -> 保管语义
DEPOT_KINDS = ('depot',)            # 场站库房入库
TRANSFER_KINDS = ('handover',)      # 交给另一主体（乘客领取、司机移交等）
SEAL_KINDS = ('open', 'reseal')     # 开封/重封，只改封签不换保管人


def latest(scans, trip):
    """按 trip 取最后一条扫描（仅用于复现旧版 latest→decide 关单原件）。

    注意：该函数不区分物品，不能用于驱动单件物品的申领结论。
    """
    return next((s for s in reversed(scans) if s['trip'] == trip), None)


def trip_scans(data, trip):
    return sorted((s for s in data.get('scans', []) if s.get('trip') == trip),
                  key=lambda s: (s.get('at', ''), s.get('id', '')))


def _receipt_index(data):
    return {r.get('scan_id'): r for r in data.get('receipts', [])}


def item_chain(data, seal):
    """按物品身份链取该件实物的全部扫描步骤（含合法开封/重封后的新封签）。

    封签号经 property_identity.build_identity 缝成物品链；链上任一封签的扫描
    都属于同一件实物。返回按 (时间, 扫描编号) 排序的步骤，每步带签收校验结果。
    """
    modules = build_identity(data)
    item_id = item_of(modules, seal)
    if item_id is None:
        return []
    seals = set(modules[item_id]['seals'])
    receipts = _receipt_index(data)
    steps = []
    for scan in data.get('scans', []):
        if scan.get('seal') not in seals:
            continue
        rec = receipts.get(scan['id'])
        steps.append({'scan': scan,
                      'receipt': rec,
                      'receipt_valid': receipt_valid(rec, scan) if rec else False,
                      'item_id': item_id})
    return sorted(steps, key=lambda step: (step['scan'].get('at', ''),
                                           step['scan'].get('id', '')))


def custody(data, seal):
    """逐件推断当前保管人：每次转移必须有双方签收，缺口时保持当前保管人待核。

    - depot 扫描：物品在场站库房，保管人为扫描 custodian；
    - handover/open/reseal：只有匹配且双方签收有效的签收原件才能转移保管人；
      单方扫描或签收缺口时保持当前保管人，状态置为 ``待核``；
    - open/reseal 不改变保管人，只反映封签状态（重封后仍为封存）。

    返回当前保管快照与可审计的保管链步骤。
    """
    chain = item_chain(data, seal)
    custodian = None
    sealed = None
    place = None
    for step in chain:
        scan = step['scan']
        kind = scan.get('kind')
        if kind in DEPOT_KINDS:
            # 入库扫描即库房占有的起点；若有双方签收原件则一并附在链上。
            custodian = scan.get('custodian')
            place = scan.get('place') or 'depot'
            sealed = True
            step['effect'] = 'depot_intake'
        elif kind in TRANSFER_KINDS:
            if step['receipt_valid']:
                custodian = step['receipt']['to_party']
                place = step['receipt'].get('place')
                step['effect'] = 'transferred'
            else:
                # 硬约束二：不允许以单方扫描推断已归还，保持当前保管人待核。
                step['effect'] = 'gap_pending'
        elif kind in SEAL_KINDS:
            sealed = (kind == 'reseal')
            step['effect'] = 'seal_change'
        else:
            step['effect'] = 'recorded'
    last = chain[-1] if chain else None
    pending = bool(last and last['effect'] == 'gap_pending')
    return {'item_id': last['item_id'] if last else None,
            'seal': seal,
            'custodian': custodian,
            'place': place,
            'sealed': sealed,
            'pending_receipt': pending,
            'chain': chain}


def all_items(data):
    """身份模块中每件实物的 item_id 与其封签链。"""
    return build_identity(data)
