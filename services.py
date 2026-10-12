"""Orchestration services for inline LinkedIn post extraction, scoring, and drafting."""

from datetime import datetime, timezone
import logging
import time
from typing import Optional

from sqlmodel import Session, delete

from config import (
    MAX_PRIOR_COMMENTS_IN_PROMPT,
    PROFILE_PATH,
    TOTAL_DEADLINE_SECONDS,
)
from db import (
    compute_body_hash,
    find_prior_posted_post,
    get_or_create_author,
    get_recent_author_comments,
    normalize_author_name,
    transition_post_status,
)
from llm import LLMError, analyze_and_draft_post, generate_structured
from models import (
    AlreadyCommentedInfo,
    Author,
    DMDraftResult,
    Draft,
    DraftResponse,
    Post,
    PostStatus,
    PriorInteractionInfo,
    utc_now,
)
from prompts import DM_DRAFT_PROMPT, format_inline_prompt

logger = logging.getLogger(__name__)


def load_profile() -> str:
    """Reads profile background markdown from PROFILE_PATH if present."""
    if PROFILE_PATH.exists():
        try:
            return PROFILE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read profile from %s: %s", PROFILE_PATH, exc)
    return ""


def save_profile(content: str) -> None:
    """Writes profile markdown to PROFILE_PATH, creating parent directories if needed."""
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(content, encoding="utf-8")


def format_local_datetime(dt: Optional[datetime]) -> str:
    """Formats a UTC datetime into the user's local timezone for dashboard display."""
    if not dt:
        return "N/A"
    # Convert naive or aware UTC datetimes into system local time
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%b %d, %Y %H:%M")


def generate_inline_drafts(
    post_id: int,
    session: Session,
    guidance: Optional[str] = None,
) -> DraftResponse:
    """Extracts post metadata, scores opportunity, and generates 3 grounded comment drafts."""
    post = session.get(Post, post_id)
    if not post:
        raise ValueError(f"Post {post_id} not found.")

    start_time = time.monotonic()
    profile = load_profile()

    try:
        # First call: extract metadata, summarize points, and draft based on profile
        prompt = format_inline_prompt(
            selection=post.raw_text,
            url=post.url,
            page_title=post.page_title or "",
            profile=profile,
            prior_comments=None,
            guidance=guidance,
        )
        result = analyze_and_draft_post(prompt)

        # Normalize and resolve author record
        author = get_or_create_author(
            session=session,
            name=result.author_name,
            headline_or_company=result.author_headline_or_company,
        )

        post.author_id = author.id
        post.post_body = result.post_body
        post.post_body_hash = compute_body_hash(result.post_body)
        post.relevance_score = result.relevance_score
        post.opportunity_score = result.opportunity_score
        post.one_line_reason = result.one_line_reason
        post.topic_tags = result.topic_tags
        post.existing_comment_points = result.existing_comment_points

        # Warning only: check if post body or activity ID was previously commented on
        duplicate_warning: Optional[AlreadyCommentedInfo] = None
        prior_post = find_prior_posted_post(
            session=session,
            body_hash=post.post_body_hash,
            url=post.url,
        )
        if prior_post and prior_post.id != post.id and prior_post.posted_at and prior_post.final_comment:
            duplicate_warning = AlreadyCommentedInfo(
                posted_at=prior_post.posted_at,
                final_comment=prior_post.final_comment,
            )

        prior_interaction: Optional[PriorInteractionInfo] = None
        if author.key != "unknown" and author.comments_posted > 0:
            prior_interaction = PriorInteractionInfo(
                comments_posted=author.comments_posted,
                last_interaction_at=author.last_interaction_at,
            )

            # Repeat author: regenerate drafts including previous comments if under total deadline
            elapsed = time.monotonic() - start_time
            if elapsed < TOTAL_DEADLINE_SECONDS:
                priors = get_recent_author_comments(
                    session=session,
                    author_id=author.id,
                    limit=MAX_PRIOR_COMMENTS_IN_PROMPT,
                )
                if priors:
                    repeat_prompt = format_inline_prompt(
                        selection=post.raw_text,
                        url=post.url,
                        page_title=post.page_title or "",
                        profile=profile,
                        prior_comments=priors,
                        guidance=guidance,
                    )
                    result = analyze_and_draft_post(repeat_prompt)

        # Replace existing drafts if regenerating
        session.exec(delete(Draft).where(Draft.post_id == post.id))

        for draft_model in result.drafts:
            angle_str = draft_model.angle.value if hasattr(draft_model.angle, "value") else str(draft_model.angle)
            db_draft = Draft(
                post_id=post.id,
                angle=angle_str,
                text=draft_model.text,
                adds_beyond_existing=draft_model.adds_beyond_existing,
                grounding_used=draft_model.grounding_used,
            )
            session.add(db_draft)

        transition_post_status(session, post.id, PostStatus.DRAFTED)
        post.error_message = None
        session.commit()
        session.refresh(post)

        return DraftResponse(
            post_id=post.id,
            author_name=author.name,
            author_headline=author.headline_or_company,
            post_body=post.post_body,
            existing_comment_points=post.existing_comment_points,
            relevance_score=post.relevance_score,
            opportunity_score=post.opportunity_score,
            one_line_reason=post.one_line_reason,
            topic_tags=post.topic_tags,
            drafts=result.drafts,
            duplicate_warning=duplicate_warning,
            prior_interaction=prior_interaction,
        )

    except LLMError as exc:
        # LLM failure preserves raw capture in captured state with error message
        post.error_message = str(exc)
        session.add(post)
        session.commit()
        raise


def generate_author_dm_draft(author_id: int, session: Session) -> str:
    """Generates a peer-to-peer LinkedIn direct message based on prior interaction history."""
    author = session.get(Author, author_id)
    if not author:
        raise ValueError(f"Author {author_id} not found.")

    profile = load_profile()
    priors = get_recent_author_comments(session, author.id, limit=MAX_PRIOR_COMMENTS_IN_PROMPT)
    prior_bullets = "\n".join(f"- {c}" for c in priors) if priors else "No prior comments recorded."

    prompt = DM_DRAFT_PROMPT.format(
        profile=profile.strip() if profile else "No profile provided.",
        name=author.name,
        headline=author.headline_or_company or "N/A",
        notes=author.notes or "None",
        prior_comments=prior_bullets,
    )

    result = generate_structured(prompt, DMDraftResult)
    author.last_dm_draft = result.dm_text
    author.updated_at = utc_now()
    session.add(author)
    session.commit()
    session.refresh(author)
    return result.dm_text
