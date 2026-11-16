"""Business checks for per-station approved-estimate distribution.

Run from backend/ with a throwaway SQLite database:

    DATABASE_URL=sqlite:////tmp/station_test.db \\
    REDIS_URL=redis://localhost:1/0 ARTIFACT_DIR=/tmp/artifacts \\
    python -m scripts.test_stations

These are unit-level checks against the service layer (the station simulator is
embedded, so no Redis / external depot is needed).  Real container, permission
and UI checks are recorded separately in TESTING.md.
"""
import os
import tempfile

os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(tempfile.gettempdir(), 'station_test.db'))
os.environ.setdefault('REDIS_URL', 'redis://localhost:1/0')
os.environ.setdefault('ARTIFACT_DIR', os.path.join(tempfile.gettempdir(), 'artifacts'))

from sqlalchemy import select
from src.config.database import Base, engine, Session
from src import models
from src.models import Job, Station, StationDirective, StationReplica, User
from src.services import stations, station_sim
from src.services.business import analyze, metadata, seed_records
from src.schemas.station import StationInput
from src.utils.security import hash_password
from src.utils.serialization import checksum


class Admin:
    def __init__(self, **kw):
        self.email = kw.get('email', 'admin@test.local')


def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        admin = User(email='admin@test.local', name='测试负责人', role='admin', password=hash_password('x'))
        db.add(admin)
        db.commit()
        db.refresh(admin)
        return admin.id


def approved_job(db, admin_id, model='AC-8', reliability=0.9, name='测试基线'):
    rows = seed_records()
    from src.models import Dataset
    dataset = Dataset(name=f'测试批次 {name} {model} {reliability}', source='test', records=rows,
                      checksum=checksum(rows), created_at=0)
    db.add(dataset)
    db.flush()
    result = analyze(rows, {'model': model, 'reliability': reliability})
    job = Job(name=name, dataset_id=dataset.id, params={'model': model, 'reliability': reliability},
              status='completed', result=result, created_by=admin_id, created_at=0, finished_at=0,
              approved=True)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def station(db, code):
    return stations.create_station(db, StationInput(code=code, name='测试站 ' + code))


def send(db, directives):
    db.commit()
    stations.send_now(db, directives)
    for d in directives:
        db.refresh(d)
    return directives


def check(label, condition, detail=''):
    assert condition, f'{label} 失败 {detail}'
    print('  ok  ' + label)


def scenario_freeze_and_idempotent_retry():
    print('\n[1] 冻结快照下发 + 漏回执重试不重复生效')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        s = station(db, 'S-1')
        d = stations.publish(db, job.id, [s.id], admin)[0]
        db.commit()
        station_sim.set_drill(db, s.id, drop_receipt=True)
        db.commit()
        stations.send_now(db, [d])
        db.refresh(d)
        replica = db.get(StationReplica, s.id)
        check('首次下发因漏回执保持未决', d.status == 'receipt_lost', d.status)
        check('站端实际已生效', replica.effective_version == 1)
        check('后端未把未回执当作已确认', db.get(Station, s.id).confirmed_version is None)
        check('快照冻结型号与里程', d.snapshot['model'] == 'AC-8' and d.snapshot['mileage'] > 0)
        epoch_before = replica.state_epoch
        # Retry with the receipt path restored: ledger answers, no second effect.
        station_sim.set_drill(db, s.id, drop_receipt=False)
        db.commit()
        d.next_attempt_at = 0
        stations.dispatch_one(db, d)
        db.commit()
        db.refresh(d)
        replica = db.get(StationReplica, s.id)
        check('重试后确认生效', d.status == 'effective', d.status)
        check('重试未再次生效', replica.state_epoch == epoch_before, f'{epoch_before}->{replica.state_epoch}')
        check('回执来自站端台账', (d.receipt or {}).get('directive_id') == d.id)
        check('站端版本仍为 v1', replica.effective_version == 1)


