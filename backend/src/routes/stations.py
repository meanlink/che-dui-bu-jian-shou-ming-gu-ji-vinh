from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import current_user, admin_only
from src.models import Station, StationDirective
from src.schemas.station import StationInput, PublishInput, PublishOneInput, RevokeInput, SimulateInput, ReleaseInput
from src.services import stations, station_sim
from src.services.audit import record

router = APIRouter(prefix='/stations')


@router.get('')
def listing(db=Depends(get_db), u=Depends(current_user)):
    return [stations.station_dict(s) for s in db.scalars(select(Station).order_by(Station.id))]


@router.post('', status_code=201)
def create(data: StationInput, db=Depends(get_db), u=Depends(admin_only)):
    s = stations.create_station(db, data)
    record(db, u.email, 'station.create', {'id': s.id, 'code': s.code})
    db.commit()
    return stations.station_dict(s)


@router.get('/board')
def board(db=Depends(get_db), u=Depends(current_user)):
    return stations.overview(db)


@router.post('/reconcile')
def reconcile_all(db=Depends(get_db), u=Depends(admin_only)):
    report = stations.reconcile(db)
    record(db, u.email, 'station.reconcile_all', {'stations': len(report)})
    db.commit()
    return report


@router.get('/{id}')
def detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    s = db.get(Station, id)
    if not s:
        raise HTTPException(404, '站点不存在')
    replica = station_sim.ensure_replica(db, id)
    directives = list(db.scalars(select(StationDirective).where(
        StationDirective.station_id == id).order_by(StationDirective.id.desc()).limit(50)))
    data = stations.station_dict(s)
    data.update({
        'station_effective': station_sim.state(replica),
        'drill': {'online': replica.online, 'drop_receipt': replica.drop_receipt,
                  'hold': replica.hold, 'held': replica.held or []},
        'directives': [stations.directive_dict(d) for d in directives],
    })
    db.commit()
    return data


@router.post('/{id}/publish', status_code=201)
def publish(id: int, data: PublishOneInput, db=Depends(get_db), u=Depends(admin_only)):
    directives = stations.publish(db, data.job_id, [id], u)
    db.commit()
    stations.send_now(db, directives)
    if directives:
        db.refresh(directives[0])
    return stations.directive_dict(directives[0])


@router.post('/publish')
def publish_many(data: PublishInput, db=Depends(get_db), u=Depends(admin_only)):
    directives = stations.publish(db, data.job_id, data.station_ids, u)
    db.commit()
    stations.send_now(db, directives)
    return [stations.directive_dict(d) for d in directives]


@router.post('/{id}/revoke', status_code=201)
def revoke(id: int, data: RevokeInput, db=Depends(get_db), u=Depends(admin_only)):
    d = stations.revoke(db, id, data.version, u)
    db.commit()
    stations.send_now(db, [d])
    db.refresh(d)
    return stations.directive_dict(d)


@router.post('/{id}/reconcile')
def reconcile_one(id: int, db=Depends(get_db), u=Depends(admin_only)):
    if not db.get(Station, id):
        raise HTTPException(404, '站点不存在')
    report = stations.reconcile(db, id)
    record(db, u.email, 'station.reconcile', {'station': id})
    db.commit()
    return report[0] if report else None


@router.post('/{id}/directives/{directive_id}/retry')
def retry(id: int, directive_id: int, db=Depends(get_db), u=Depends(admin_only)):
    d = db.get(StationDirective, directive_id)
    if not d or d.station_id != id:
        raise HTTPException(404, '指令不存在')
    if not stations.is_pending(d):
        raise HTTPException(409, '该指令已结束，无需重试')
    d.next_attempt_at = 0
    d.sending_at = None
    record(db, u.email, 'station.directive.retry', {'directive': d.id, 'version': d.version})
    db.commit()
    stations.dispatch_one(db, d)
    db.commit()
    db.refresh(d)
    return stations.directive_dict(d)


# ---- station-side drill console (the other end of the wire) ----

@router.get('/{id}/station')
def station_side(id: int, db=Depends(get_db), u=Depends(current_user)):
    if not db.get(Station, id):
        raise HTTPException(404, '站点不存在')
    replica = station_sim.ensure_replica(db, id)
    data = {'station_id': id, 'state': station_sim.state(replica),
            'drill': {'online': replica.online, 'drop_receipt': replica.drop_receipt,
                      'hold': replica.hold, 'held': replica.held or []}}
    db.commit()
    return data


@router.post('/{id}/station/drill')
def drill(id: int, data: SimulateInput, db=Depends(get_db), u=Depends(admin_only)):
    if not db.get(Station, id):
        raise HTTPException(404, '站点不存在')
    replica = station_sim.set_drill(db, id, online=data.online, drop_receipt=data.drop_receipt,
                                    hold=data.hold)
    record(db, u.email, 'station.drill', {'station': id, 'online': replica.online,
                                          'drop_receipt': replica.drop_receipt, 'hold': replica.hold})
    db.commit()
    return {'station_id': id, 'state': station_sim.state(replica),
            'drill': {'online': replica.online, 'drop_receipt': replica.drop_receipt,
                      'hold': replica.hold, 'held': replica.held or []}}


@router.post('/{id}/station/release')
def release(id: int, data: ReleaseInput, db=Depends(get_db), u=Depends(admin_only)):
    if not db.get(Station, id):
        raise HTTPException(404, '站点不存在')
    result = station_sim.release(db, id, data.order, data.directive_ids)
    record(db, u.email, 'station.release', {'station': id, 'order': data.order,
                                           'released': result['released']})
    db.commit()
    return result


@router.get('/{id}/station/directives/{directive_id}')
def station_receipt(id: int, directive_id: int, db=Depends(get_db), u=Depends(current_user)):
    """Query back the original station result for one directive (no re-apply)."""
    if not db.get(Station, id):
        raise HTTPException(404, '站点不存在')
    result = station_sim.query(db, id, directive_id)
    db.commit()
    return result


@router.get('/{id}/directives')
def directives(id: int, db=Depends(get_db), u=Depends(current_user)):
    if not db.get(Station, id):
        raise HTTPException(404, '站点不存在')
    return [stations.directive_dict(d) for d in db.scalars(select(StationDirective).where(
        StationDirective.station_id == id).order_by(StationDirective.id.desc()).limit(100))]
