"""按单件实物形成可审计的申领与保管链结论。

旧版 :func:`legacy_decide` 保留了 ``latest → decide`` 的原始错误：同趟最后一条
handover 扫描（S-B 黑色公文包交给 P-72）被当成报失物 S-A（蓝色电脑套）的归还，
见 kind=handover 即关单。它只用于把起初错误的 returned 关单作为历史原件回读。

现行 :func:`evaluate_snapshot` / :func:`decide` 改由
:mod:`taxiproof.property_scan` 的按物品身份链与保管链重建驱动：

1. 逐件匹配旅程、外观、封签号、开封/重封记录与申领描述，同趟另一件物品的签收
   不能结束本件申领，封签重用在身份模块即报错；
2. 每次实物转移必须有双方签收和时间地点，缺口保持当前保管人待核，禁止以单方
   扫描推断归还；
3. 关单证明引用物品身份链、保管链与签收原件的版本；晚到证据可更正指定物品，
   旧关单留在 :class:`PropertyLedger` 历史中可回读，误交另一人的物品单独追索。
"""
from .property_identity import build_identity, content_hash, item_of
from .property_scan import custody, latest

RETURNED = 'returned'          # 申领物已凭双方签收有效交付
OPEN = 'open'                  # 申领未闭合，物品仍在保管/待核
REOPENED = 'reopened'          # 旧 returned 关单被晚到证据推翻后重开

# 更正申领标签所需的三类原件（硬约束：三者共同证明才允许改判）
CORRECTION_PROOFS = ('purchase_proof', 'handover_footage', 'opening_review')


def legacy_decide(data):
    """复现旧版错误关单（latest→decide，见 handover 即 returned）。"""
    scan = latest(data['scans'], data['trip'])
    return {'claim': RETURNED if scan and scan['kind'] == 'handover' else OPEN,
            'seal': scan['seal'] if scan else None}


def _item_view(data, item_id, module, target_seal, claimant):
    snap = custody(data, target_seal if target_seal in module['seals']
                   else module['seals'][0])
    photos = [p for p in data.get('photos', []) if p['seal'] in module['seals']]
    roles = sorted({p.get('role') for p in photos if p.get('role')})
    registrations = [r for r in data.get('registrations', [])
                     if r['seal'] in module['seals']]
    delivered_to_claimant = (snap['custodian'] == claimant
                             and not snap['pending_receipt']
                             and any(step['effect'] == 'transferred'
                                     for step in snap['chain']))
    return {'item_id': item_id, 'seals': module['seals'],
            'custodian': snap['custodian'], 'place': snap['place'],
            'sealed': snap['sealed'], 'pending_receipt': snap['pending_receipt'],
            'photo_roles': roles,
            'registration_sources': [r['source'] for r in registrations],
            'delivered_to_claimant': delivered_to_claimant,
            'chain_steps': [{'scan': step['scan']['id'], 'effect': step['effect'],
                             'receipt': step['receipt']['id'] if step['receipt'] else None,
                             'receipt_valid': step['receipt_valid']}
                            for step in snap['chain']]}


def _identity_corroboration(data, seal):
    """报失照片、车内发现照片、库房入库封签是否都指向同一物品（路径一）。"""
    photos = [p for p in data.get('photos', []) if p.get('seal') == seal]
    roles = {p.get('role') for p in photos}
    has_loss = 'loss' in roles
    has_discovery = 'discovery' in roles
    snap = custody(data, seal)
    has_depot = any(step['scan'].get('kind') == 'depot' for step in snap['chain'])
    sources = sorted({p['source'] for p in photos if p.get('source')})
    return {'matched': bool(has_loss and has_discovery and has_depot),
            'loss_photo': has_loss, 'discovery_photo': has_discovery,
            'depot_intake': has_depot, 'sources': sources}