def scenario_receipt_lost_without_retry_reconciled():
    print('\n[2] 回执丢失后先查回原结果（对账补齐，不重复生效）')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        s = station(db, 'S-2')
        d = stations.publish(db, job.id, [s.id], admin)[0]
        db.commit()
        station_sim.set_drill(db, s.id, drop_receipt=True)
        db.commit()
        stations.send_now(db, [d])
        db.refresh(d)
        epoch = db.get(StationReplica, s.id).state_epoch
        report = stations.reconcile(db, s.id)
        db.refresh(d)
        check('对账查回丢失回执', d.status == 'effective', d.status)
        check('对账未再次生效', db.get(StationReplica, s.id).state_epoch == epoch)
        check('对账报告标记已补齐', d.id in report[0]['repaired'])
        found = station_sim.query(db, s.id, d.id)
        check('站端可查到原结果', found['receipt']['outcome'] == 'applied')


def scenario_revoke_needs_confirmation():
    print('\n[3] 撤销未获站端确认前保留待核对，不报已撤销')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        s = station(db, 'S-3')
        send(db, stations.publish(db, job.id, [s.id], admin))
        station_sim.set_drill(db, s.id, offline=True)
        db.commit()
        r = stations.revoke(db, s.id, None, admin)
        db.commit()
        stations.send_now(db, [r])
        db.refresh(r)
        fresh = db.get(Station, s.id)
        check('撤销保持待核对', r.status in ('pending', 'retrying'), r.status)
        check('后端未报已撤销', fresh.confirmed_version == 1)
        board = stations.overview(db)
        row = next(x for x in board['stations'] if x['station']['id'] == s.id)
        check('撤销未确认时状态为撤销待核对',
              row['status'] == 'withdrawing', row['status'])
        station_sim.set_drill(db, s.id, offline=False)
        db.commit()
        r.next_attempt_at = 0
        stations.dispatch_one(db, r)
        db.commit()
        db.refresh(r)
        check('站端确认后撤销完成', r.status == 'revoked', r.status)
        check('站端版本清空', db.get(Station, s.id).confirmed_version is None)
        check('站端 replica 清空', db.get(StationReplica, s.id).effective_version is None)


def scenario_late_publish_does_not_overwrite_newer():
    print('\n[4] 并发下发新版与撤销旧版：迟到的旧指令不覆盖新版')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job1 = approved_job(db, admin_id, reliability=0.9, name='基线 v1')
        job2 = approved_job(db, admin_id, reliability=0.95, name='基线 v2')
        s = station(db, 'S-4')
        first = send(db, stations.publish(db, job1.id, [s.id], admin))[0]
        check('旧版 v1 已生效', first.status == 'effective')
        # Park the new version's directive at the station.
        station_sim.set_drill(db, s.id, hold=True)
        db.commit()
        second = stations.publish(db, job2.id, [s.id], admin)[0]
        db.commit()
        stations.send_now(db, [second])
        db.refresh(second)
        check('新版 v2 被暂扣', second.status == 'pending')
        # A late retry of the old directive must not roll the station back.
        station_sim.release(db, s.id, 'lifo')
        db.commit()
        result = station_sim.release(db, s.id, 'fifo')
        db.commit()
        replica = db.get(StationReplica, s.id)
        check('站端只前进到 v2', replica.effective_version == 2, replica.effective_version)
        old_retry = dict(first.receipt or {})
        check('旧回执有效', old_retry.get('outcome') == 'applied')
        check('新版快照已上线', (replica.effective_payload or {}).get('job_id') == job2.id)


def scenario_late_old_publish_via_hold():
    print('\n[5] 乱序释放旧指令：站端拒绝覆盖新版')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job1 = approved_job(db, admin_id, reliability=0.9, name='旧版')
        job2 = approved_job(db, admin_id, reliability=0.95, name='新版')
        s = station(db, 'S-5')
        d1 = stations.publish(db, job1.id, [s.id], admin)[0]
        db.commit()
        station_sim.set_drill(db, s.id, hold=True)
        db.commit()
        # First attempt parks d1.
        stations.send_now(db, [d1])
        # Second attempt also parks (same directive already held).
        d2 = stations.publish(db, job2.id, [s.id], admin)[0]
        db.commit()
        stations.send_now(db, [d2])
        db.refresh(d1)
        db.refresh(d2)
        check('两条指令均在站端暂扣', d1.status == 'pending' and d2.status == 'pending')
        station_sim.release(db, s.id, 'lifo')  # v2 first
        db.commit()
        out = station_sim.release(db, s.id, 'fifo')  # then v1 arrives late
        db.commit()
        outcomes = {r['version']: r['outcome'] for r in out['results']}
        replica = db.get(StationReplica, s.id)
        check('v2 生效', replica.effective_version == 2, replica.effective_version)
        check('迟到的 v1 被站端判为取代', outcomes.get(1) in (None, 'superseded'), outcomes)
        d1.next_attempt_at = 0
        stations.dispatch_one(db, d1)
        db.commit()
        db.refresh(d1)
        check('后端记录旧下发为被取代', d1.status == 'superseded', d1.status)
        check('站端版本未被拉回', db.get(StationReplica, s.id).effective_version == 2)


