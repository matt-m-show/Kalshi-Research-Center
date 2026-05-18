"""Pydantic v2 models for all ledger artifacts: recommendations, decisions, orders, fills."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderType(str, Enum):
    LIMIT = "LIMIT"


class TimeHorizon(str, Enum):
    INTRADAY = "intraday"
    SWING = "swing"
    HOLD_TO_RESOLUTION = "hold-to-resolution"
    MULTI_WEEK = "multi_week"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TrustTier(int, Enum):
    PRIMARY = 1       # wire services, official sources
    AGGREGATOR = 2    # reputable analysis / news aggregators
    SOCIAL = 3        # X, Reddit, forums


class DecisionVerdict(str, Enum):
    APPROVE = "APPROVE"
    MODIFY = "MODIFY"
    DENY = "DENY"


class OrderStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"   # awaiting HITL sign-off
    APPROVED = "APPROVED"                   # approved, not yet sent
    SUBMITTED = "SUBMITTED"                 # sent to Kalshi
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class KalshiEnv(str, Enum):
    DEMO = "demo"
    PROD = "prod"


class OperationalMode(str, Enum):
    PAPER = "paper"       # demo API, no real money
    HITL = "hitl"         # live API, HITL approval per trade
    AUTONOMOUS = "autonomous"  # live API, fully autonomous


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


class ExitConditions(BaseModel):
    take_profit_prob: float = Field(..., ge=0.0, le=1.0)
    stop_loss_prob: float = Field(..., ge=0.0, le=1.0)
    invalidation_event: str


class Source(BaseModel):
    title: str
    url: str
    retrieved_at: datetime
    trust_tier: TrustTier


class HardConstraintsDeclared(BaseModel):
    must_be_in_universe: bool
    respects_max_position_pct: bool


class Recommendation(BaseModel):
    rec_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    market_ticker: str
    event_ticker: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    limit_price_cents: int = Field(..., ge=1, le=99)
    target_size_contracts: int = Field(..., gt=0)
    estimated_fair_prob: float = Field(..., ge=0.0, le=1.0)
    market_implied_prob: float = Field(..., ge=0.0, le=1.0)
    edge_bps: int = Field(..., description="Estimated edge in basis points")
    time_horizon: TimeHorizon
    exit_conditions: ExitConditions
    confidence: Confidence
    thesis: str = Field(..., min_length=1, max_length=2000)
    sources: list[Source] = Field(..., min_length=1)
    hard_constraints_declared: HardConstraintsDeclared

    @field_validator("time_horizon", mode="before")
    @classmethod
    def normalise_time_horizon(cls, v: Any) -> Any:
        """Accept both 'multi-week' (hyphen) and 'multi_week' (underscore)."""
        if v == "multi-week":
            return "multi_week"
        return v

    @field_validator("sources")
    @classmethod
    def at_least_one_tier1(cls, sources: list[Source]) -> list[Source]:
        tiers = {s.trust_tier for s in sources}
        if TrustTier.PRIMARY not in tiers:
            raise ValueError("At least one tier-1 (primary) source is required")
        return sources


# ---------------------------------------------------------------------------
# QA Decision
# ---------------------------------------------------------------------------


class ApprovedOrder(BaseModel):
    market_ticker: str
    side: Side
    order_type: OrderType
    limit_price_cents: int
    size_contracts: int
    time_in_force: str = "GTD"
    expiry_ts: datetime | None = None
    client_order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class Decision(BaseModel):
    rec_id: str
    decision: DecisionVerdict
    reason_codes: list[str]
    modifications: dict[str, Any] = Field(default_factory=dict)
    approved_order: ApprovedOrder | None = None
    qa_notes: str = ""
    decided_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Order & Fill
# ---------------------------------------------------------------------------


class Order(BaseModel):
    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_order_id: str
    rec_id: str
    market_ticker: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    limit_price_cents: int
    size_contracts: int
    filled_contracts: int = 0
    status: OrderStatus = OrderStatus.PENDING_APPROVAL
    submitted_at: datetime | None = None
    last_updated_at: datetime = Field(default_factory=datetime.utcnow)
    kalshi_order_id: str | None = None   # ID returned by Kalshi API
    env: KalshiEnv = KalshiEnv.DEMO


class Fill(BaseModel):
    fill_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kalshi_trade_id: str
    order_id: str
    rec_id: str
    market_ticker: str
    side: Side
    price_cents: int
    count: int
    is_taker: bool
    fee_cents: int = 0
    filled_at: datetime = Field(default_factory=datetime.utcnow)
    env: KalshiEnv = KalshiEnv.DEMO


# ---------------------------------------------------------------------------
# Audit log entry (append-only JSONL)
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    seq: int | None = None      # assigned by ledger on write
    event_ts: datetime = Field(default_factory=datetime.utcnow)
    event_type: str             # e.g. RECOMMENDATION, DECISION, ORDER_SUBMITTED, HALT, etc.
    actor: str                  # agent name or "user"
    payload: dict[str, Any]
    prev_hash: str | None = None  # SHA-256 of previous entry for chain integrity


# ---------------------------------------------------------------------------
# System state
# ---------------------------------------------------------------------------


class SystemState(BaseModel):
    mode: OperationalMode = OperationalMode.PAPER
    halted: bool = False
    halt_reason: str | None = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)
