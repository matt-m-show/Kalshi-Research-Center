"""Pydantic v2 models for all ledger artifacts.

Identity rule: every ID and timestamp on these artifacts is assigned by the
server. Nothing an LLM writes is trusted to be unique or accurate.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    """Naive UTC timestamp (the ledger stores naive UTC throughout)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Side(str, Enum):
    YES = "YES"
    NO = "NO"

    @property
    def other(self) -> "Side":
        return Side.NO if self == Side.YES else Side.YES


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
    PRIMARY = 1       # wire services, official sources, sportsbook consensus
    AGGREGATOR = 2    # reputable analysis / news aggregators
    SOCIAL = 3        # X, Reddit, forums


class DecisionVerdict(str, Enum):
    APPROVE = "APPROVE"
    MODIFY = "MODIFY"
    DENY = "DENY"


class OrderStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"   # awaiting HITL sign-off
    APPROVED = "APPROVED"                   # approved, not yet sent
    SUBMITTED = "SUBMITTED"                 # resting on Kalshi
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


OPEN_ORDER_STATUSES = (
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)


class OrderPurpose(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"


class KalshiEnv(str, Enum):
    DEMO = "demo"
    PROD = "prod"


class OperationalMode(str, Enum):
    PAPER = "paper"            # orders are recorded but never sent
    HITL = "hitl"              # each order waits for approval in the dashboard
    AUTONOMOUS = "autonomous"  # orders are sent as soon as they pass QA


# ---------------------------------------------------------------------------
# Research artifacts
# ---------------------------------------------------------------------------


class Source(BaseModel):
    title: str
    url: str
    retrieved_at: datetime = Field(default_factory=utcnow)
    trust_tier: TrustTier = TrustTier.AGGREGATOR


class Assessment(BaseModel):
    """One market the research model evaluated, whether or not it was traded.

    Every assessment is scored against the market's result once it settles, so
    calibration data accumulates from every estimate, not just executed trades.
    """

    assessment_id: str = Field(default_factory=new_id)
    run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    market_ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    title: str = ""
    fair_yes_prob: float | None = Field(None, ge=0.0, le=1.0)   # None when skipped
    skip_reason: str | None = None     # set when the model declined to estimate
    confidence: Confidence = Confidence.MEDIUM
    thesis: str = ""
    key_factors: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    invalidation_event: str = ""
    model: str = ""
    # Market snapshot when the estimate was recorded (YES terms, cents)
    yes_bid: int | None = None
    yes_ask: int | None = None
    market_close_time: datetime | None = None
    # Filled in once the market settles
    resolved_yes: bool | None = None
    resolved_at: datetime | None = None

    @field_validator("thesis")
    @classmethod
    def clip_thesis(cls, v: str) -> str:
        return v[:2000]


class ExitConditions(BaseModel):
    take_profit_prob: float = Field(..., ge=0.0, le=1.0)
    stop_loss_prob: float = Field(..., ge=0.0, le=1.0)
    invalidation_event: str = ""


class HardConstraintsDeclared(BaseModel):
    must_be_in_universe: bool = True
    respects_max_position_pct: bool = True


class Recommendation(BaseModel):
    """A trade proposal built by the pricing engine from an Assessment.

    ``estimated_fair_prob`` is the fair probability of the side being bought;
    ``fair_yes_prob`` is always in YES terms. Prices are in cents of the side
    being bought.
    """

    rec_id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    assessment_id: str | None = None
    run_id: str | None = None
    market_ticker: str
    event_ticker: str
    title: str = ""
    side: Side
    order_type: OrderType = OrderType.LIMIT
    limit_price_cents: int = Field(..., ge=1, le=99)
    max_price_cents: int | None = None
    target_size_contracts: int = Field(..., ge=0)
    estimated_fair_prob: float = Field(..., ge=0.0, le=1.0)
    fair_yes_prob: float | None = None
    market_implied_prob: float = Field(0.0, ge=0.0, le=1.0)
    edge_bps: int = 0
    edge_cents_after_fees: float | None = None
    fee_cents_per_contract: float | None = None
    time_horizon: TimeHorizon = TimeHorizon.HOLD_TO_RESOLUTION
    exit_conditions: ExitConditions | None = None
    confidence: Confidence = Confidence.MEDIUM
    thesis: str = Field("", max_length=4000)
    sources: list[Source] = Field(default_factory=list)
    hard_constraints_declared: HardConstraintsDeclared = Field(
        default_factory=HardConstraintsDeclared
    )
    market_close_time: datetime | None = None

    @field_validator("time_horizon", mode="before")
    @classmethod
    def normalise_time_horizon(cls, v: Any) -> Any:
        if v == "multi-week":
            return "multi_week"
        return v


# ---------------------------------------------------------------------------
# QA Decision
# ---------------------------------------------------------------------------


class ApprovedOrder(BaseModel):
    market_ticker: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    limit_price_cents: int
    max_price_cents: int | None = None     # never pay more than this when repricing
    size_contracts: int
    time_in_force: str = "good_till_canceled"
    post_only: bool = True
    expiry_ts: datetime | None = None
    client_order_id: str = Field(default_factory=new_id)


class Decision(BaseModel):
    rec_id: str
    decision: DecisionVerdict
    reason_codes: list[str]
    modifications: dict[str, Any] = Field(default_factory=dict)
    approved_order: ApprovedOrder | None = None
    qa_notes: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Order & Fill
# ---------------------------------------------------------------------------


class Order(BaseModel):
    order_id: str = Field(default_factory=new_id)
    client_order_id: str
    rec_id: str
    market_ticker: str
    side: Side                        # the outcome side whose contracts move
    action: str = "buy"               # "buy" | "sell"
    purpose: OrderPurpose = OrderPurpose.ENTRY
    order_type: OrderType = OrderType.LIMIT
    limit_price_cents: int
    max_price_cents: int | None = None
    size_contracts: int
    filled_contracts: int = 0
    avg_fill_price_cents: float | None = None
    fees_cents: int = 0
    status: OrderStatus = OrderStatus.PENDING_APPROVAL
    status_reason: str = ""
    submitted_at: datetime | None = None
    last_updated_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    market_close_time: datetime | None = None
    reprice_count: int = 0
    replaces_order_id: str | None = None   # set when this order re-priced an earlier one
    kalshi_order_id: str | None = None
    env: KalshiEnv = KalshiEnv.DEMO

    @property
    def remaining_contracts(self) -> int:
        return max(0, self.size_contracts - self.filled_contracts)


class Fill(BaseModel):
    fill_id: str = Field(default_factory=new_id)
    kalshi_trade_id: str
    order_id: str
    rec_id: str
    market_ticker: str
    side: Side
    action: str = "buy"
    price_cents: int
    count: int
    is_taker: bool
    fee_cents: int = 0
    filled_at: datetime = Field(default_factory=utcnow)
    env: KalshiEnv = KalshiEnv.DEMO


class MarketSettlement(BaseModel):
    """Our P&L on one settled market, as reported by Kalshi."""

    market_ticker: str
    event_ticker: str = ""
    result: str                       # "yes" | "no" | "scalar"
    yes_count: int = 0
    no_count: int = 0
    cost_cents: int = 0
    revenue_cents: int = 0
    fee_cents: int = 0
    pnl_cents: int = 0
    settled_at: datetime | None = None


# ---------------------------------------------------------------------------
# Research run record
# ---------------------------------------------------------------------------


class ResearchRun(BaseModel):
    run_id: str = Field(default_factory=new_id)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    trigger: str = "manual"           # "manual" | "schedule" | "event"
    status: str = "running"           # "running" | "ok" | "error" | "skipped"
    mode: str = ""
    model: str = ""
    candidates: list[dict[str, Any]] = Field(default_factory=list)  # ticker, title, stage, reason
    assessments: int = 0
    recommendations: int = 0
    approved: int = 0
    submitted: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)              # tokens + cost per model
    cost_usd: float = 0.0
    error: str | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Audit log entry (append-only JSONL)
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    seq: int | None = None
    event_ts: datetime = Field(default_factory=utcnow)
    event_type: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str | None = None


# ---------------------------------------------------------------------------
# System state
# ---------------------------------------------------------------------------


class SystemState(BaseModel):
    mode: OperationalMode = OperationalMode.PAPER
    halted: bool = False
    halt_reason: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
