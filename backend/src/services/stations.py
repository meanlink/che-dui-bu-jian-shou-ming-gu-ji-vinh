"""Per-station distribution of approved mileage estimates (frozen snapshots).

Delivery language
-----------------
* publish directive: freezes model / mileage / version at dispatch time.
* revoke directive: targets an explicit version and only counts as done once
  the station acknowledges it.  Until then the backend keeps it unresolved and
  the page shows "撤销待核对"; it never reports the version as revoked.
* retries reuse the persisted directive row and idempotency key, so the station
  can answer from its receipt ledger without taking effect twice.
* a late directive for an older version can never overwrite or withdraw a newer
  version at the station.
"""
import time
import uuid
from fastapi import HTTPException
from sqlalchemy import select
from src.models.station import Station, StationDirective
from src.models import Job
from src.services import station_sim
from src.services.audit import record
from src.utils.serialization import checksum

PENDING = ('pending', 'retrying', 'receipt_lost')
FATAL_ATTEMPTS = 5
BACKOFF_BASE = 2.0
BACKOFF_CAP = 30.0


def _now():
    return time.time()


def is_pending(directive):
    return directive.status in PENDING


def directive_dict(d):
    data = {k: getattr(d, k) for k in ['id', 'station_id', 'kind', 'version', 'status', 'snapshot',
                                       'idempotency_key', 'attempts', 'next_attempt_at', 'last_error',
                                       'receipt', 'created_at', 'updated_at', 'confirmed_at']}
    data['receipt_lost'] = bool((d.receipt or {}).get('receipt_lost'))
    data['pending'] = d.status in PENDING
    return data


def station_dict(s):
    return {k: getattr(s, k) for k in ['id', 'code', 'name', 'enabled', 'last_version', 'confirmed_version',
                                       'confirmed_directive_id', 'confirmed_snapshot', 'confirmed_at',
                                       'observed_version', 'observed_at', 'last_reconciled_at', 'created_at']}


def create_station(db, data):
    code = data.code.strip()
    name = data.name.strip()
    if db.scalar(select(Station).where(Station.code == code)):
        raise HTTPException(409, '站点编码已存在')
    station = Station(code=code, name=name, enabled=True, last_version=0, confirmed_version=None,
                      confirmed_directive_id=None, confirmed_snapshot={}, confirmed_at=None,
                      observed_version=None, observed_directive_id=None, observed_at=None,
                      last_reconciled_at=None, created_at=_now())
    db.add(station)
    db.flush()
    station_sim.ensure_replica(db, station.id)
    return station


def freeze_snapshot(job):
    """Immutable payload: model, mileage figures and the approved job version."""
    result = job.result or {}
    metrics = {m.get('label'): m.get('value') for m in result.get('metrics', [])}
    snapshot = {
        'job_id': job.id,
        'job_name': job.name,
        'model': job.params.get('model'),
        'reliability': job.params.get('reliability'),
        'mileage': metrics.get('目标可靠度里程'),
        'b10': metrics.get('B10寿命'),
        'shape': metrics.get('形状参数β'),
        'scale': metrics.get('尺度参数η'),
        'params': job.params,
        'summary': result.get('summary'),
        'metrics': result.get('metrics', []),
        'approved': True,
        'job_finished_at': job.finished_at,
    }
    snapshot['fingerprint'] = checksum({k: snapshot[k] for k in ('job_id', 'model', 'reliability', 'mileage', 'params')})
    return snapshot


def _station_for_update(db, station_id):
    """Lock the station row so concurrent publishes cannot allocate the same version."""
    return db.scalar(select(Station).where(Station.id == station_id).with_for_update())


