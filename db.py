"""Database engine initialization, CRUD operations, deduplication, and lifecycle state management."""

import hashlib
import re
from typing import Optional, Tuple
from sqlalchemy import event, func
from sqlalchemy.engine import Engine, URL
from sqlmodel import SQLModel, Session, create_engine, select

from config import (
    DB_PATH,
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


class DuplicatePostError(PollinateDBError):
    pass


class InvalidStatusTransitionError(PollinateDBError):
    pass


_ENGINE: Optional[Engine] = None


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
    cursor = dbapi_connection.cursor()
    # WAL allows readers while writing; busy_timeout mitigates concurrency locks between web and capture processes
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def get_engine(db_path: Optional[str] = None) -> Engine:
    """Returns or creates the shared SQLModel engine using the configured DB_PATH."""
    global _ENGINE
    if db_path is not None:
        if str(db_path).startswith("sqlite"):
            url = str(db_path)
        else:
            # URL.create generates cross-platform file paths on POSIX and Windows
            url = URL.create("sqlite", database=str(db_path))
        return create_engine(url, echo=False)

    if _ENGINE is None:
        url = URL.create("sqlite", database=str(DB_PATH))
        _ENGINE = create_engine(url, echo=False)
    return _ENGINE


def init_db(engine: Optional[Engine] = None) -> Engine:
    """Creates all database tables on the specified or default engine."""
    active_engine = engine or get_engine()
    SQLModel.metadata.create_all(active_engine)
    return active_engine


def get_session(engine: Optional[Engine] = None) -> Session:
    return Session(engine or get_engine())


# Handles urn, hyphenated post slugs, and update URL variants containing LinkedIn activity IDs
_ACTIVITY_ID_PATTERN = re.compile(
    r"(?:urn:li:activity:(\d+)|(?:-activity-|/activity/)(\d+)|update/urn:li:activity:(\d+))",
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


def normalize_post_text(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.strip().split())


def compute_dedupe_info(url: str, raw_text: str) -> Tuple[str, Optional[str], bool]:
    """Returns canonical permalink if activity ID present; otherwise returns raw text hash."""
    activity_id = extract_linkedin_activity_id(url)
    if activity_id:
        canonical_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
        return canonical_url, canonical_url, False

    # Feed clips share a generic feed URL; hashing normalized text prevents duplicate captures of the same post
    clean_text = normalize_post_text(raw_text)
    text_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
    dedupe_key = f"hash:{text_hash}"
    return dedupe_key, None, True


def normalize_author_name(name: str) -> str:
    return " ".join(name.strip().lower().split()) if name else "unknown"


def generate_author_key(name: str, profile_url: Optional[str] = None) -> str:
    norm_name = normalize_author_name(name)
    if profile_url and profile_url.strip():
        norm_profile = profile_url.strip().lower().rstrip("/")
        return f"{norm_name}|{norm_profile}"
    return norm_name


def get_or_create_author(
    session: Session,
    name: str,
    headline_or_company: Optional[str] = "",
    profile_url: Optional[str] = None,
) -> Author:
    """Finds an existing author by normalized key or creates a new author record."""
    key = generate_author_key(name, profile_url)
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

    clean_name = name.strip() if name and name.strip() else "Unknown"
    author = Author(
        name=clean_name,
        key=key,
        headline_or_company=headline_or_company.strip() if headline_or_company else "",
        profile_url=profile_url.strip() if profile_url else None,
        comments_posted=0,
        notes="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(author)
    session.commit()
    session.refresh(author)
    return author


def merge_authors(
    session: Session,
    primary_author_id: int,
    secondary_author_id: int,
) -> Author:
    """Merges secondary author into primary, consolidating posts, comment counts, and notes."""
    if primary_author_id == secondary_author_id:
        raise ValueError("Cannot merge an author into themselves.")

    primary = session.get(Author, primary_author_id)
    secondary = session.get(Author, secondary_author_id)

    if not primary or not secondary:
        raise ValueError("Both primary and secondary authors must exist.")

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


VALID_TRANSITIONS = {
    PostStatus.NEW.value: {
        PostStatus.DRAFTED.value,
        PostStatus.SKIPPED.value,
        PostStatus.REJECTED.value,
    },
    PostStatus.DRAFTED.value: {
        PostStatus.APPROVED.value,
        PostStatus.SKIPPED.value,
        PostStatus.REJECTED.value,
    },
    PostStatus.SKIPPED.value: {
        PostStatus.NEW.value,
        PostStatus.DRAFTED.value,
    },
    PostStatus.APPROVED.value: {
        PostStatus.POSTED.value,
        PostStatus.DRAFTED.value,
    },
    PostStatus.POSTED.value: set(),
    PostStatus.REJECTED.value: {
        PostStatus.NEW.value,
        PostStatus.DRAFTED.value,
    },
}


def transition_post_status(
    session: Session,
    post_id: int,
    target_status: PostStatus,
    final_comment: Optional[str] = None,
    approved_draft_id: Optional[int] = None,
) -> Post:
    """Enforces valid lifecycle transitions and records timestamps and author metrics."""
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

    now = utc_now()
    post.status = target
    post.updated_at = now

    if target == PostStatus.APPROVED.value:
        post.approved_at = now
        if final_comment is not None:
            post.final_comment = final_comment
        if approved_draft_id is not None:
            post.approved_draft_id = approved_draft_id

    elif target == PostStatus.POSTED.value:
        post.posted_at = now
        post.archived = True
        if final_comment is not None:
            post.final_comment = final_comment

        if post.author_id:
            author = session.get(Author, post.author_id)
            if author:
                author.comments_posted += 1
                author.last_interaction_at = now
                author.updated_at = now
                session.add(author)

    elif target == PostStatus.DRAFTED.value:
        if current == PostStatus.APPROVED.value:
            post.approved_at = None

    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def create_post(
    session: Session,
    url: str,
    raw_text: str,
    page_title: str = "",
    source: str = "extension",
) -> Tuple[Post, bool]:
    """Captures a post, returning (post, True) on creation or (existing_post, False) on duplicate."""
    dedupe_key, normalized_url, needs_permalink = compute_dedupe_info(url, raw_text)

    existing = session.exec(select(Post).where(Post.dedupe_key == dedupe_key)).first()
    if existing:
        return existing, False

    new_post = Post(
        url=url,
        normalized_url=normalized_url,
        dedupe_key=dedupe_key,
        needs_permalink=needs_permalink,
        page_title=page_title or "",
        raw_text=raw_text,
        post_body=None,
        needs_manual_cleanup=False,
        status=PostStatus.NEW.value,
        source=source,
        archived=False,
        captured_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(new_post)
    session.commit()
    session.refresh(new_post)
    return new_post, True


def update_post_permalink(
    session: Session,
    post_id: int,
    new_url: str,
) -> Post:
    """Updates a post with its permalink URL and recomputes the dedupe key, raising on conflicts."""
    post = session.get(Post, post_id)
    if not post:
        raise PollinateDBError(f"Post with id {post_id} not found.")

    activity_id = extract_linkedin_activity_id(new_url)
    if not activity_id:
        raise ValueError(
            "Provided URL is not a valid LinkedIn post permalink. "
            "It must contain an activity ID (e.g. urn:li:activity:12345 or -activity-12345)."
        )

    canonical_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"

    existing = session.exec(
        select(Post).where(Post.dedupe_key == canonical_url, Post.id != post_id)
    ).first()
    if existing:
        raise DuplicatePostError(
            f"Another post (ID {existing.id}) already exists with this permalink."
        )

    post.url = new_url
    post.normalized_url = canonical_url
    post.dedupe_key = canonical_url
    post.needs_permalink = False
    post.updated_at = utc_now()

    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def count_new_posts(session: Session) -> int:
    statement = select(func.count(Post.id)).where(
        Post.status == PostStatus.NEW.value,
        Post.archived == False,  # noqa: E712
    )
    return int(session.exec(statement).one())


def is_outreach_candidate(
    author: Author,
    max_opportunity_score: Optional[int] = None,
) -> bool:
    if author.outreach_override is not None:
        return author.outreach_override
    if author.comments_posted >= OUTREACH_MIN_COMMENTS and (max_opportunity_score or 0) >= OUTREACH_MIN_OPPORTUNITY_SCORE:
        return True
    return False
