"""Database models and Pydantic schemas for post capture, triage, and drafting."""

from datetime import datetime, timezone
from enum import Enum
import json
from typing import List, Optional
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import SQLModel, Field, Relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PostStatus(str, Enum):
    NEW = "new"
    DRAFTED = "drafted"
    APPROVED = "approved"
    POSTED = "posted"
    SKIPPED = "skipped"
    REJECTED = "rejected"


class DraftAngle(str, Enum):
    EXPERIENCE = "experience"
    QUESTION = "question"
    COUNTERPOINT = "counterpoint"


class PostSource(str, Enum):
    EXTENSION = "extension"
    MANUAL = "manual"
    CSV = "csv"


class Author(SQLModel, table=True):
    __tablename__ = "author"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    # Lowercased name ensures case-insensitive matching across captures without requiring profile URLs
    key: str = Field(unique=True, index=True)
    headline_or_company: Optional[str] = Field(default="")
    profile_url: Optional[str] = Field(default=None)
    comments_posted: int = Field(default=0)
    last_interaction_at: Optional[datetime] = Field(default=None)
    notes: str = Field(default="")
    outreach_override: Optional[bool] = Field(default=None)
    last_dm_draft: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    posts: List["Post"] = Relationship(back_populates="author")


class Post(SQLModel, table=True):
    __tablename__ = "post"

    id: Optional[int] = Field(default=None, primary_key=True)
    author_id: Optional[int] = Field(default=None, foreign_key="author.id")
    url: str
    normalized_url: Optional[str] = Field(default=None)
    # Dedupe key uses canonical permalink or text hash for feed clips lacking post permalinks
    dedupe_key: str = Field(unique=True, index=True)
    # Blocks 'open and copy' action until user pastes a direct post URL
    needs_permalink: bool = Field(default=False)
    page_title: Optional[str] = Field(default="")
    raw_text: str
    post_body: Optional[str] = Field(default=None)
    needs_manual_cleanup: bool = Field(default=False)
    status: str = Field(default=PostStatus.NEW.value, index=True)
    source: str = Field(default=PostSource.EXTENSION.value)

    relevance_score: Optional[int] = Field(default=None)
    opportunity_score: Optional[int] = Field(default=None)
    # User override takes precedence over model estimation for outreach calculation
    opportunity_score_override: Optional[int] = Field(default=None)
    combined_score: Optional[float] = Field(default=None)
    topic_tags_raw: Optional[str] = Field(default="[]")
    one_line_reason: Optional[str] = Field(default=None)

    final_comment: Optional[str] = Field(default=None)
    approved_draft_id: Optional[int] = Field(default=None)
    error_message: Optional[str] = Field(default=None)

    # Posts are archived upon posting rather than deleted to retain outreach relationship history
    archived: bool = Field(default=False, index=True)
    captured_at: datetime = Field(default_factory=utc_now)
    triaged_at: Optional[datetime] = Field(default=None)
    approved_at: Optional[datetime] = Field(default=None)
    posted_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=utc_now)

    author: Optional[Author] = Relationship(back_populates="posts")
    drafts: List["Draft"] = Relationship(back_populates="post")

    @property
    def effective_opportunity_score(self) -> Optional[int]:
        if self.opportunity_score_override is not None:
            return self.opportunity_score_override
        return self.opportunity_score

    @property
    def topic_tags(self) -> List[str]:
        if not self.topic_tags_raw:
            return []
        try:
            return json.loads(self.topic_tags_raw)
        except Exception:
            return []

    @topic_tags.setter
    def topic_tags(self, tags: List[str]) -> None:
        self.topic_tags_raw = json.dumps(tags or [])


class Draft(SQLModel, table=True):
    __tablename__ = "draft"

    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    angle: str = Field(index=True)
    text: str
    edited_text: Optional[str] = Field(default=None)
    grounding_used_raw: Optional[str] = Field(default="[]")
    # True when profile.md contains no factual basis for the experience angle
    is_ungrounded_placeholder: bool = Field(default=False)
    # Set when banned phrases remain after maximum regeneration retries
    needs_attention: bool = Field(default=False)
    regeneration_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=utc_now)

    post: Optional[Post] = Relationship(back_populates="drafts")

    @property
    def grounding_used(self) -> List[str]:
        if not self.grounding_used_raw:
            return []
        try:
            return json.loads(self.grounding_used_raw)
        except Exception:
            return []

    @grounding_used.setter
    def grounding_used(self, facts: List[str]) -> None:
        self.grounding_used_raw = json.dumps(facts or [])

    @property
    def active_text(self) -> str:
        return self.edited_text if self.edited_text is not None else self.text


class ExtractionResult(BaseModel):
    author_name: str
    author_headline_or_company: str = ""
    post_body: str


class TriageResult(BaseModel):
    relevance_score: int = PydanticField(ge=1, le=10)
    opportunity_score: int = PydanticField(ge=1, le=10)
    topic_tags: List[str] = PydanticField(default_factory=list)
    one_line_reason: str


class SingleDraft(BaseModel):
    angle: DraftAngle
    text: str
    grounding_used: List[str] = PydanticField(default_factory=list)


class DraftingResult(BaseModel):
    drafts: List[SingleDraft]


class DMDraftResult(BaseModel):
    dm_text: str
    grounding_used: List[str] = PydanticField(default_factory=list)


class CaptureRequest(BaseModel):
    selected_text: str
    url: str
    page_title: Optional[str] = ""


class CaptureResponse(BaseModel):
    status: str
    post_id: Optional[int] = None
    message: Optional[str] = None


class CountResponse(BaseModel):
    new: int