def publish(db, job_id, station_ids, user):
    job = db.get(Job, job_id)
    if not job or job.status != 'completed' or not job.approved:
        raise HTTPException(409, '只能下发已完成且已批准的分析结果')
    snapshot = freeze_snapshot(job)
    seen, directives = set(), []
    for station_id in station_ids:
        if station_id in seen:
            continue
        seen.add(station_id)
        station = _station_for_update(db, station_id)
        if not station:
            raise HTTPException(404, f'站点 {station_id} 不存在')
        if not station.enabled:
            raise HTTPException(409, f'站点 {station.code} 已停用，不能下发')
        inflight = list(db.scalars(select(StationDirective).where(
            StationDirective.station_id == station.id, StationDirective.kind == 'publish',
            StationDirective.status.in_(PENDING))))
        for old in inflight:
            if (old.snapshot or {}).get('fingerprint') == snapshot['fingerprint']:
                raise HTTPException(409, f'站点 {station.code} 已有同批下发 v{old.version} 待核对回执，'
                                         f'请先对账或重试，勿重复下发')
        if station.confirmed_version is not None and \
                (station.confirmed_snapshot or {}).get('fingerprint') == snapshot['fingerprint']:
            raise HTTPException(409, f'该分析结果已是 {station.code} 的生效版本 v{station.confirmed_version}，'
                                     f'无需重复下发')
        station.last_version = (station.last_version or 0) + 1
        directive = StationDirective(
            station_id=station.id, kind='publish', version=station.last_version, status='pending',
            snapshot=snapshot, idempotency_key=uuid.uuid4().hex, attempts=0, next_attempt_at=_now(),
            last_error='', receipt={}, created_by=user.id, created_at=_now(), updated_at=_now(),
            sending_at=None, confirmed_at=None)
        db.add(directive)
        directives.append(directive)
    db.flush()
    for directive in directives:
        record(db, user.email, 'station.publish', {
            'directive': directive.id, 'station': directive.station_id, 'version': directive.version,
            'job': job.id, 'model': snapshot['model'], 'mileage': snapshot['mileage']})
    return directives


def revoke(db, station_id, version, user):
    station = _station_for_update(db, station_id)
    if not station:
        raise HTTPException(404, '站点不存在')
    if not station.enabled:
        raise HTTPException(409, '站点已停用，无法撤销')
    if station.confirmed_version is None:
        raise HTTPException(409, '站端尚未确认任何生效版本，无需撤销')
    target = version if version is not None else station.confirmed_version
    if target < 1 or target > (station.last_version or 0):
        raise HTTPException(422, f'版本 v{target} 不在该站点下发记录中')
    if target > station.confirmed_version:
        raise HTTPException(409, f'v{target} 尚未获得站端确认，不能撤销；请先核对回执')
    if target < station.confirmed_version:
        raise HTTPException(409, f'站端已生效 v{station.confirmed_version}，v{target} 已被取代，无需撤销')
    withdrawing = db.scalar(select(StationDirective).where(
        StationDirective.station_id == station.id, StationDirective.kind == 'revoke',
        StationDirective.status.in_(PENDING)))
    if withdrawing is not None:
        if withdrawing.version == target:
            return withdrawing
        raise HTTPException(409, f'站点已有撤销 v{withdrawing.version} 待站端确认，请先核对')
    directive = StationDirective(
        station_id=station.id, kind='revoke', version=target, status='pending',
        snapshot=dict(station.confirmed_snapshot or {}), idempotency_key=uuid.uuid4().hex,
        attempts=0, next_attempt_at=_now(), last_error='', receipt={}, created_by=user.id,
        created_at=_now(), updated_at=_now(), sending_at=None, confirmed_at=None)
    db.add(directive)
    db.flush()
    record(db, user.email, 'station.revoke.requested', {'directive': directive.id, 'station': station.id,
                                                       'version': target})
    return directive


