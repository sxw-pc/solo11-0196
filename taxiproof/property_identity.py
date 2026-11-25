"""物品身份与签收采录。

每件实物以封签号锚定，开封/重封事件把旧封签与新封签缝成同一条物品身份链；
同一封签号被第二条物品链重用即触发 :class:`PropertyIdentityError`，封签重用
不能冒充同物。司机、场站库房、乘客之间的每次实物转移都用一条双方签收原件
（:func:`receipt`）固定，单方扫描不构成有效转移。

本模块只负责采录与原件校验，不产生关单结论；结论由 property_claim 在按物品
重建的身份链与保管链之上作出。
"""
import hashlib
import json

CANON_KW = dict(ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class PropertyIdentityError(ValueError):
    """物品身份链冲突：封签重用或同一封签指向了两件不同实物。"""


def canonical(payload):
    """原件规范化字节，用于内容身份（哈希）与重放比对。"""
    return json.dumps(payload, **CANON_KW).encode('utf-8')


def content_hash(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


def _require(record, fields):
    missing = [f for f in fields if record.get(f) in (None, '')]
    if missing:
        raise ValueError(f"采录记录缺少必填字段: {','.join(missing)}")


def signature(party, receipt_id):
    """确定性签收标记：绑定签收人与签收原件编号，双方各签各的。"""
    if not party or not receipt_id:
        raise ValueError('签收必须绑定签收人与签收原件编号')
    return f'SIG:{party}:{receipt_id}'


def signature_valid(signature_value, party, receipt_id):
    return signature_value == signature(party, receipt_id)


def photo(seal, description, source, at=None, role=None):
    """采录一张照片原件。

    role 标明证据用途，供逐件匹配：``loss`` 报失照片、``discovery`` 车内发现
    照片、``depot`` 库房入库照片、``handover_footage`` 交接连续影像。
    """
    record = {'seal': seal, 'description': description, 'source': source}
    if at is not None:
        record['at'] = at
    if role is not None:
        record['role'] = role
    _require(record, ('seal', 'description', 'source'))
    record['sha256'] = content_hash(record)
    return record


def registration(seal, description, source, at=None, item_hint=None):
    """失物登记/入库登记；item_hint 是登记员指认的物品链，用于发现贴错标签。"""
    record = {'seal': seal, 'description': description, 'source': source}
    if at is not None:
        record['at'] = at
    if item_hint is not None:
        record['item_hint'] = item_hint
    _require(record, ('seal', 'description', 'source'))
    record['sha256'] = content_hash(record)
    return record


def seal_event(scan_id, kind, old_seal, new_seal, at, custodian, reason):
    """开封（open）/重封（reseal）记录：把两枚封签缝到同一物品身份链上。

    重封时 old_seal 是拆下的封签、new_seal 是新封签；新封签若已属于另一条
    物品链，构建身份模块时会抛出 :class:`PropertyIdentityError`。
    """
    if kind not in ('open', 'reseal'):
        raise ValueError("seal_event.kind 仅支持 open/reseal")
    record = {'scan_id': scan_id, 'kind': kind, 'old_seal': old_seal,
              'new_seal': new_seal, 'at': at, 'custodian': custodian, 'reason': reason}
    _require(record, ('scan_id', 'old_seal', 'new_seal', 'at', 'custodian', 'reason'))
    record['sha256'] = content_hash(record)
    return record


def receipt(receipt_id, scan, from_party, to_party, at, place,
            from_signature, to_signature):
    """采录一次实物转移的双方签收原件。

    双方签名各自绑定本人与本签收编号（见 :func:`signature`），并记录时间地点；
    :func:`receipt_valid` 再核对签收与扫描逐件匹配。单方签名无法通过校验，
    保管缺口期间保持当前保管人待核。
    """
    record = {'id': receipt_id, 'scan_id': scan['id'], 'seal': scan['seal'],
              'trip': scan.get('trip'), 'from_party': from_party, 'to_party': to_party,
              'at': at, 'place': place,
              'from_signature': from_signature, 'to_signature': to_signature}
    _require(record, ('id', 'scan_id', 'seal', 'from_party', 'to_party',
                      'at', 'place', 'from_signature', 'to_signature'))
    record['sha256'] = content_hash(record)
    return record


def receipt_valid(rec, scan):
    """签收原件与扫描逐件匹配：封签号、时间地点、交出/接收双方与双方签名。"""
    if not rec or rec.get('seal') != scan.get('seal'):
        return False
    if rec.get('at') != scan.get('at'):
        return False
    if rec.get('scan_id') != scan.get('id'):
        return False
    if scan.get('kind') in ('depot',):
        # 入库扫描的 custodian 是接收方库房，交出方是送货司机。
        if rec.get('to_party') != scan.get('custodian'):
            return False
    else:
        if rec.get('from_party') != scan.get('custodian'):
            return False
        # 非入库扫描必须与 recipient 对齐。
        if scan.get('recipient') is not None and rec.get('to_party') != scan.get('recipient'):
            return False
    if not rec.get('place'):
        return False
    return (signature_valid(rec.get('from_signature'), rec['from_party'], rec['id'])
            and signature_valid(rec.get('to_signature'), rec['to_party'], rec['id']))


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _source_id(record):
    """原件短编号：优先采录时固化的 sha256，原始数据则现算内容哈希。"""
    return record.get('sha256') or content_hash(record)


def _parent_seals(data):
    """身份链缝合所涉及的全部封签：扫描、开封/重封、登记与照片原件。"""
    seals = {s['seal'] for s in data.get('scans', [])}
    seals.update(e['old_seal'] for e in data.get('seal_events', []))
    seals.update(e['new_seal'] for e in data.get('seal_events', []))
    seals.update(r['seal'] for r in data.get('registrations', []))
    seals.update(p['seal'] for p in data.get('photos', []))
    return seals


def build_identity(data):
    """按物品重建身份模块：并查集缝合开封/重封，检测封签重用。

    返回 ``{item_id: {'seals', 'sources', 'identity_version'}}``。item_id 取该链
    最早出现的封签号；同一 new_seal 被两次重封使用，或登记/照片把同一封签指到
    两条不同物品链，均抛出 :class:`PropertyIdentityError`。
    """
    uf = _UnionFind()
    for seal in _parent_seals(data):
        uf.find(seal)
    reused = set()
    for event in data.get('seal_events', []):
        if event['new_seal'] in reused:
            raise PropertyIdentityError(f"封签重用: {event['new_seal']} 被两次重封")
        reused.add(event['new_seal'])
        uf.union(event['old_seal'], event['new_seal'])

    # 登记员/照片对封签的物品指认必须与缝合后的链一致，否则是贴错标签或封签冒充。
    hinted = {}

    def bind(seal, hint, source_id):
        root_s, root_h = uf.find(seal), uf.find(hint)
        if root_s != root_h:
            raise PropertyIdentityError(
                f"封签 {seal} 与指认物品 {hint} 不属于同一身份链（来源 {source_id}）")
        prev = hinted.get(seal)
        if prev and prev != root_s:
            raise PropertyIdentityError(f"封签 {seal} 被指认为两件不同实物")
        hinted[seal] = root_s

    for reg in data.get('registrations', []):
        if reg.get('item_hint'):
            bind(reg['seal'], reg['item_hint'], _source_id(reg)[:12])
    for pic in data.get('photos', []):
        if pic.get('item_hint'):
            bind(pic['seal'], pic['item_hint'], _source_id(pic)[:12])

    groups = {}
    for seal in uf.parent:
        groups.setdefault(uf.find(seal), []).append(seal)
    modules = {}
    for root, seals in groups.items():
        ordered = sorted(seals)
        item_id = ordered[0]
        sources = sorted(
            [s['id'] for s in data.get('scans', []) if s['seal'] in seals]
            + [e['scan_id'] for e in data.get('seal_events', [])
               if e['old_seal'] in seals or e['new_seal'] in seals]
            + [_source_id(r)[:12] for r in data.get('registrations', []) if r['seal'] in seals]
            + [_source_id(p)[:12] for p in data.get('photos', []) if p['seal'] in seals])
        modules[item_id] = {'seals': ordered, 'sources': sources,
                            'identity_version': content_hash(
                                {'item_id': item_id, 'seals': ordered, 'sources': sources})[:16]}
    return modules


def item_of(identity_modules, seal):
    """封签号 -> 物品链 item_id；未知封签返回 None。"""
    for item_id, module in identity_modules.items():
        if seal in module['seals']:
            return item_id
    return None


def describe(data, seal):
    """汇总一枚封签在登记/照片原件中的外观描述（逐件匹配的输入）。"""
    return [r.get('description') for r in data.get('registrations', [])
            if r['seal'] == seal and r.get('description')] + \
           [p.get('description') for p in data.get('photos', [])
            if p['seal'] == seal and p.get('description')]