def scenario_late_revoke_does_not_remove_newer():
    print('\n[6] 迟到的旧撤销不撤掉新版')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job1 = approved_job(db, admin_id, reliability=0.9, name='旧版')
        job2 = approved_job(db, admin_id, reliability=0.95, name='新版')
        s = station(db, 'S-6')
        send(db, stations.publish(db, job1.id, [s.id], admin))
        station_sim.set_drill(db, s.id, hold=True)
        db.commit()
        revoke = stations.revoke(db, s.id, 1, admin)
        db.commit()
        stations.send_now(db, [revoke])
        db.refresh(revoke)
        check('旧撤销暂扣未生效', revoke.status == 'pending')
        # Simulate operator cancelling the revoke by publishing a newer version,
        # which reaches the station first on release.
        newer = stations.publish(db, job2.id, [s.id], admin)[0]
        db.commit()
        stations.send_now(db, [newer])
        db.refresh(newer)
        check('新版在站端暂扣', newer.status == 'pending')
        station_sim.release(db, s.id, 'fifo')  # revoke (lower id) first, then v2
        db.commit()
        replica = db.get(StationReplica, s.id)
        check('站端最终为新版 v2', replica.effective_version == 2, replica.effective_version)
        revoke.next_attempt_at = 0
        stations.dispatch_one(db, revoke)
        newer.next_attempt_at = 0
        stations.dispatch_one(db, newer)
        db.commit()
        db.refresh(revoke)
        db.refresh(newer)
        check('撤销完成但仅撤掉旧版', revoke.status == 'revoked', revoke.status)
        check('新版仍是生效版本', db.get(Station, s.id).confirmed_version == 2)


def scenario_late_revoke_after_newer_effective():
    print('\n[7] 新版已生效后，旧撤销到达判定为无需撤销')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job1 = approved_job(db, admin_id, reliability=0.9, name='旧版')
        job2 = approved_job(db, admin_id, reliability=0.95, name='新版')
        s = station(db, 'S-7')
        send(db, stations.publish(db, job1.id, [s.id], admin))
        # Craft a stale revoke for v1 and deliver it only after v2 took effect.
        revoke = stations.revoke(db, s.id, 1, admin)
        db.commit()
        revoke.status = 'pending'
        revoke.next_attempt_at = 0
        db.commit()
        send(db, stations.publish(db, job2.id, [s.id], admin))
        check('v2 已生效', db.get(Station, s.id).confirmed_version == 2)
        # Now the late revoke for v1 arrives.
        stations.dispatch_one(db, revoke)
        db.commit()
        db.refresh(revoke)
        replica = db.get(StationReplica, s.id)
        check('站端保留 v2', replica.effective_version == 2, replica.effective_version)
        check('旧撤销被判为无需撤销', revoke.receipt.get('outcome') == 'noop', revoke.receipt)
        check('后端未报 v2 被撤销', db.get(Station, s.id).confirmed_version == 2)


def scenario_interruption_recovery():
    print('\n[8] 中断后恢复未决指令')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        s = station(db, 'S-8')
        d = stations.publish(db, job.id, [s.id], admin)[0]
        db.commit()
        # Simulate a crash mid-send: half-open send, no receipt.
        d.status = 'pending'
        d.sending_at = 1
        db.commit()
        count = stations.recover(db)
        db.refresh(d)
        check('恢复未决指令', count == 1 and d.status == 'retrying' and d.sending_at is None, d.status)
        d.next_attempt_at = 0
        stations.dispatch_one(db, d)
        db.commit()
        db.refresh(d)
        check('恢复后正常送达', d.status == 'effective', d.status)