def dispatch_one(db, directive):
    """One send attempt.  Returns True when the directive reached a terminal state."""
    if not is_pending(directive):
        return True
    station = db.get(Station, directive.station_id)
    if not station or not station.enabled:
        directive.status = 'blocked'
        directive.last_error = '站点已停用'
        directive.updated_at = _now()
        return True
    directive.attempts = (directive.attempts or 0) + 1
    directive.sending_at = _now()
    payload = {'id': directive.id, 'kind': directive.kind, 'version': directive.version,
               'idempotency_key': directive.idempotency_key, 'snapshot': directive.snapshot}
    try:
        receipt = station_sim.apply(db, directive.station_id, payload)
    except Exception as exc:  # station crashed / transport failure
        receipt = {'accepted': False, 'transport_error': True, 'reason': str(exc)[:300]}
    if receipt.get('offline'):
        # Connection refused: keep the directive pending, retry with backoff.
        directive.status = 'pending'
        directive.last_error = '站点离线，保持待下发'
        directive.next_attempt_at = _now() + min(BACKOFF_CAP, BACKOFF_BASE * directive.attempts)
        directive.updated_at = _now()
        directive.sending_at = None
        return False
    if receipt.get('held'):
        directive.status = 'pending'
        directive.last_error = '站端演练暂扣指令，等待释放'
        directive.next_attempt_at = _now() + 5
        directive.updated_at = _now()
        directive.sending_at = None
        return False
    if receipt.get('receipt_lost'):
        # The station applied it, but the receipt never came back.  Keep it
        # unresolved; the next attempt replays the ledger and takes no effect.
        directive.status = 'receipt_lost'
        directive.last_error = '站端已生效但回执丢失，重试将查回原结果'
        directive.receipt = {'receipt_lost': True, 'state': receipt.get('state', {})}
        directive.next_attempt_at = _now() + 3
        directive.updated_at = _now()
        directive.sending_at = None
        return False
    return _settle(db, directive, station, receipt)


def _settle(db, directive, station, receipt):
    directive.receipt = receipt
    directive.updated_at = _now()
    directive.sending_at = None
    if receipt.get('transport_error'):
        directive.status = 'retrying'
        directive.last_error = '站端通信异常：' + str(receipt.get('reason', ''))[:300]
        if directive.attempts >= FATAL_ATTEMPTS:
            directive.status = 'failed'
            directive.last_error = '连续多次无法送达站端，已停止自动重试；可手工重试或对账'
        directive.next_attempt_at = _now() + min(BACKOFF_CAP, BACKOFF_BASE * directive.attempts)
        return directive.status == 'failed'
    directive.last_error = ''
    directive.confirmed_at = _now()
    outcome = receipt.get('outcome')
    if directive.kind == 'publish':
        # "effective" only when this very version is the station's live version.
        # A replay of an older applied directive while a newer version is live
        # is reported as superseded: its one-time effect stands, but it must not
        # be shown as, nor become, the current station version.
        applied = outcome == 'applied' and receipt.get('effective_version') == directive.version
        directive.status = 'effective' if applied else 'superseded'
        _confirm_publish(station, directive, receipt)
    else:
        # A noop revoke means the station has nothing at or below the target
        # live (a newer version is effective); report it as superseded, never
        # as revoked.
        if outcome == 'revoked':
            directive.status = 'revoked'
            _confirm_revoke(station, directive, receipt)
        else:
            directive.status = 'superseded'
            _observe(station, receipt)
    return True


def _observe(station, receipt):
    station.observed_version = receipt.get('effective_version')
    station.observed_directive_id = receipt.get('effective_directive_id')
    station.observed_at = _now()


def _confirm_publish(station, directive, receipt):
    _observe(station, receipt)
    if directive.status != 'effective':
        return
    effective = receipt.get('effective_version')
    if effective is None:
        return
    if station.confirmed_version is not None and station.confirmed_version > effective:
        return
    station.confirmed_version = effective
    station.confirmed_directive_id = receipt.get('effective_directive_id')
    station.confirmed_snapshot = receipt.get('effective_payload') or directive.snapshot
    station.confirmed_at = _now()


def _confirm_revoke(station, directive, receipt):
    _observe(station, receipt)
    if directive.status != 'revoked':
        return
    effective = receipt.get('effective_version')
    if effective is not None and effective > directive.version:
        # A newer version became effective after this revoke; keep that truth.
        return
    if station.confirmed_version is not None and station.confirmed_version > directive.version:
        return  # a newer version was confirmed meanwhile; never report the old one revoked
    station.confirmed_version = effective
    station.confirmed_directive_id = receipt.get('effective_directive_id')
    station.confirmed_snapshot = receipt.get('effective_payload') or {}
    station.confirmed_at = _now()


