"""SQLite ledger: single source of truth for all artifacts.

Writes are append-or-update by server-assigned ID. JSON artifact files in
data/ are written alongside the DB so every step can be replayed from disk.
The audit table and data/audit.log are hash-chained and append-only.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    bindparam,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.common.schemas import (
    Assessment,
    AuditEntry,
    Decision,
    Fill,
    MarketSettlement,
    Order,
    OrderStatus,
    Recommendation,
    ResearchRun,
    SystemState,
    utcnow,
)

DATA_DIR = Path(os.getenv("KALSHI_DATA_DIR") or Path(__file__).parents[2] / "data")
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
    created_at = Column(DateTime, index=True)
    market_ticker = Column(String, index=True)
    side = Column(String)
    limit_price_cents = Column(Integer)
    target_size_contracts = Column(Integer)
    edge_bps = Column(Integer)
    confidence = Column(String)
    json_blob = Column(Text)


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
    kalshi_order_id = Column(String, index=True, nullable=True)
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
    kalshi_trade_id = Column(String, index=True, nullable=True)
    json_blob = Column(Text)


class AssessmentRow(Base):
    __tablename__ = "assessments"
    assessment_id = Column(String, primary_key=True)
    run_id = Column(String, index=True)
    created_at = Column(DateTime, index=True)
    market_ticker = Column(String, index=True)
    series_ticker = Column(String, index=True)
    fair_yes_prob = Column(Float)
    resolved_yes = Column(Integer, nullable=True)   # NULL until settled
    json_blob = Column(Text)


class RunRow(Base):
    __tablename__ = "runs"
    run_id = Column(String, primary_key=True)
    started_at = Column(DateTime, index=True)
    status = Column(String)
    cost_usd = Column(Float)
    json_blob = Column(Text)


class SettlementRow(Base):
    __tablename__ = "settlements"
    market_ticker = Column(String, primary_key=True)
    settled_at = Column(DateTime, index=True)
    result = Column(String)
    pnl_cents = Column(Integer)
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
    halted = Column(Integer)
    halt_reason = Column(String, nullable=True)
    updated_at = Column(DateTime)


# Columns added after the first release; created on existing databases.
_ADDED_COLUMNS = [
    ("orders", "kalshi_order_id", "VARCHAR"),
    ("fills", "kalshi_trade_id", "VARCHAR"),
]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class Ledger:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)

        @event.listens_for(self._engine, "connect")
        def _set_pragmas(dbapi_conn, _conn_rec):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self._engine)
        self._migrate()
        self._Session = sessionmaker(bind=self._engine)
        self._ensure_system_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        return self._Session()

    def _migrate(self) -> None:
        with self._engine.begin() as conn:
            for table, col, coltype in _ADDED_COLUMNS:
                existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))

    def _ensure_system_state(self) -> None:
        with self._session() as s:
            if not s.get(SystemStateRow, 1):
                s.add(SystemStateRow(id=1, mode="paper", halted=0, halt_reason=None,
                                     updated_at=utcnow()))
                s.commit()

    @staticmethod
    def _hash_entry(payload_json: str, prev_hash: str | None) -> str:
        return hashlib.sha256(((prev_hash or "") + payload_json).encode()).hexdigest()

    def _last_audit_hash(self, s: Session) -> str | None:
        row = s.execute(text("SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1")).fetchone()
        return row[0] if row else None

    def _write_artifact(self, subdir: str, filename: str, obj: dict) -> None:
        path = DATA_DIR / subdir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------
    # Assessments
    # ------------------------------------------------------------------

    def save_assessment(self, a: Assessment) -> None:
        with self._session() as s:
            s.merge(AssessmentRow(
                assessment_id=a.assessment_id,
                run_id=a.run_id,
                created_at=a.created_at,
                market_ticker=a.market_ticker,
                series_ticker=a.series_ticker,
                fair_yes_prob=a.fair_yes_prob,
                resolved_yes=None if a.resolved_yes is None else int(a.resolved_yes),
                json_blob=a.model_dump_json(),
            ))
            s.commit()

    def get_assessment(self, assessment_id: str) -> Assessment | None:
        with self._session() as s:
            row = s.get(AssessmentRow, assessment_id)
            return Assessment.model_validate_json(row.json_blob) if row else None

    def list_assessments(
        self, run_id: str | None = None, limit: int = 200, resolved_only: bool = False
    ) -> list[Assessment]:
        sql = "SELECT json_blob FROM assessments WHERE 1=1"
        params: dict = {"lim": limit}
        if run_id:
            sql += " AND run_id = :run"
            params["run"] = run_id
        if resolved_only:
            sql += " AND resolved_yes IS NOT NULL"
        sql += " ORDER BY created_at DESC LIMIT :lim"
        with self._session() as s:
            rows = s.execute(text(sql), params).fetchall()
        return [Assessment.model_validate_json(r[0]) for r in rows]

    def recently_assessed_tickers(self, hours: float) -> set[str]:
        cutoff = utcnow() - timedelta(hours=hours)
        with self._session() as s:
            rows = s.execute(
                text("SELECT DISTINCT market_ticker FROM assessments WHERE created_at >= :c"),
                {"c": cutoff},
            ).fetchall()
        return {r[0] for r in rows}

    def unresolved_assessment_tickers(self) -> set[str]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT DISTINCT market_ticker FROM assessments WHERE resolved_yes IS NULL")
            ).fetchall()
        return {r[0] for r in rows}

    def resolve_assessments(self, market_ticker: str, resolved_yes: bool, at: datetime) -> int:
        """Score every assessment of a market once it settles. Returns rows updated."""
        updated = 0
        with self._session() as s:
            rows = s.execute(
                text("SELECT assessment_id, json_blob FROM assessments "
                     "WHERE market_ticker = :t AND resolved_yes IS NULL"),
                {"t": market_ticker},
            ).fetchall()
            for aid, blob in rows:
                a = Assessment.model_validate_json(blob)
                a.resolved_yes, a.resolved_at = resolved_yes, at
                s.execute(
                    text("UPDATE assessments SET resolved_yes = :r, json_blob = :b "
                         "WHERE assessment_id = :id"),
                    {"r": int(resolved_yes), "b": a.model_dump_json(), "id": aid},
                )
                updated += 1
            s.commit()
        return updated

    # ------------------------------------------------------------------
    # Research runs
    # ------------------------------------------------------------------

    def save_run(self, run: ResearchRun) -> None:
        with self._session() as s:
            s.merge(RunRow(run_id=run.run_id, started_at=run.started_at, status=run.status,
                           cost_usd=run.cost_usd, json_blob=run.model_dump_json()))
            s.commit()

    def get_run(self, run_id: str) -> ResearchRun | None:
        with self._session() as s:
            row = s.get(RunRow, run_id)
            return ResearchRun.model_validate_json(row.json_blob) if row else None

    def list_runs(self, limit: int = 50) -> list[ResearchRun]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM runs ORDER BY started_at DESC LIMIT :lim"),
                {"lim": limit},
            ).fetchall()
        return [ResearchRun.model_validate_json(r[0]) for r in rows]

    def api_spend_since(self, since: datetime) -> float:
        with self._session() as s:
            val = s.execute(
                text("SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE started_at >= :s"),
                {"s": since},
            ).scalar()
        return float(val or 0.0)

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------

    def save_recommendation(self, rec: Recommendation) -> None:
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
                json_blob=rec.model_dump_json(),
            ))
            s.commit()
        self._write_artifact("recommendations", f"{rec.rec_id}.json", rec.model_dump(mode="json"))
        self.append_audit("RECOMMENDATION", "pricing_engine",
                          {"rec_id": rec.rec_id, "market_ticker": rec.market_ticker})

    def get_recommendation(self, rec_id: str) -> Recommendation | None:
        with self._session() as s:
            row = s.get(RecommendationRow, rec_id)
            return Recommendation.model_validate_json(row.json_blob) if row else None

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def save_decision(self, decision: Decision) -> None:
        with self._session() as s:
            s.merge(DecisionRow(
                rec_id=decision.rec_id,
                decided_at=decision.decided_at,
                decision=decision.decision.value,
                reason_codes=json.dumps(decision.reason_codes),
                json_blob=decision.model_dump_json(),
            ))
            s.commit()
        self._write_artifact("decisions", f"{decision.rec_id}.json", decision.model_dump(mode="json"))
        self.append_audit("DECISION", "qa_engine",
                          {"rec_id": decision.rec_id, "verdict": decision.decision.value})

    def get_decision(self, rec_id: str) -> Decision | None:
        with self._session() as s:
            row = s.get(DecisionRow, rec_id)
            return Decision.model_validate_json(row.json_blob) if row else None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def save_order(self, order: Order) -> None:
        with self._session() as s:
            s.merge(OrderRow(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                rec_id=order.rec_id,
                market_ticker=order.market_ticker,
                status=order.status.value,
                submitted_at=order.submitted_at,
                last_updated_at=order.last_updated_at,
                kalshi_order_id=order.kalshi_order_id,
                json_blob=order.model_dump_json(),
            ))
            s.commit()
        self._write_artifact("orders", f"{order.order_id}.json", order.model_dump(mode="json"))

    def get_order(self, order_id: str) -> Order | None:
        with self._session() as s:
            row = s.get(OrderRow, order_id)
            return Order.model_validate_json(row.json_blob) if row else None

    def get_orders_by_status(self, *statuses: str) -> list[Order]:
        if not statuses:
            return []
        stmt = text("SELECT json_blob FROM orders WHERE status IN :st").bindparams(
            bindparam("st", expanding=True)
        )
        with self._session() as s:
            rows = s.execute(stmt, {"st": list(statuses)}).fetchall()
        return [Order.model_validate_json(r[0]) for r in rows]

    def get_orders_for_rec(self, rec_id: str) -> list[Order]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM orders WHERE rec_id = :r ORDER BY last_updated_at"),
                {"r": rec_id},
            ).fetchall()
        return [Order.model_validate_json(r[0]) for r in rows]

    def get_orders_for_ticker(self, ticker: str) -> list[Order]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM orders WHERE market_ticker = :t ORDER BY last_updated_at"),
                {"t": ticker},
            ).fetchall()
        return [Order.model_validate_json(r[0]) for r in rows]

    def get_order_by_kalshi_id(self, kalshi_order_id: str) -> Order | None:
        with self._session() as s:
            row = s.execute(
                text("SELECT json_blob FROM orders WHERE kalshi_order_id = :k"),
                {"k": kalshi_order_id},
            ).fetchone()
        return Order.model_validate_json(row[0]) if row else None

    def list_orders(self, limit: int = 100) -> list[Order]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM orders ORDER BY last_updated_at DESC LIMIT :lim"),
                {"lim": limit},
            ).fetchall()
        return [Order.model_validate_json(r[0]) for r in rows]

    def expire_unsent_orders(self, older_than_hours: float, actor: str = "order_manager") -> int:
        """Expire APPROVED / PENDING_APPROVAL orders that were never sent to Kalshi."""
        cutoff = utcnow() - timedelta(hours=older_than_hours)
        n = 0
        for o in self.get_orders_by_status(OrderStatus.APPROVED.value,
                                           OrderStatus.PENDING_APPROVAL.value):
            if o.kalshi_order_id is None and o.last_updated_at < cutoff:
                o.status = OrderStatus.EXPIRED
                o.status_reason = f"Never submitted; expired after {older_than_hours:g}h"
                o.last_updated_at = utcnow()
                self.save_order(o)
                n += 1
        if n:
            self.append_audit("ORDERS_EXPIRED_UNSENT", actor, {"count": n})
        return n

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def save_fill(self, fill: Fill) -> None:
        with self._session() as s:
            s.merge(FillRow(
                fill_id=fill.fill_id,
                order_id=fill.order_id,
                rec_id=fill.rec_id,
                market_ticker=fill.market_ticker,
                price_cents=fill.price_cents,
                count=fill.count,
                filled_at=fill.filled_at,
                kalshi_trade_id=fill.kalshi_trade_id,
                json_blob=fill.model_dump_json(),
            ))
            s.commit()
        self._write_artifact("fills", f"{fill.fill_id}.json", fill.model_dump(mode="json"))
        self.append_audit("FILL", "order_manager",
                          {"fill_id": fill.fill_id, "order_id": fill.order_id,
                           "ticker": fill.market_ticker, "count": fill.count,
                           "price": fill.price_cents})

    def has_fill(self, kalshi_trade_id: str) -> bool:
        with self._session() as s:
            row = s.execute(
                text("SELECT 1 FROM fills WHERE kalshi_trade_id = :t LIMIT 1"),
                {"t": kalshi_trade_id},
            ).fetchone()
        return row is not None

    def get_fills_for_order(self, order_id: str) -> list[Fill]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM fills WHERE order_id = :oid"), {"oid": order_id}
            ).fetchall()
        return [Fill.model_validate_json(r[0]) for r in rows]

    def get_fills_for_ticker(self, ticker: str) -> list[Fill]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT json_blob FROM fills WHERE market_ticker = :t ORDER BY filled_at"),
                {"t": ticker},
            ).fetchall()
        return [Fill.model_validate_json(r[0]) for r in rows]

    def latest_fill_time(self) -> datetime | None:
        with self._session() as s:
            val = s.execute(text("SELECT MAX(filled_at) FROM fills")).scalar()
        if val is None:
            return None
        return val if isinstance(val, datetime) else datetime.fromisoformat(str(val))

    # ------------------------------------------------------------------
    # Settlements
    # ------------------------------------------------------------------

    def save_settlement(self, st: MarketSettlement) -> bool:
        """Insert a settlement; returns False if this market was already recorded."""
        with self._session() as s:
            if s.get(SettlementRow, st.market_ticker):
                return False
            s.add(SettlementRow(market_ticker=st.market_ticker, settled_at=st.settled_at,
                                result=st.result, pnl_cents=st.pnl_cents,
                                json_blob=st.model_dump_json()))
            s.commit()
        self.append_audit("SETTLEMENT", "order_manager",
                          {"ticker": st.market_ticker, "result": st.result, "pnl_cents": st.pnl_cents})
        return True

    def list_settlements(self, since: datetime | None = None) -> list[MarketSettlement]:
        sql, params = "SELECT json_blob FROM settlements", {}
        if since:
            sql += " WHERE settled_at >= :s"
            params["s"] = since
        sql += " ORDER BY settled_at"
        with self._session() as s:
            rows = s.execute(text(sql), params).fetchall()
        return [MarketSettlement.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Audit log (append-only, hash-chained)
    # ------------------------------------------------------------------

    def append_audit(self, event_type: str, actor: str, payload: dict) -> None:
        payload_json = json.dumps(payload, default=str)
        with self._session() as s:
            prev_hash = self._last_audit_hash(s)
            row = AuditRow(
                event_ts=utcnow(),
                event_type=event_type,
                actor=actor,
                payload_json=payload_json,
                prev_hash=prev_hash,
                entry_hash=self._hash_entry(payload_json, prev_hash),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            entry = AuditEntry(seq=row.seq, event_ts=row.event_ts, event_type=event_type,
                               actor=actor, payload=payload, prev_hash=prev_hash)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

    def list_audit(self, limit: int = 100) -> list[dict]:
        with self._session() as s:
            rows = s.execute(
                text("SELECT seq, event_ts, event_type, actor, payload_json FROM audit "
                     "ORDER BY seq DESC LIMIT :lim"),
                {"lim": limit},
            ).fetchall()
        return [{"seq": r[0], "ts": str(r[1]), "type": r[2], "actor": r[3],
                 "payload": json.loads(r[4])} for r in rows]

    # ------------------------------------------------------------------
    # System state
    # ------------------------------------------------------------------

    def get_system_state(self) -> SystemState:
        with self._session() as s:
            row = s.get(SystemStateRow, 1)
            return SystemState(mode=row.mode, halted=bool(row.halted),
                               halt_reason=row.halt_reason, updated_at=row.updated_at)

    def set_system_state(self, state: SystemState, actor: str = "system") -> None:
        with self._session() as s:
            row = s.get(SystemStateRow, 1)
            row.mode = state.mode.value
            row.halted = int(state.halted)
            row.halt_reason = state.halt_reason
            row.updated_at = utcnow()
            s.commit()
        self.append_audit("SYSTEM_STATE_CHANGE", actor, state.model_dump(mode="json"))

    def is_halted(self) -> bool:
        if (DATA_DIR / "HALT").exists():
            return True
        return self.get_system_state().halted

    def halt(self, reason: str, actor: str = "system") -> None:
        state = self.get_system_state()
        state.halted, state.halt_reason = True, reason
        self.set_system_state(state, actor)
        (DATA_DIR / "HALT").touch()
        self.append_audit("HALT", actor, {"reason": reason})

    def resume(self, actor: str = "user") -> None:
        state = self.get_system_state()
        state.halted, state.halt_reason = False, None
        self.set_system_state(state, actor)
        halt_file = DATA_DIR / "HALT"
        if halt_file.exists():
            halt_file.unlink()
        self.append_audit("RESUME", actor, {})
