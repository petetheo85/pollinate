"""Tests for models, database operations, deduplication, and lifecycle transitions."""

from datetime import datetime, timezone
import pytest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from config import SQLITE_BUSY_TIMEOUT_MS
from db import (
    DuplicatePostError,
    InvalidStatusTransitionError,
    compute_dedupe_info,
    create_post,
    extract_linkedin_activity_id,
    get_or_create_author,
    is_outreach_candidate,
    merge_authors,
    transition_post_status,
    update_post_permalink,
)
from models import Author, Post, PostStatus


@pytest.fixture
def test_db_session(tmp_path):
    db_file = tmp_path / "test_pollinate.db"
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_sqlite_pragmas(test_db_session):
    journal_mode = test_db_session.exec(text("PRAGMA journal_mode;")).one()[0]
    busy_timeout = test_db_session.exec(text("PRAGMA busy_timeout;")).one()[0]
    foreign_keys = test_db_session.exec(text("PRAGMA foreign_keys;")).one()[0]

    assert journal_mode.lower() == "wal"
    assert int(busy_timeout) == SQLITE_BUSY_TIMEOUT_MS
    assert int(foreign_keys) == 1


def test_extract_activity_id_and_canonicalization():
    url1 = "https://www.linkedin.com/feed/update/urn:li:activity:7234567890123456789/"
    url2 = "https://www.linkedin.com/posts/alice-smith_ai-activity-7234567890123456789-xyzq?utm_source=share"
    url3 = "https://www.linkedin.com/feed/"

    assert extract_linkedin_activity_id(url1) == "7234567890123456789"
    assert extract_linkedin_activity_id(url2) == "7234567890123456789"
    assert extract_linkedin_activity_id(url3) is None

    dedupe_key, norm_url, needs_perm = compute_dedupe_info(url2, "Some text")
    assert norm_url == "https://www.linkedin.com/feed/update/urn:li:activity:7234567890123456789/"
    assert dedupe_key == norm_url
    assert needs_perm is False

    feed_key, feed_norm, feed_needs_perm = compute_dedupe_info(url3, "Here is a great insight on AI.")
    assert feed_key.startswith("hash:")
    assert feed_norm is None
    assert feed_needs_perm is True


def test_feed_clip_deduplication(test_db_session):
    raw_text = "Exciting breakthrough in reasoning models today!\nCheck it out."
    feed_url = "https://www.linkedin.com/feed/"

    post1, created1 = create_post(test_db_session, feed_url, raw_text, page_title="LinkedIn Feed")
    assert created1 is True
    assert post1.needs_permalink is True
    assert post1.dedupe_key.startswith("hash:")

    post2, created2 = create_post(test_db_session, feed_url, "  Exciting breakthrough in reasoning models today!  \n Check it out. ")
    assert created2 is False
    assert post2.id == post1.id

    diff_text = "A different discussion about AI agents architecture."
    post3, created3 = create_post(test_db_session, feed_url, diff_text, page_title="LinkedIn Feed")
    assert created3 is True
    assert post3.id != post1.id


def test_update_permalink_and_conflict_check(test_db_session):
    feed_url = "https://www.linkedin.com/feed/"
    post, _ = create_post(test_db_session, feed_url, "Testing permalink resolution.")
    assert post.needs_permalink is True

    with pytest.raises(ValueError, match="not a valid LinkedIn post permalink"):
        update_post_permalink(test_db_session, post.id, "https://www.linkedin.com/in/someone/")

    real_url = "https://www.linkedin.com/posts/johndoe_ai-activity-7111222333444555666-abcd"
    updated = update_post_permalink(test_db_session, post.id, real_url)
    assert updated.needs_permalink is False
    assert updated.dedupe_key == "https://www.linkedin.com/feed/update/urn:li:activity:7111222333444555666/"
    assert updated.normalized_url == updated.dedupe_key

    post_other, _ = create_post(test_db_session, feed_url, "Another post text.")
    with pytest.raises(DuplicatePostError):
        update_post_permalink(test_db_session, post_other.id, real_url)


