"""Typed artifacts. Agents only hand off these validated shapes, never free text."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Sourced(BaseModel):
    claim: str
    source_url: str = Field(description="URL the claim came from; empty only for client-provided facts")


# ---------------- research ----------------------------------------------------
class VoiceProfile(BaseModel):
    summary: str = Field(description="Two sentences describing how the founder writes")
    tone: list[str] = Field(min_length=2, max_length=6)
    sentence_style: str
    formatting_rules: list[str] = Field(min_length=1, max_length=8)
    vocabulary_use: list[str] = Field(max_length=12)
    vocabulary_avoid: list[str] = Field(max_length=12)
    signature_themes: list[str] = Field(min_length=1, max_length=6)
    example_hooks: list[str] = Field(min_length=1, max_length=5)
    learned_rules: list[str] = Field(default_factory=list, description="Rules learned from the founder's edits")


class MarketReport(BaseModel):
    industry: str
    summary: str
    buyer_segments: list[str] = Field(min_length=1, max_length=6)
    buyer_pains: list[str] = Field(min_length=1, max_length=8)
    key_facts: list[Sourced] = Field(max_length=10)


class Competitor(BaseModel):
    name: str
    positioning: str
    offerings: list[str] = Field(max_length=6)
    content_themes: list[str] = Field(max_length=6)
    posting_cadence: str
    strengths: list[str] = Field(max_length=4)
    weaknesses: list[str] = Field(max_length=4)
    source_urls: list[str] = Field(default_factory=list)


class CompetitorMatrix(BaseModel):
    competitors: list[Competitor] = Field(min_length=1, max_length=6)
    whitespace: list[str] = Field(min_length=1, max_length=6, description="Content angles competitors leave open")


class Trend(BaseModel):
    name: str
    summary: str
    evidence_url: str
    relevance: int = Field(ge=1, le=5)
    content_angle: str


class TrendReport(BaseModel):
    trends: list[Trend] = Field(min_length=1, max_length=8)


# ---------------- strategy ----------------------------------------------------
class Pillar(BaseModel):
    name: str
    description: str
    share_pct: int = Field(ge=5, le=60)
    example_angles: list[str] = Field(min_length=1, max_length=5)


class ContentStrategy(BaseModel):
    positioning: str
    pillars: list[Pillar] = Field(min_length=3, max_length=5)
    format_mix: dict[str, int] = Field(description="format -> share percent")
    posts_per_week: int = Field(ge=4, le=7)
    goals: list[str] = Field(min_length=1, max_length=4)

    @field_validator("pillars")
    @classmethod
    def shares_sum(cls, v: list[Pillar]) -> list[Pillar]:
        total = sum(p.share_pct for p in v)
        if not 90 <= total <= 110:
            raise ValueError(f"pillar share_pct must sum to ~100, got {total}")
        return v


class ICPSegment(BaseModel):
    name: str
    titles: list[str] = Field(min_length=1, max_length=10)
    industries: list[str] = Field(min_length=1, max_length=8)
    min_employees: int = Field(ge=1)
    max_employees: int
    geos: list[str] = Field(min_length=1)
    pains: list[str] = Field(max_length=5)
    buying_triggers: list[str] = Field(max_length=6)


class ICP(BaseModel):
    segments: list[ICPSegment] = Field(min_length=1, max_length=3)
    exclusions: list[str] = Field(default_factory=list)
    scoring_notes: str


FORMATS = ["story", "how_to", "opinion", "data_insight", "carousel_outline", "case_study", "question"]


class SlotPlan(BaseModel):
    week: int = Field(ge=1, le=104, description="Week number counted from the calendar start")
    day: str = Field(description="Mon..Sun")
    pillar: str
    format: str
    topic: str
    angle: str
    trend_ref: str = ""

    @field_validator("format")
    @classmethod
    def known_format(cls, v: str) -> str:
        if v not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        return v


class CalendarPlan(BaseModel):
    slots: list[SlotPlan] = Field(min_length=1)


# ---------------- content -----------------------------------------------------
class PostDraft(BaseModel):
    text: str = Field(description="The full LinkedIn post, plain text with line breaks")
    hashtags: list[str] = Field(default_factory=list, max_length=8)
    sources_used: list[str] = Field(default_factory=list, description="URLs of sources whose facts the post uses")
    rationale: str = Field(description="One sentence: why this post, for this audience, now")


class Critique(BaseModel):
    voice_match: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    hook: int = Field(ge=1, le=5)
    insight: int = Field(ge=1, le=5)
    cta_fit: int = Field(ge=1, le=5)
    must_fix: list[str] = Field(default_factory=list, max_length=6)
    verdict: str

    @property
    def overall(self) -> float:
        return round((self.voice_match * 1.5 + self.specificity + self.hook + self.insight + self.cta_fit * 0.5) / 5, 2)


class LearnedRules(BaseModel):
    rules: list[str] = Field(max_length=8, description="Concrete, reusable writing rules inferred from the edits")


# ---------------- leads -------------------------------------------------------
class ProspectingSummary(BaseModel):
    searches: list[str] = Field(description="Short description of each search you ran")
    notes: str = ""


class LeadScore(BaseModel):
    prospect_id: str
    fit_score: int = Field(ge=0, le=100)
    segment: str = ""
    evidence: list[str] = Field(default_factory=list, max_length=4)
    disqualifiers: list[str] = Field(default_factory=list, max_length=4)


class LeadScores(BaseModel):
    scores: list[LeadScore]


class OutreachDraft(BaseModel):
    message: str
    alt_message: str
    personalization_hook: str


class OutreachCritique(BaseModel):
    personalization: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    must_fix: list[str] = Field(default_factory=list, max_length=4)

    @property
    def overall(self) -> float:
        return round((self.personalization + self.relevance + self.clarity) / 3, 2)