def send_now(db, directives):
    """Best-effort immediate send for freshly created directives.

    Each directive is settled independently; a station that is offline or slow
    never blocks the others, and the caller returns with a pending status that
    the worker will retry.
    """
    for directive in directives:
        directive_id = directive.id
        try:
            dispatch_one(db, directive)
            db.commit()
        except Exception:
            db.rollback()
            fresh = db.get(StationDirective, directive_id)
            if fresh is not None and is_pending(fresh):
                fresh.status = 'retrying'
                fresh.last_error = '发送异常，等待重试'
                fresh.sending_at = None
                fresh.next_attempt_at = _now() + BACKOFF_BASE
                fresh.updated_at = _now()
                db.commit()


def tick(db, limit=20):
    """Advance all due directives.  One station's failure never blocks another."""
    due = list(db.scalars(select(StationDirective).where(
        StationDirective.status.in_(PENDING),
        StationDirective.next_attempt_at <= _now()).order_by(StationDirective.id).limit(limit)))
    progressed = 0
    for item in due:
        directive_id = item.id
        try:
            dispatch_one(db, item)
            db.commit()
            progressed += 1
        except Exception:
            db.rollback()
            directive = db.get(StationDirective, directive_id)
            if directive is not None and is_pending(directive):
                directive.status = 'retrying'
                directive.last_error = '发送异常，等待重试'
                directive.sending_at = None
                directive.next_attempt_at = _now() + min(BACKOFF_CAP, BACKOFF_BASE * (directive.attempts or 1))
                directive.updated_at = _now()
                db.commit()
                progressed += 1
    return progressed


def recover(db):
    """After a restart, resume unresolved directives (receipt may be in flight)."""
    stale = list(db.scalars(select(StationDirective).where(
        StationDirective.status.in_(PENDING), StationDirective.sending_at.is_not(None))))
    for directive in stale:
        directive.sending_at = None
        directive.status = 'retrying'
        directive.next_attempt_at = _now()
        directive.last_error = '进程中断后恢复，重试不重复生效'
        directive.updated_at = _now()
        record(db, 'worker', 'station.directive.recovered', {'directive': directive.id})
    if stale:
        db.commit()
    return len(stale)


