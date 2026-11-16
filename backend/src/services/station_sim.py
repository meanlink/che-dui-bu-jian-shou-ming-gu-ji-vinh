"""Station-side simulator: the "other end" of the wire, used for drills.

It is deliberately small and idempotent:

* every directive carries an immutable snapshot (model / mileage / version)
  plus a unique idempotency key;
* the station keeps a durable per-directive receipt ledger, so a retried
  directive is answered from the ledger and never takes effect twice;
* a receipt can be dropped on the way back (drill switch) while the ledger
  still holds the original result, which the backend can query back;
* directives can be parked and released in reverse order (out-of-order drill);
* a late publish for an older version never overwrites a newer effective
  version, and a late revoke for an older version never withdraws a newer one.
"""
import time
import uuid
from sqlalchemy import select
from src.models.station import StationReplica, StationReceipt


def ensure_replica(db, station_id):
    replica = db.get(StationReplica, station_id)
    if replica is None:
        replica = StationReplica(
            station_id=station_id, effective_version=None, effective_directive_id=None,
            effective_payload={}, revoked_floor=0, state_epoch=0, online=True,
            drop_receipt=False, hold=False, held=[], updated_at=time.time())
        db.add(replica)
        db.flush()
    return replica


def state(replica):
    return {
        'effective_version': replica.effective_version,
        'effective_directive_id': replica.effective_directive_id,
        'effective_payload': replica.effective_payload or {},
        'revoked_floor': replica.revoked_floor,
        'state_epoch': replica.state_epoch,
        'online': replica.online,
    }


def ledger_receipt(db, station_id, directive_id):
    return db.scalar(select(StationReceipt).where(
        StationReceipt.station_id == station_id,
        StationReceipt.directive_id == directive_id))


def query(db, station_id, directive_id):
    """Read-only: look up the original result of a directive without re-applying it."""
    replica = ensure_replica(db, station_id)
    receipt = ledger_receipt(db, station_id, directive_id)
    return {
        'online': replica.online,
        'state': state(replica),
        'receipt': _receipt_payload(receipt, replica=replica) if receipt is not None else None,
    }


def apply(db, station_id, directive):
    """Deliver one directive to the station.  Returns an ACK payload or a drill marker."""
    replica = ensure_replica(db, station_id)
    if not replica.online:
        return {'accepted': False, 'offline': True, 'reason': 'station offline'}

    existing = ledger_receipt(db, station_id, directive['id'])
    if existing is not None:
        # Already handled before; the receipt may have been lost on the way back.
        # Answer from the ledger, never apply a second effect.  The ACK reports
        # the station's *current* state, so a later version is never rolled back.
        if replica.drop_receipt:
            # The drill keeps dropping the ACK until the switch is turned off;
            # the one-time effect is already in the ledger and is not repeated.
            return {'accepted': True, 'receipt_lost': True,
                    'reason': 'receipt dropped by drill (ledger already holds the result)',
                    'state': state(replica)}
        return _receipt_payload(existing, replayed=True, replica=replica)

    if replica.hold:
        held = list(replica.held or [])
        if not any(item.get('id') == directive['id'] for item in held):
            held.append(directive)
            replica.held = held
            replica.updated_at = time.time()
        return {'accepted': False, 'held': True, 'reason': 'held by drill for out-of-order release'}

    receipt = _apply_effect(db, replica, directive)
    if replica.drop_receipt:
        # Drill: the station did take effect, but the receipt never reached the
        # backend.  The ledger still has the original result for a query-back.
        return {'accepted': True, 'receipt_lost': True, 'reason': 'receipt dropped by drill',
                'state': state(replica)}
    return _receipt_payload(receipt)


def _apply_effect(db, replica, directive):
    if directive['kind'] == 'publish':
        version = directive['version']
        if replica.revoked_floor >= version:
            return _write_receipt(db, replica, directive, 'superseded', duplicate=False,
                                  reason=f'version {version} was already revoked at station')
        if replica.effective_version is not None and replica.effective_version >= version:
            return _write_receipt(db, replica, directive, 'superseded', duplicate=False,
                                  reason=f'station already at version {replica.effective_version}')
        replica.effective_version = version
        replica.effective_directive_id = directive['id']
        replica.effective_payload = directive.get('snapshot') or {}
        replica.state_epoch += 1
        replica.updated_at = time.time()
        return _write_receipt(db, replica, directive, 'applied')

    if directive['kind'] == 'revoke':
        target = directive['version']
        current = replica.effective_version
        replica.revoked_floor = max(replica.revoked_floor, target)
        if current is not None and current <= target:
            replica.effective_version = None
            replica.effective_directive_id = None
            replica.effective_payload = {}
            replica.state_epoch += 1
            replica.updated_at = time.time()
            return _write_receipt(db, replica, directive, 'revoked')
        # Nothing at or below the target is live.  A late revoke for an older
        # version must not withdraw a newer effective version.
        return _write_receipt(db, replica, directive, 'noop', duplicate=False,
                              reason='no matching effective version to withdraw')
    raise ValueError(f"unknown directive kind {directive['kind']}")


