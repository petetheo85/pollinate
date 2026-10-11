"""Database models and Pydantic schemas for the inline capture and drafting flow."""

from datetime import datetime, timezone
import json
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field as PydanticField, field_validator
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PostStatus(str, Enum):
    CAPTURED = "captured"
    DRAFTED = "drafted"
    POSTED = "posted"
    SKIPPED = "skipped"


class DraftAngle(str, Enum):
    EXPERIENCE = "experience"
    QUESTION = "question"
    COUNTERPOINT = "counterpoint"


class Author(SQLModel, table=True):
    __tablename__ = "author"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    # Lowercased normalized name provides consistent matching across captures
    key: str = Field(unique=True, index=True)
    headline_or_company: Optional[str] = Field(default="")
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
    # Stored for reference and duplicate activity ID detection, non-unique
    url: str
    # Hash of normalized post body used to warn if previously commented upon
    post_body_hash: Optional[str] = Field(default=None, index=True)
    page_title: Optional[str] = Field(default="")
    raw_text: str
    post_body: Optional[str] = Field(default=None)
    existing_comment_points_raw: Optional[str] = Field(default="[]")
    status: str = Field(default=PostStatus.CAPTURED.value, index=True)

    relevance_score: Optional[int] = Field(default=None)
    opportunity_score: Optional[int] = Field(default=None)
    topic_tags_raw: Optional[str] = Field(default="[]")
    one_line_reason: Optional[str] = Field(default=None)

    final_comment: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)

    captured_at: datetime = Field(default_factory=utc_now)
    triaged_at: Optional[datetime] = Field(default=None)
    posted_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=utc_now)

    author: Optional[Author] = Relationship(back_populates="posts")
    drafts: List["Draft"] = Relationship(back_populates="post")

    @property
    def existing_comment_points(self) -> List[str]:
        if not self.existing_comment_points_raw:
            return []
        try:
            return json.loads(self.existing_comment_points_raw)
        except Exception:
            return []

    @existing_comment_points.setter
    def existing_comment_points(self, points: List[str]) -> None:
        self.existing_comment_points_raw = json.dumps(points or [])

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
    # Highlights the novel contribution relative to existing thread discussion
    adds_beyond_existing: str = Field(default="")
    grounding_used_raw: Optional[str] = Field(default="[]")
    is_ungrounded_placeholder: bool = Field(default=False)
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


class SingleDraft(BaseModel):
    angle: DraftAngle
    text: str
    grounding_used: List[str] = PydanticField(default_factory=list)
    adds_beyond_existing: str


class InlineAnalysisResult(BaseModel):
    """Unified result from the single LLM inference call."""
    author_name: str
    author_headline_or_company: str
    post_body: str
    existing_comment_points: List[str] = PydanticField(default_factory=list)
    relevance_score: int = PydanticField(ge=1, le=10)
    opportunity_score: int = PydanticField(ge=1, le=10)
    topic_tags: List[str] = PydanticField(default_factory=list)
    one_line_reason: str
    drafts: List[SingleDraft]

    @field_validator("drafts")
    @classmethod
    def validate_draft_angles(cls, drafts: List[SingleDraft]) -> List[SingleDraft]:
        if len(drafts) != 3:
            raise ValueError(f"Expected exactly 3 drafts, got {len(drafts)}")
        angles = {d.angle for d in drafts}
        required_angles = {DraftAngle.EXPERIENCE, DraftAngle.QUESTION, DraftAngle.COUNTERPOINT}
        if angles != required_angles:
            raise ValueError(
                f"Drafts must have distinct angles covering experience, question, and counterpoint; got {angles}"
            )
        return drafts


class PriorInteractionInfo(BaseModel):
    comments_posted: int
    last_interaction_at: Optional[datetime] = None


class AlreadyCommentedInfo(BaseModel):
    posted_at: datetime
    final_comment: str


class DraftRequest(BaseModel):
    selected_text: str
    url: str
    page_title: Optional[str] = ""


class PostPostedRequest(BaseModel):
    final_comment: str


class DraftResponse(BaseModel):
    post_id: int
    author_name: str
    author_headline: str = ""
    post_body: str
    existing_comment_points: List[str] = PydanticField(default_factory=list)
    relevance_score: int
    opportunity_score: int
    one_line_reason: str
    topic_tags: List[str] = PydanticField(default_factory=list)
    drafts: List[SingleDraft]
    duplicate_warning: Optional[AlreadyCommentedInfo] = None
    prior_interaction: Optional[PriorInteractionInfo] = None