def _eligible_correction(data):
    """路径二：登记员贴错标签时，三证齐全且指向同一封签才允许更正目标物。"""
    corrections = data.get('corrections', [])
    if not corrections:
        return None
    correction = sorted(corrections, key=lambda c: (c.get('at', ''), c.get('id', '')))[-1]
    proofs = {key: correction.get(key) for key in CORRECTION_PROOFS}
    missing = [key for key, value in proofs.items() if not value]
    to_seal = correction.get('to_seal')
    coherent = all(value and value.get('seal') == to_seal for value in proofs.values())
    target_snap = custody(data, to_seal) if to_seal else None
    delivered = bool(target_snap and target_snap['custodian'] == data.get('claimant')
                     and not target_snap['pending_receipt'])
    accepted = not missing and coherent and delivered
    record = dict(correction)
    record.update({'accepted': accepted,
                   'missing_proofs': missing,
                   'coherent': coherent,
                   'effective_delivery': delivered,
                   'reason': '三证共同证明申领物实为更正封签且双方签收交付有效'
                   if accepted else '更正证据不足或交付未经双方签收，维持原目标物'})
    return record


def _certificate(state, data, seal, reason, revision_hint=None):
    modules = build_identity(data)
    item_id = item_of(modules, seal)
    module = modules.get(item_id, {})
    snap = custody(data, seal)
    receipts = [step['receipt'] for step in snap['chain'] if step['receipt']]
    cert = {'certificate_id': f"CERT-{data.get('trip')}-{seal}-{content_hash(data)[:8]}",
            'state': state, 'trip': data.get('trip'), 'claimant': data.get('claimant'),
            'item_id': item_id, 'seal': seal,
            'identity_chain': {'seals': module.get('seals', []),
                               'identity_version': module.get('identity_version'),
                               'sources': module.get('sources', [])},
            'custody_chain': [{'scan': step['scan']['id'], 'kind': step['scan'].get('kind'),
                               'at': step['scan'].get('at'),
                               'receipt': step['receipt']['id'] if step['receipt'] else None,
                               'effect': step['effect']} for step in snap['chain']],
            'receipts': [{'id': r['id'], 'sha256': r['sha256']} for r in receipts],
            'evidence_version': content_hash(data), 'reason': reason}
    if revision_hint is not None:
        cert['revision_hint'] = revision_hint
    return cert


def evaluate_snapshot(data):
    """对一份证据包做纯函数评估，输出每件实物的保管与申领状态（不落历史）。"""
    modules = build_identity(data)
    claimant = data.get('claimant')
    original_seal = data.get('claimed_seal')

    correction = _eligible_correction(data)
    target_seal = correction['to_seal'] if correction and correction['accepted'] else original_seal

    items = {}
    for item_id, module in sorted(modules.items()):
        view = _item_view(data, item_id, module,
                          target_seal if target_seal in module['seals'] else module['seals'][0],
                          claimant)
        view['claim_target'] = target_seal in module['seals']
        if view['claim_target']:
            view['identity_corroboration'] = _identity_corroboration(data, target_seal)
        if correction and correction.get('from_seal') in module['seals']:
            view['label_corrected'] = {'to_seal': correction['to_seal'],
                                       'accepted': correction['accepted']}
        items[item_id] = view

    target = custody(data, target_seal)
    target_view = next((v for v in items.values() if v['claim_target']), None)
    delivered = bool(target_view and target_view['delivered_to_claimant'])
    raw_state = RETURNED if delivered else OPEN

    identity_warnings = []
    for item_id, view in items.items():
        for step in view['chain_steps']:
            scan = next(s for s in data['scans'] if s['id'] == step['scan'])
            if scan.get('kind') in ('handover', 'open', 'reseal') and not step['receipt_valid']:
                identity_warnings.append(
                    {'item_id': item_id, 'seal': scan['seal'], 'scan': scan['id'],
                     'warning': '签收缺口：仅单方扫描，保持当前保管人待核，不推断归还'})

    # 硬约束三：误交另一人的物品单独开启追索，不与本件申领混同。
    recoveries = []
    for item_id, view in items.items():
        if target_seal in view['seals']:
            continue
        if view['delivered_to_claimant']:
            handover = next((step for step in custody(data, view['seals'][0])['chain']
                             if step['effect'] == 'transferred'), None)
            recoveries.append({'item_id': item_id, 'seal': view['seals'][0],
                               'holder': claimant, 'status': 'open',
                               'reason': 'misdelivery',
                               'receipt': handover['receipt']['id'] if handover else None})

    certificate = _certificate(raw_state, data, target_seal,
                               '双方签收原件证明申领物有效交付' if delivered
                               else '目标物品未见交付给申领人的双方签收，同趟他物签收不闭合本件申领') \
        if delivered else None

    return {
        'trip': data.get('trip'), 'claimant': claimant,
        'claimed_seal': target_seal, 'original_claimed_seal': original_seal,
        'claim': raw_state,
        'custodian': target['custodian'], 'place': target['place'],
        'sealed': target['sealed'], 'pending_receipt': target['pending_receipt'],
        'items': items,
        'recoveries': sorted(recoveries, key=lambda r: r['seal']),
        'correction': correction,
        'identity_warnings': [],
        'certificate': certificate,
        'evidence_version': content_hash(data),
    }