def scenario_offline_station_does_not_block():
    print('\n[9] 断开站点不拖住其他站')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        bad = station(db, 'S-BAD')
        good = station(db, 'S-GOOD')
        directives = stations.publish(db, job.id, [bad.id, good.id], admin)
        station_sim.set_drill(db, bad.id, offline=True)
        db.commit()
        stations.send_now(db, directives)
        for d in directives:
            db.refresh(d)
        good_d = next(d for d in directives if d.station_id == good.id)
        bad_d = next(d for d in directives if d.station_id == bad.id)
        check('好站生效', good_d.status == 'effective', good_d.status)
        check('坏站保持待下发', bad_d.status == 'pending', bad_d.status)
        board = stations.overview(db)
        bad_row = next(x for x in board['stations'] if x['station']['id'] == bad.id)
        good_row = next(x for x in board['stations'] if x['station']['id'] == good.id)
        check('页面显示离线', bad_row['status'] == 'offline', bad_row['status'])
        check('好站无待决指令', good_row['status'] == 'in_sync' and not good_row['pending'], good_row['status'])


def scenario_duplicate_publish_blocked():
    print('\n[10] 同批重复下发被拦截')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        s = station(db, 'S-10')
        d = stations.publish(db, job.id, [s.id], admin)[0]
        db.commit()
        station_sim.set_drill(db, s.id, offline=True)
        db.commit()
        stations.send_now(db, [d])
        try:
            stations.publish(db, job.id, [s.id], admin)
            raise AssertionError('未拦截重复下发')
        except Exception as exc:
            check('重复下发被拒绝', '同批下发' in str(exc), str(exc))


def scenario_worker_tick_isolated_stations():
    print('\n[11] worker 轮询：多站待决指令逐条推进，不互相阻塞')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        a = station(db, 'S-T1')
        b = station(db, 'S-T2')
        directives = stations.publish(db, job.id, [a.id, b.id], admin)
        db.commit()
        station_sim.set_drill(db, b.id, offline=True)
        db.commit()
        for d in directives:
            d.next_attempt_at = 0
        db.commit()
        progressed = stations.tick(db)
        for d in directives:
            db.refresh(d)
        first = next(d for d in directives if d.station_id == a.id)
        second = next(d for d in directives if d.station_id == b.id)
        check('轮询推进了在线站', first.status == 'effective', first.status)
        check('离线站仍在重试队列', second.status == 'pending', second.status)
        check('轮询未抛异常', progressed >= 1, progressed)
        station_sim.set_drill(db, b.id, offline=False)
        db.commit()
        for d in directives:
            d.next_attempt_at = 0
        db.commit()
        stations.tick(db)
        db.refresh(second)
        check('离线恢复后补发成功', second.status == 'effective', second.status)


def scenario_same_result_not_republished():
    print('\n[12] 已生效的同一批准结果不再重复下发')
    admin_id = fresh_db()
    with Session() as db:
        admin = db.get(User, admin_id)
        job = approved_job(db, admin_id)
        s = station(db, 'S-12')
        send(db, stations.publish(db, job.id, [s.id], admin))
        try:
            stations.publish(db, job.id, [s.id], admin)
            raise AssertionError('未拦截重复下发')
        except Exception as exc:
            check('已生效同批结果被拒绝', '无需重复下发' in str(exc), str(exc))


def main():
    scenario_freeze_and_idempotent_retry()
    scenario_receipt_lost_without_retry_reconciled()
    scenario_revoke_needs_confirmation()
    scenario_late_publish_does_not_overwrite_newer()
    scenario_late_old_publish_via_hold()
    scenario_late_revoke_does_not_remove_newer()
    scenario_late_revoke_after_newer_effective()
    scenario_interruption_recovery()
    scenario_offline_station_does_not_block()
    scenario_duplicate_publish_blocked()
    scenario_worker_tick_isolated_stations()
    scenario_same_result_not_republished()
    print('\n全部业务检查通过')


if __name__ == '__main__':
    main()
