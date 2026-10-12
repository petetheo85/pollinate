"""Database engine initialization, CRUD operations, lifecycle state management, and duplicate warning detection."""

import hashlib
import re
import string
from typing import List, Optional
import unicodedata

from sqlalchemy import event
from sqlalchemy.engine import Engine, URL
from sqlmodel import SQLModel, Session, create_engine, select

import config
from config import (
    MAX_PRIOR_COMMENTS_IN_PROMPT,
    OUTREACH_MIN_COMMENTS,
    OUTREACH_MIN_OPPORTUNITY_SCORE,
    SQLITE_BUSY_TIMEOUT_MS,
)
from models import (
    Author,
    Post,
    PostStatus,
    utc_now,
)


class PollinateDBError(Exception):
    pass


class InvalidStatusTransitionError(PollinateDBError):
    pass


_ENGINE: Optional[Engine] = None
_CURRENT_DB_PATH: Optional[str] = None


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
    cursor = dbapi_connection.cursor()
    # WAL enables concurrent readers while writing; busy_timeout waits for locks instead of failing immediately
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def get_engine(db_path: Optional[str] = None) -> Engine:
    """Returns or creates the shared SQLModel engine using the configured DB_PATH."""
    global _ENGINE, _CURRENT_DB_PATH
    if db_path is not None:
        if str(db_path).startswith("sqlite"):
            url = str(db_path)
        else:
            # URL.create generates cross-platform file paths on POSIX and Windows
            url = URL.create("sqlite", database=str(db_path))
        return create_engine(url, echo=False)

    target_path = str(config.DB_PATH)
    if _ENGINE is None or _CURRENT_DB_PATH != target_path:
        url = URL.create("sqlite", database=target_path)
        _ENGINE = create_engine(url, echo=False)
        _CURRENT_DB_PATH = target_path
    return _ENGINE


def init_db(engine: Optional[Engine] = None) -> Engine:
    """Creates all database tables on the specified or default engine."""
    active_engine = engine or get_engine()
    SQLModel.metadata.create_all(active_engine)
    return active_engine


def get_session(engine: Optional[Engine] = None) -> Session:
    return Session(engine or get_engine())




_ACTIVITY_ID_PATTERN = re.compile(
    r"(?:urn:li:activity:(\d+)|(?:-activity-|/activity/)(\d+)|update/urn:li:activity:(\d+))",
    re.IGNORECASE,
)

_DEGREE_MARKERS_PATTERN = re.compile(
    r"(\s*[·•]\s*(?:1st|2nd|3rd|\d+\+?|Following)|(\s*\((?:he|she|they)/[^\)]+\)))",
    re.IGNORECASE,
)

_PREFIX_TITLES_PATTERN = re.compile(
    r"^(?:dr|prof|mr|mrs|ms)\.?\s+",
    re.IGNORECASE,
)


def extract_linkedin_activity_id(url: str) -> Optional[str]:
    if not url:
        return None
    match = _ACTIVITY_ID_PATTERN.search(url)
    if not match:
        return None
    for group in match.groups():
        if group:
            return group
    return None


def normalize_author_name(name: str) -> str:
    """Strips titles, connection degree markers, pronouns, and emojis to produce a canonical name."""
    if not name:
        return "unknown"

    # Remove emoji and symbols
    no_emoji = "".join(
        c for c in name if not (unicodedata.category(c).startswith("So") or ord(c) > 0x1F000)
    )
    # Strip LinkedIn degree suffixes (e.g. ' · 1st') and pronouns
    clean = _DEGREE_MARKERS_PATTERN.sub("", no_emoji)
    # Strip common prefix honorifics
    clean = _PREFIX_TITLES_PATTERN.sub("", clean.strip())

    collapsed = " ".join(clean.split())
    return collapsed if collapsed else "unknown"


def generate_author_key(name: str) -> str:
    return normalize_author_name(name).casefold()


def normalize_body_for_hash(body: str) -> str:
    """Casefolds, removes punctuation, and collapses whitespace for duplicate post detection."""
    if not body:
        return ""
    casefolded = body.casefold()
    no_punct = "".join(c for c in casefolded if c not in string.punctuation)
    return " ".join(no_punct.split())


def compute_body_hash(body: str) -> str:
    normalized = normalize_body_for_hash(body)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_prior_posted_post(
    session: Session,
    body_hash: Optional[str] = None,
    url: Optional[str] = None,
) -> Optional[Post]:
    """Finds an earlier posted post by matching normalized body hash or activity ID."""
    # Match by body hash
    if body_hash:
        statement = select(Post).where(
            Post.post_body_hash == body_hash,
            Post.status == PostStatus.POSTED.value,
        )
        match = session.exec(statement).first()
        if match:
            return match

    # Match by LinkedIn activity ID if present in URL
    if url:
        activity_id = extract_linkedin_activity_id(url)
        if activity_id:
            posted_posts = session.exec(
                select(Post).where(Post.status == PostStatus.POSTED.value)
            ).all()
            for post in posted_posts:
                if extract_linkedin_activity_id(post.url) == activity_id:
                    return post

    return None




