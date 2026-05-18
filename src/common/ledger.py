"""SQLite ledger: single source of truth for all artifacts.

All writes are append-only for audit trail. JSON artifact files in data/ are
written alongside the DB so every step can be replayed from disk alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.common.schemas import (
    AuditEntry,
    Decision,
    Fill,
    Order,
    Recommendation,
    SystemState,
)

DATA_DIR = Path(__file__).parents[2] / "data"
DB_PATH = DATA_DIR / "ledger.db"
AUDIT_LOG = DATA_DIR / "audit.log"


# ---------------------------------------------------------------------------
# SQLAlchemy ORM models
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class RecommendationRow(Base):
    __tablename__ = "recommendations"
    rec_id = Column(String, primary_key=True)
    created_at = Column(DateTime)
    market_ticker = Column(String, index=True)
    side = Column(String)
    limit_price_cents = Column(Integer)
    target_size_contracts = Column(Integer)
    edge_bps = Column(Integer)
    confidence = Column(String)
    json_blob = Column(Text)  # full Recommendation JSON


class DecisionRow(Base):
    __tablename__ = "decisions"
    rec_id = Column(String, primary_key=True)
    decided_at = Column(DateTime)
    decision = Column(String, index=True)
    reason_codes = Column(Text)
    json_blob = Column(Text)


class OrderRow(Base):
    __tablename__ = "orders"
    order_id = Column(String, primary_key=True)
    client_order_id = Column(String, index=True)
    rec_id = Column(String, index=True)
    market_ticker = Column(String, index=True)
    status = Column(String, index=True)
    submitted_at = Column(DateTime, nullable=True)
    last_updated_at = Column(DateTime)
    json_blob = Column(Text)


class FillRow(Base):
    __tablename__ = "fills"
    fill_id = Column(String, primary_key=True)
    order_id = Column(String, index=True)
    rec_id = Column(String, index=True)
    market_ticker = Column(String, index=True)
    price_cents = Column(Integer)
    count = Column(Integer)
    filled_at = Column(DateTime)
    json_blob = Column(Text)


class AuditRow(Base):
    __tablename__ = "audit"
    seq = Column(Integer, primary_key=True, autoincrement=True)
    event_ts = Column(DateTime)
    event_type = Column(String, index=True)
    actor = Column(String)
    payload_json = Column(Text)
    prev_hash = Column(String, nullable=True)
    entry_hash = Column(String)


class SystemStateRow(Base):
    __tablename__ = "system_state"
    id = Column(Integer, primary_key=True, default=1)
    mode = Column(String)
    halted = Column(Integer)  # 0/1 — SQLite has no bool
    halt_reason = Column(String, nullable=True)
    updated_at = Column(DateTime)


# ---------------------------------------------------------------------------
# Ledger class
# ---------------------------------------------------------------------------


class Ledger:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        # Enable WAL for concurrent reads
        @event.listens_for(self._engine, "connect")
        def _set_wal(dbapi_conn, _conn_rec):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine)
        self._ensure_system_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        return self._Session()

    def _ensure_system_state(self) -> None:
        with self._session() as s:
            if not s.get(SystemStateRow, 1):
                s.add(SystemStateRow(
                    id=1, mode="paper", halted=0,
                    halt_reason=None, updated_at=datetime.utcnow(),
                ))
                s.commit()

    @staticmethod
    def _hash_entry(payload_json: str, prev_hash: str | None) -> str:
        data = (prev_hash or "") + payload_json
        return hashlib.sha256(data.encode()).hexdigest()

    def _last_audit_hash(self, s: Session) -> str | None:
        row = s.execute(
            text("SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1")
        ).fetchone()
        return row[0] if row else None

    def _write_artifact(self, subdir: str, filename: str, obj: dict) -> None:
        path = DATA_DIR / subdir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, default=str))

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------

    def save_recommendation(self, rec: Recommendation) -> None:
        blob = rec.model_dump_json()
        with self._session() as s:
            s.merge(RecommendationRow(
                rec_id=rec.rec_id,
                created_at=rec.created_at,
                market_ticker=rec.market_ticker,
                side=rec.side.value,
                limit_price_cents=rec.limit_price_cents,
                target_size_contracts=rec.target_size_contracts,
                edge_bps=rec.edge_bps,
                confidence=rec.confidence.value,
                json_blob=blob,
            ))
            s.commit()
        self._write_artifact("recommendations", f"{rec.created_at.strftime('%Y%m%dT%H%M%SZ')}_{rec.market_ticker}.json", rec.model_dump(mode="json"))
        self.append_audit("RECOMMENDATION", "research_agent", {"rec_id": rec.rec_id, "market_ticker": rec.market_ticker})

    def get_recommendation(self, rec_id: str) -> Recommendation | None:
        with self._session() as s:
            row = s.get(RecommendationRow, rec_id)
            return Recommendation.model_validate_json(row.json_blob) if row else None

    def get_recently_analyzed_tickers(self, hours: float = 6.0) -> set[str]:
        """Return market tickers that already have a recommendation within the last N hours.

        Used by the orchestrator to skip re-researching markets Claude already
        evaluated this cycle — avoids burning credits on the same conclusion.
        """
        from datetime import timedelta
        from sqlalchemy import text as sql_text
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with self._session() as s:
            rows = s.execute(
                sql_text(
                    "SELECT DISTINCT market_ticker FROM recommendations "
                    "WHERE created_at >= :cutoff"
                ),
                {"cutoff": cutoff},
            ).fetchall()
        return {row[0] for row in rows}

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def save_decision(self, decision: Decision) -> None:
        blob = decision.model_dump_json()
        with self._session() as s:
            s.merge(DecisionRow(
                rec_id=decision.rec_id,
                decided_at=decision.decided_at,
                decision=decision.decision.value,
                reason_codes=json.dumps(decision.reason_codes),
                json_blob=blob,
            ))
            s.commit()
        self._write_artifact("decisions", f"{decision.rec_id}.json", decision.model_dump(mode="json"))
        self.append_audit("DECISION", "qa_agent", {"rec_id": decision.rec_id, "verdict": decision.decision.value})

    def get_decision(self, rec_id: str) -> Decision | None:
        with self._session() as s:
            row = s.get(DecisionRow, rec_id)
            return Decision.model_validate_json(row.json_blob) if row else None

    def get_pending_approvals(self) -> list[Decision]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM decisions WHERE decision = 'APPROVE' AND rec_id NOT IN (SELECT rec_id FROM orders)")
            ).fetchall()
            return [Decision.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def save_order(self, order: Order) -> None:
        blob = order.model_dump_json()
        with self._session() as s:
            s.merge(OrderRow(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                rec_id=order.rec_id,
                market_ticker=order.market_ticker,
                status=order.status.value,
                submitted_at=order.submitted_at,
                last_updated_at=order.last_updated_at,
                json_blob=blob,
            ))
            s.commit()
        self._write_artifact("orders", f"{order.order_id}.json", order.model_dump(mode="json"))

    def get_order(self, order_id: str) -> Order | None:
        with self._session() as s:
            row = s.get(OrderRow, order_id)
            return Order.model_validate_json(row.json_blob) if row else None

    def get_orders_by_status(self, *statuses: str) -> list[Order]:
        placeholders = ",".join(f"'{s}'" for s in statuses)
        with self._session() as s:
            rows = s.execute(
                text(f"SELECT json_blob FROM orders WHERE status IN ({placeholders})")
            ).fetchall()
            return [Order.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def save_fill(self, fill: Fill) -> None:
        blob = fill.model_dump_json()
        with self._session() as s:
            s.merge(FillRow(
                fill_id=fill.fill_id,
                order_id=fill.order_id,
                rec_id=fill.rec_id,
                market_ticker=fill.market_ticker,
                price_cents=fill.price_cents,
                count=fill.count,
                filled_at=fill.filled_at,
                json_blob=blob,
            ))
            s.commit()
        self._write_artifact("fills", f"{fill.fill_id}.json", fill.model_dump(mode="json"))
        self.append_audit("FILL", "execute_agent", {"fill_id": fill.fill_id, "order_id": fill.order_id})

    def get_fills_for_order(self, order_id: str) -> list[Fill]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM fills WHERE order_id = :oid"),
                {"oid": order_id},
            ).fetchall()
            return [Fill.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Audit log (append-only, hash-chained)
    # ------------------------------------------------------------------

    def append_audit(self, event_type: str, actor: str, payload: dict) -> None:
        payload_json = json.dumps(payload, default=str)
        with self._session() as s:
            prev_hash = self._last_audit_hash(s)
            entry_hash = self._hash_entry(payload_json, prev_hash)
            row = AuditRow(
                event_ts=datetime.utcnow(),
                event_type=event_type,
                actor=actor,
                payload_json=payload_json,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            entry = AuditEntry(
                seq=row.seq,
                event_ts=row.event_ts,
                event_type=event_type,
                actor=actor,
                payload=payload,
                prev_hash=prev_hash,
            )
        # Also write to append-only flat file
        with open(AUDIT_LOG, "a") as f:
            f.write(entry.model_dump_json() + "\n")

    # ------------------------------------------------------------------
    # System state
    # ------------------------------------------------------------------

    def get_system_state(self) -> SystemState:
        with self._session() as s:
            row = s.get(SystemStateRow, 1)
            return SystemState(
                mode=row.mode,
                halted=bool(row.halted),
                halt_reason=row.halt_reason,
                updated_at=row.updated_at,
            )

    def set_system_state(self, state: SystemState) -> None:
        with self._session() as s:
            row = s.get(SystemStateRow, 1)
            row.mode = state.mode.value
            row.halted = int(state.halted)
            row.halt_reason = state.halt_reason
            row.updated_at = datetime.utcnow()
            s.commit()
        self.append_audit("SYSTEM_STATE_CHANGE", "orchestrator", state.model_dump(mode="json"))

    def is_halted(self) -> bool:
        halt_file = DATA_DIR / "HALT"
        if halt_file.exists():
            return True
        return self.get_system_state().halted

    def halt(self, reason: str, actor: str = "system") -> None:
        state = self.get_system_state()
        state.halted = True
        state.halt_reason = reason
        self.set_system_state(state)
        # Also touch the HALT file for out-of-process checks
        (DATA_DIR / "HALT").touch()
        self.append_audit("HALT", actor, {"reason": reason})

    def resume(self, actor: str = "user") -> None:
        state = self.get_system_state()
        state.halted = False
        state.halt_reason = None
        self.set_system_state(state)
        halt_file = DATA_DIR / "HALT"
        if halt_file.exists():
            halt_file.unlink()
        self.append_audit("RESUME", actor, {})