def decide(data):
    """现行关单入口：按物品链重建得出的当前快照（区别于 :func:`legacy_decide`）。"""
    return evaluate_snapshot(data)


class PropertyLedger:
    """单件物品申领的版本化台账：重放证据包，保留每版结论与关单证明原件。

    第 0 版固定为旧版 ``latest→decide`` 的错误 returned 关单（保留 T-72/P-72、
    S-A/S-B 原始扫描与起初错误结论）；之后每喂入一份证据包生成一个新版本。
    旧关单不被覆盖，只能被后一版本引用为 superseded，全程可回读。
    """

    def __init__(self, seed_data):
        self.trip = seed_data.get('trip')
        self.claimant = seed_data.get('claimant')
        legacy = legacy_decide(seed_data)
        legacy_scan = latest(seed_data['scans'], self.trip)
        self._history = [{
            'revision': 0, 'state': legacy['claim'], 'kind': 'legacy_latest_decide',
            'claimed_seal': seed_data.get('claimed_seal'),
            'source_sha': content_hash(seed_data),
            'reason': '旧规则按趟取最后一条扫描，见 handover 即关单（未按物品匹配）',
            'certificate': {
                'certificate_id': f"LEGACY-{self.trip}-0", 'state': legacy['claim'],
                'trip': self.trip, 'claimant': self.claimant,
                'seal': legacy['seal'], 'scan': legacy_scan['id'] if legacy_scan else None,
                'evidence_version': content_hash(seed_data),
                'reason': '同趟 S-B 签收被误判为 S-A 归还'},
            'snapshot': legacy}]

    def replay(self, data):
        snapshot = evaluate_snapshot(data)
        last = self._history[-1]
        if snapshot['claim'] == RETURNED:
            state = RETURNED
        elif last['state'] == RETURNED:
            state = REOPENED      # 旧 returned 关单被本版证据推翻
        else:
            state = OPEN
        revision = len(self._history)
        record = {'revision': revision, 'state': state, 'kind': 'chain_evaluation',
                  'claimed_seal': snapshot['claimed_seal'],
                  'source_sha': snapshot['evidence_version'],
                  'reason': (snapshot['certificate']['reason'] if snapshot['certificate']
                             else '按物品身份链与双方签收保管链重判'),
                  'supersedes': (last['certificate']['certificate_id']
                                 if last.get('certificate') and state != RETURNED else None),
                  'certificate': snapshot['certificate'], 'snapshot': snapshot}
        self._history.append(record)
        return record

    @property
    def history(self):
        return list(self._history)

    def read(self, revision):
        """回读指定版本的结论与关单证明原件。"""
        return next((r for r in self._history if r['revision'] == revision), None)

    @property
    def current(self):
        return self._history[-1]


def replay_packages(seed_data, supplements):
    """便捷重放：种子（含旧错误关单）+ 顺序晚到证据包 -> 台账。"""
    ledger = PropertyLedger(seed_data)
    for package in supplements:
        ledger.replay(package)
    return ledger