def test_status_lifecycle_and_side_effects(test_db_session):
    author = get_or_create_author(test_db_session, "Alice Developer", "AI Engineer")
    post, _ = create_post(
        test_db_session,
        "https://www.linkedin.com/feed/update/urn:li:activity:999988887777/",
        "Post content",
    )
    post.author_id = author.id
    test_db_session.add(post)
    test_db_session.commit()

    assert post.status == PostStatus.NEW.value

    with pytest.raises(InvalidStatusTransitionError):
        transition_post_status(test_db_session, post.id, PostStatus.POSTED)
    with pytest.raises(InvalidStatusTransitionError):
        transition_post_status(test_db_session, post.id, PostStatus.APPROVED)

    post = transition_post_status(test_db_session, post.id, PostStatus.DRAFTED)
    assert post.status == PostStatus.DRAFTED.value

    comment_text = "My real world perspective on this..."
    post = transition_post_status(
        test_db_session,
        post.id,
        PostStatus.APPROVED,
        final_comment=comment_text,
    )
    assert post.status == PostStatus.APPROVED.value
    assert post.final_comment == comment_text
    assert post.approved_at is not None

    post = transition_post_status(test_db_session, post.id, PostStatus.DRAFTED)
    assert post.status == PostStatus.DRAFTED.value
    assert post.approved_at is None

    post = transition_post_status(test_db_session, post.id, PostStatus.APPROVED, final_comment=comment_text)

    assert author.comments_posted == 0
    post = transition_post_status(test_db_session, post.id, PostStatus.POSTED)
    assert post.status == PostStatus.POSTED.value
    assert post.archived is True
    assert post.posted_at is not None

    test_db_session.refresh(author)
    assert author.comments_posted == 1
    assert author.last_interaction_at == post.posted_at

    with pytest.raises(InvalidStatusTransitionError):
        transition_post_status(test_db_session, post.id, PostStatus.DRAFTED)


def test_author_merging(test_db_session):
    author_primary = get_or_create_author(test_db_session, "Bob Builder", "Founder")
    author_primary.comments_posted = 1
    author_primary.last_interaction_at = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
    author_primary.notes = "Primary notes."
    test_db_session.add(author_primary)

    author_secondary = get_or_create_author(test_db_session, "Bob Builder AI", "CEO")
    author_secondary.comments_posted = 2
    author_secondary.last_interaction_at = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
    author_secondary.notes = "Secondary notes from duplicate."
    test_db_session.add(author_secondary)
    test_db_session.commit()

    post1, _ = create_post(test_db_session, "https://www.linkedin.com/feed/update/urn:li:activity:1001/", "Post 1")
    post1.author_id = author_primary.id
    post2, _ = create_post(test_db_session, "https://www.linkedin.com/feed/update/urn:li:activity:1002/", "Post 2")
    post2.author_id = author_secondary.id
    test_db_session.add(post1)
    test_db_session.add(post2)
    test_db_session.commit()

    merged = merge_authors(test_db_session, author_primary.id, author_secondary.id)

    assert merged.id == author_primary.id
    assert merged.comments_posted == 3
    assert merged.last_interaction_at == datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert "Primary notes." in merged.notes
    assert "Secondary notes from duplicate." in merged.notes

    test_db_session.refresh(post2)
    assert post2.author_id == author_primary.id
    assert test_db_session.get(Author, author_secondary.id) is None


def test_effective_opportunity_score_and_outreach_candidate(test_db_session):
    author = get_or_create_author(test_db_session, "Carol Danvers", "Staff Engineer")
    author.comments_posted = 2

    assert is_outreach_candidate(author, max_opportunity_score=6) is False
    assert is_outreach_candidate(author, max_opportunity_score=8) is True

    author.outreach_override = False
    assert is_outreach_candidate(author, max_opportunity_score=9) is False

    author.outreach_override = True
    assert is_outreach_candidate(author, max_opportunity_score=2) is True

    post = Post(
        url="https://example.com",
        dedupe_key="hash:123",
        raw_text="Hello",
        opportunity_score=5,
        opportunity_score_override=9,
    )
    assert post.effective_opportunity_score == 9