def reconcile(db, station_id=None):
    """Query station truth and persist both sides' effective versions."""
    query = select(Station)
    if station_id is not None:
        query = query.where(Station.id == station_id)
    stations = list(db.scalars(query.order_by(Station.id)))
    if station_id is not None and not stations:
        raise HTTPException(404, '站点不存在')
    report = []
    for station in stations:
        replica = station_sim.ensure_replica(db, station.id)
        before_confirmed = station.confirmed_version
        station.last_reconciled_at = _now()
        station.observed_version = replica.effective_version
        station.observed_directive_id = replica.effective_directive_id
        station.observed_at = _now()
        pending = list(db.scalars(select(StationDirective).where(
            StationDirective.station_id == station.id, StationDirective.status.in_(PENDING))))
        pending_before = [{'directive': d.id, 'kind': d.kind, 'version': d.version, 'status': d.status}
                          for d in pending]
        repaired = []
        for directive in pending:
            found = station_sim.query(db, station.id, directive.id)
            ledger = found.get('receipt')
            if ledger is not None:
                # The station ledger has the original result: the directive was
                # handled even though the receipt was lost on the way back.
                receipt = dict(ledger)
                receipt['reconciled'] = True
                # Confirm against the station's current state, not the state at
                # the time of receipt, so a newer version is never rolled back.
                receipt['effective_version'] = replica.effective_version
                receipt['effective_directive_id'] = replica.effective_directive_id
                receipt['effective_payload'] = replica.effective_payload or {}
                directive.receipt = receipt
                directive.confirmed_at = _now()
                directive.updated_at = _now()
                directive.last_error = '对账查回站端原结果，补齐丢失回执'
                if directive.kind == 'publish' and ledger.get('outcome') == 'applied' \
                        and replica.effective_version == directive.version:
                    directive.status = 'effective'
                    _confirm_publish(station, directive, receipt)
                elif directive.kind == 'publish' and ledger.get('outcome') == 'applied':
                    directive.status = 'superseded'
                    directive.last_error = '回执已查回，但站端已有更新版本'
                    _observe(station, receipt)
                elif directive.kind == 'revoke' and ledger.get('outcome') == 'revoked':
                    directive.status = 'revoked'
                    _confirm_revoke(station, directive, receipt)
                else:
                    directive.status = 'superseded'
                    directive.last_error = '对账查回站端回执：该指令未生效（站端已有更新版本）'
                    _observe(station, receipt)
                repaired.append(directive.id)
            elif directive.kind == 'revoke' and replica.effective_version is not None \
                    and replica.effective_version > directive.version:
                # A newer version is live; the old revoke is moot.
                directive.status = 'superseded'
                directive.confirmed_at = _now()
                directive.updated_at = _now()
                directive.last_error = '站端已有更新版本，旧撤销不再生效'
                repaired.append(directive.id)
            elif directive.kind == 'publish' and replica.effective_version is not None \
                    and replica.effective_version > directive.version:
                directive.status = 'superseded'
                directive.confirmed_at = _now()
                directive.updated_at = _now()
                directive.last_error = '站端已有更新版本，迟到旧指令未覆盖'
                repaired.append(directive.id)
            else:
                directive.next_attempt_at = _now()
        record(db, 'worker', 'station.reconciled', {
            'station': station.id, 'observed': replica.effective_version,
            'confirmed_before': before_confirmed, 'confirmed_after': station.confirmed_version,
            'repaired': repaired, 'pending': len(pending) - len(repaired)})
        report.append({
            'station_id': station.id, 'code': station.code, 'name': station.name,
            'confirmed_version': station.confirmed_version,
            'observed_version': replica.effective_version,
            'station_effective': station_sim.state(replica),
            'confirmed_snapshot': station.confirmed_snapshot,
            'drift': station.confirmed_version != replica.effective_version,
            'pending_before': pending_before,
            'repaired': repaired,
            'pending_directives': [directive_dict(d) for d in db.scalars(select(StationDirective).where(
                StationDirective.station_id == station.id, StationDirective.status.in_(PENDING)))],
            'reconciled_at': station.last_reconciled_at})
    db.commit()
    return report


def overview(db):
    """Two-sided version board for the page."""
    rows = []
    for station in db.scalars(select(Station).order_by(Station.id)):
        replica = station_sim.ensure_replica(db, station.id)
        directives = list(db.scalars(select(StationDirective).where(
            StationDirective.station_id == station.id).order_by(StationDirective.id.desc()).limit(20)))
        latest_publish = next((d for d in directives if d.kind == 'publish'), None)
        pending = [d for d in directives if is_pending(d)]
        rows.append({
            'station': station_dict(station),
            'station_effective': station_sim.state(replica),
            'drill': {'online': replica.online, 'drop_receipt': replica.drop_receipt,
                      'hold': replica.hold, 'held': len(replica.held or [])},
            'latest_publish': directive_dict(latest_publish) if latest_publish else None,
            'pending': [directive_dict(d) for d in pending],
            'directives': [directive_dict(d) for d in directives],
            'status': _station_status(station, replica, pending),
        })
    db.commit()
    return {'stations': rows, 'generated_at': _now()}


def _station_status(station, replica, pending):
    # A revoke awaiting the station's confirmation takes precedence: the page
    # must never imply the version was withdrawn before the station said so.
    if any(d.kind == 'revoke' for d in pending):
        return 'withdrawing'
    if not station.enabled:
        return 'disabled'
    if not replica.online:
        return 'offline'
    if pending:
        return 'awaiting_receipt'
    if station.confirmed_version != replica.effective_version:
        return 'drift'
    return 'in_sync'