def release(db, station_id, order='fifo', directive_ids=None):
    """Release parked directives (drill) so they can arrive out of order."""
    replica = ensure_replica(db, station_id)
    held = list(replica.held or [])
    if directive_ids:
        wanted = set(directive_ids)
        chosen = [item for item in held if item.get('id') in wanted]
        remaining = [item for item in held if item.get('id') not in wanted]
    else:
        chosen = list(held)
        remaining = []
    chosen.sort(key=lambda item: item.get('id', 0), reverse=(order == 'lifo'))
    replica.held = remaining
    results = []
    for directive in chosen:
        existing = ledger_receipt(db, station_id, directive['id'])
        if existing is not None:
            results.append(_receipt_payload(existing, replayed=True, replica=replica))
            continue
        receipt = _apply_effect(db, replica, directive)
        results.append(_receipt_payload(receipt, replica=replica))
    replica.updated_at = time.time()
    return {'released': len(chosen), 'order': order, 'results': results, 'state': state(replica)}


def set_drill(db, station_id, *, online=None, offline=None, drop_receipt=None, hold=None):
    replica = ensure_replica(db, station_id)
    if online is not None:
        replica.online = online
    if offline is not None:
        replica.online = not offline
    if drop_receipt is not None:
        replica.drop_receipt = drop_receipt
    if hold is not None:
        replica.hold = hold
    replica.updated_at = time.time()
    return replica


def _write_receipt(db, replica, directive, outcome, duplicate=False, reason=''):
    receipt = StationReceipt(
        station_id=replica.station_id, directive_id=directive['id'], kind=directive['kind'],
        version=directive['version'], outcome=outcome, receipt_id=uuid.uuid4().hex[:16],
        effective_version_after=replica.effective_version,
        effective_directive_id_after=replica.effective_directive_id,
        effective_payload_after=replica.effective_payload or {},
        revoked_floor_after=replica.revoked_floor,
        state_epoch_after=replica.state_epoch,
        duplicate=duplicate, detail={'reason': reason} if reason else {},
        received_at=time.time())
    db.add(receipt)
    db.flush()
    return receipt


def _receipt_payload(receipt, replayed=False, replica=None):
    """Receipt as the station would send it.

    ``outcome`` and ``applied_*`` describe what this directive did at the time
    it was received (from the durable ledger).  ``effective_*`` describe the
    station's *current* state.  On a replay after a lost receipt the two can
    differ: the directive's one-time effect stands, but a newer version may
    have taken over since.  A retried older directive therefore confirms its
    own delivery without ever moving the station back.
    """
    current_version = receipt.effective_version_after
    current_directive = receipt.effective_directive_id_after
    current_payload = receipt.effective_payload_after or {}
    current_floor = receipt.revoked_floor_after
    current_epoch = receipt.state_epoch_after
    if replica is not None:
        current_version = replica.effective_version
        current_directive = replica.effective_directive_id
        current_payload = replica.effective_payload or {}
        current_floor = replica.revoked_floor
        current_epoch = replica.state_epoch
    return {
        'receipt_id': receipt.receipt_id, 'directive_id': receipt.directive_id,
        'kind': receipt.kind, 'version': receipt.version, 'outcome': receipt.outcome,
        'applied_effective_version': receipt.effective_version_after,
        'applied_effective_directive_id': receipt.effective_directive_id_after,
        'effective_version': current_version,
        'effective_directive_id': current_directive,
        'effective_payload': current_payload or {},
        'revoked_floor': current_floor, 'state_epoch': current_epoch,
        'duplicate': bool(receipt.duplicate or replayed), 'detail': receipt.detail or {},
        'received_at': receipt.received_at,
    }