def get_or_create_author(
    session: Session,
    name: str,
    headline_or_company: Optional[str] = "",
) -> Author:
    """Finds an existing author by normalized key or creates a new record."""
    clean_name = normalize_author_name(name)
    key = generate_author_key(name)
    statement = select(Author).where(Author.key == key)
    author = session.exec(statement).first()

    if author is not None:
        if headline_or_company and not author.headline_or_company:
            author.headline_or_company = headline_or_company.strip()
            author.updated_at = utc_now()
            session.add(author)
            session.commit()
            session.refresh(author)
        return author

    author = Author(
        name=clean_name,
        key=key,
        headline_or_company=headline_or_company.strip() if headline_or_company else "",
        comments_posted=0,
        notes="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(author)
    session.commit()
    session.refresh(author)
    return author


def get_recent_author_comments(
    session: Session,
    author_id: int,
    limit: int = MAX_PRIOR_COMMENTS_IN_PROMPT,
) -> List[str]:
    """Retrieves up to limit of the user's previously posted comments to this author."""
    statement = (
        select(Post.final_comment)
        .where(
            Post.author_id == author_id,
            Post.status == PostStatus.POSTED.value,
            Post.final_comment.isnot(None),
        )
        .order_by(Post.posted_at.desc())
        .limit(limit)
    )
    results = session.exec(statement).all()
    return [c for c in results if c and c.strip()]


def merge_authors(
    session: Session,
    primary_author_id: int,
    secondary_author_id: int,
) -> Author:
    """Consolidates posts, comment tallies, and notes from secondary author into primary author."""
    primary = session.get(Author, primary_author_id)
    if not primary:
        raise ValueError(f"Primary author {primary_author_id} not found.")

    if primary_author_id == secondary_author_id:
        return primary

    secondary = session.get(Author, secondary_author_id)
    if not secondary:
        raise ValueError(f"Secondary author {secondary_author_id} not found.")

    secondary_posts = session.exec(
        select(Post).where(Post.author_id == secondary.id)
    ).all()
    for post in secondary_posts:
        post.author_id = primary.id
        post.author = primary
        post.updated_at = utc_now()
        session.add(post)
    session.flush()

    primary.comments_posted += secondary.comments_posted

    interactions = [d for d in (primary.last_interaction_at, secondary.last_interaction_at) if d is not None]
    if interactions:
        primary.last_interaction_at = max(interactions)

    notes = []
    if primary.notes and primary.notes.strip():
        notes.append(primary.notes.strip())
    if secondary.notes and secondary.notes.strip():
        notes.append(f"[Merged from {secondary.name}]:\n{secondary.notes.strip()}")
    primary.notes = "\n\n---\n\n".join(notes)
    primary.updated_at = utc_now()

    # Clearing relationship collection prevents SQLAlchemy from nullifying foreign keys during author deletion
    secondary.posts = []
    session.delete(secondary)
    session.add(primary)
    session.commit()
    session.refresh(primary)
    return primary


def is_outreach_candidate(
    author: Author,
    max_opportunity_score: Optional[int] = None,
) -> bool:
    if author.outreach_override is not None:
        return author.outreach_override
    if author.comments_posted >= OUTREACH_MIN_COMMENTS and (max_opportunity_score or 0) >= OUTREACH_MIN_OPPORTUNITY_SCORE:
        return True
    return False




VALID_TRANSITIONS = {
    PostStatus.CAPTURED.value: {
        PostStatus.DRAFTED.value,
        PostStatus.SKIPPED.value,
    },
    PostStatus.DRAFTED.value: {
        PostStatus.POSTED.value,
        PostStatus.SKIPPED.value,
        PostStatus.DRAFTED.value,  # regeneration
    },
    PostStatus.SKIPPED.value: {
        PostStatus.CAPTURED.value,
        PostStatus.DRAFTED.value,
    },
    PostStatus.POSTED.value: set(),  # terminal
}


def transition_post_status(
    session: Session,
    post_id: int,
    target_status: PostStatus,
    final_comment: Optional[str] = None,
) -> Post:
    """Enforces strict status transitions (captured -> drafted -> posted/skipped)."""
    post = session.get(Post, post_id)
    if not post:
        raise PollinateDBError(f"Post with id {post_id} not found.")

    current = post.status
    target = target_status.value if isinstance(target_status, PostStatus) else target_status

    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStatusTransitionError(
            f"Invalid transition from '{current}' to '{target}'. "
            f"Allowed next states: {sorted(list(allowed))}"
        )

    if target == PostStatus.POSTED.value:
        if not final_comment or not final_comment.strip():
            raise ValueError("final_comment is required and cannot be empty when posting.")

    now = utc_now()
    post.status = target
    post.updated_at = now

    if target == PostStatus.POSTED.value:
        post.posted_at = now
        post.final_comment = final_comment.strip()

        if post.author_id:
            author = session.get(Author, post.author_id)
            # Anonymous captures must not accumulate interaction tallies or contaminate repeat author tracking
            if author and author.key != "unknown":
                author.comments_posted += 1
                author.last_interaction_at = now
                author.updated_at = now
                session.add(author)

    elif target == PostStatus.DRAFTED.value:
        pass

    session.add(post)
    session.commit()
    session.refresh(post)
    return post
