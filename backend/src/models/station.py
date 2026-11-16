from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class Station(Base):
    """A maintenance depot that receives approved mileage estimates."""
    __tablename__ = 'stations'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Next publish version to allocate for this station.
    last_version: Mapped[int] = mapped_column(Integer, default=0)
    # Backend belief, only advanced by station acknowledgements (receipts).
    confirmed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_directive_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    # Last version actually seen when querying the station.
    observed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_directive_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_reconciled_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class StationDirective(Base):
    """An immutable per-station instruction (publish or revoke) with its own version."""
    __tablename__ = 'station_directives'
    # One directive per (station, kind, version): a second concurrent publish
    # that tried to reuse a version fails instead of silently overwriting.
    __table_args__ = (UniqueConstraint('station_id', 'kind', 'version', name='uq_station_directive_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey('stations.id'))
    kind: Mapped[str] = mapped_column(String(20))          # publish | revoke
    version: Mapped[int] = mapped_column(Integer)           # published version, or target version for revoke
    status: Mapped[str] = mapped_column(String(30), default='pending')
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Double, default=0)
    last_error: Mapped[str] = mapped_column(String(500), default='')
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    sending_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    confirmed_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class StationReplica(Base):
    """Station-side state used by the embedded simulator (the other side of the wire)."""
    __tablename__ = 'station_replicas'
    station_id: Mapped[int] = mapped_column(ForeignKey('stations.id'), primary_key=True)
    effective_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_directive_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    revoked_floor: Mapped[int] = mapped_column(Integer, default=0)
    state_epoch: Mapped[int] = mapped_column(Integer, default=0)
    # Drill switches.
    online: Mapped[bool] = mapped_column(Boolean, default=True)
    drop_receipt: Mapped[bool] = mapped_column(Boolean, default=False)
    hold: Mapped[bool] = mapped_column(Boolean, default=False)
    held: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[float] = mapped_column(Double)


class StationReceipt(Base):
    """Station-side idempotency ledger: one receipt per received directive."""
    __tablename__ = 'station_receipts'
    __table_args__ = (UniqueConstraint('station_id', 'directive_id', name='uq_station_directive_receipt'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey('stations.id'))
    directive_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(30))
    receipt_id: Mapped[str] = mapped_column(String(40), unique=True)
    effective_version_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_directive_id_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_payload_after: Mapped[dict] = mapped_column(JSON, default=dict)
    revoked_floor_after: Mapped[int] = mapped_column(Integer, default=0)
    state_epoch_after: Mapped[int] = mapped_column(Integer, default=0)
    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[float] = mapped_column(Double)
