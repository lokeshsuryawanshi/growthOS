"""Typed artifacts. Agents only hand off these validated shapes, never free text."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Sourced(BaseModel):
    claim: str
    source_url: str = Field(description="URL the claim came from; empty only for client-provided facts")
    verified: bool = Field(default=False, description="Set by code: the cited source was seen and its text supports the claim")
    status: str = Field(default="verified", description="Set by code: verified | outdated")
    official: bool = Field(default=False, description="Set by code: the source is the client's or the company's own site")
    note: str = Field(default="", description="Set by code")


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
    key_facts: list[Sourced] = Field(default_factory=list, max_length=10)


class Competitor(BaseModel):
    name: str
    positioning: str
    offerings: list[str] = Field(default_factory=list, max_length=6)
    content_themes: list[str] = Field(default_factory=list, max_length=6)
    posting_cadence: str = "unknown"
    strengths: list[str] = Field(default_factory=list, max_length=4)
    weaknesses: list[str] = Field(default_factory=list, max_length=4)
    source_urls: list[str] = Field(default_factory=list)
    verified: bool = False         # set by code: a cited page was seen and its text supports positioning and offerings
    official_source: bool = False  # set by code: one of the sources is the competitor's own website
    note: str = ""                 # set by code: why it is not verified


class CompetitorMatrix(BaseModel):
    competitors: list[Competitor] = Field(min_length=1, max_length=8)
    whitespace: list[str] = Field(min_length=1, max_length=6, description="Content angles competitors leave open")


class Trend(BaseModel):
    name: str
    summary: str
    evidence_url: str = ""
    verified: bool = False   # set by code: the evidence URL was seen and its text supports the summary
    relevance: int = Field(default=3, ge=1, le=5)
    content_angle: str
    note: str = ""           # set by code: e.g. the evidence is several years old


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
    posts_per_week: int = Field(ge=3, le=7)
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
class Claim(BaseModel):
    number: str = Field(description="The figure exactly as written in the post, e.g. '83%' or '1,100'")
    statement: str = Field(description="The sentence of the post that uses the figure")
    source_url: str = Field(description="URL of the source (or 'client:verified-facts')")
    source_quote: str = Field(description="Verbatim text copied from the source that contains this figure")


class PostDraft(BaseModel):
    text: str = Field(description="The full LinkedIn post, plain text with line breaks")
    hashtags: list[str] = Field(default_factory=list, max_length=8)
    sources_used: list[str] = Field(default_factory=list, description="URLs of sources whose facts the post uses")
    claims: list[Claim] = Field(default_factory=list, description="One entry for every figure used in the post")
    rationale: str = Field(description="One sentence: why this post, for this audience, now")


class Critique(BaseModel):
    voice_match: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    opening: int = Field(ge=1, le=5, description="opens the way this founder opens")
    insight: int = Field(ge=1, le=5)
    ending: int = Field(ge=1, le=5, description="ends the way this founder ends")
    must_fix: list[str] = Field(default_factory=list, max_length=6)
    verdict: str

    @property
    def overall(self) -> float:
        return round((self.voice_match * 1.5 + self.specificity + self.opening + self.insight + self.ending * 0.5) / 5, 2)


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
    subject: str = Field(default="", max_length=120)
    message: str
    alt_message: str = ""
    personalization_hook: str = ""


class Candidate(BaseModel):
    name: str
    domain: str = Field(description="The company's own website domain, e.g. acme.com")
    evidence_url: str = Field(description="A URL returned by the search that mentions this company")
    signal: str = Field(default="", description="One concrete fact from that page showing why they are a fit now")
    why_fit: str = ""


class CompanyCandidates(BaseModel):
    companies: list[Candidate] = Field(default_factory=list, max_length=25,
                                       description="Companies named in the search results; empty if none fit")


class OutreachCritique(BaseModel):
    personalization: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    must_fix: list[str] = Field(default_factory=list, max_length=4)

    @property
    def overall(self) -> float:
        return round((self.personalization + self.relevance + self.clarity) / 3, 2)
