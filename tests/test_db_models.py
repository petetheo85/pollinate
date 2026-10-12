"""Tests for revised models, database operations, state machine, and duplicate detection."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, select

import config
import db
from db import (
    InvalidStatusTransitionError,
    compute_body_hash,
    find_prior_posted_post,
    generate_author_key,
    get_engine,
    get_or_create_author,
    get_recent_author_comments,
    is_outreach_candidate,
    merge_authors,
    normalize_author_name,
    normalize_body_for_hash,
    transition_post_status,
)
from models import Author, Draft, DraftAngle, InlineAnalysisResult, Post, PostStatus, SingleDraft, utc_now


@pytest.fixture
def test_db_session(tmp_path):
    db_file = tmp_path / "test_inline.db"
    engine = get_engine(db_path=str(db_file))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_sqlite_pragmas_on_multiple_connections(tmp_path, monkeypatch):
    test_timeout = 1234
    monkeypatch.setattr(config, "SQLITE_BUSY_TIMEOUT_MS", test_timeout)
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_MS", test_timeout)

    db_file = tmp_path / "pragma_test.db"
    engine = get_engine(db_path=str(db_file))

    # Test WAL once on the database file
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode;")).one()[0]
        assert journal_mode.lower() == "wal"

    # Hold two connections open simultaneously; assert busy_timeout and foreign_keys per connection
    with engine.connect() as conn1:
        with engine.connect() as conn2:
            for conn in [conn1, conn2]:
                busy_timeout = conn.execute(text("PRAGMA busy_timeout;")).one()[0]
                foreign_keys = conn.execute(text("PRAGMA foreign_keys;")).one()[0]

                assert int(busy_timeout) == test_timeout
                assert int(foreign_keys) == 1


def test_datetime_roundtrip_through_fresh_session(tmp_path):
    db_file = tmp_path / "dt_test.db"
    engine = get_engine(db_path=str(db_file))
    SQLModel.metadata.create_all(engine)

    saved_time = utc_now()
    with Session(engine) as session1:
        author = Author(name="Time Test", key="time test", created_at=saved_time, updated_at=saved_time)
        session1.add(author)
        session1.commit()
        author_id = author.id

    with Session(engine) as session2:
        loaded = session2.get(Author, author_id)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert abs((loaded.created_at - saved_time).total_seconds()) < 0.001


def test_author_name_normalization():
    assert normalize_author_name("Alice Smith · 1st") == "Alice Smith"
    assert normalize_author_name("Bob Jones • 2nd") == "Bob Jones"
    assert normalize_author_name("Carol Danvers (She/Her)") == "Carol Danvers"
    assert normalize_author_name("Dr. Dave Miller") == "Dave Miller"
    assert normalize_author_name("Prof. Eve \U0001F680 Stone · Following") == "Eve Stone"
    assert generate_author_key("  Dr. Alice Smith · 1st  ") == "alice smith"


def test_get_or_create_author_idempotence(test_db_session):
    author1 = get_or_create_author(test_db_session, "Alice Smith · 1st", "Engineer")
    author2 = get_or_create_author(test_db_session, "Alice Smith", "Engineer")
    assert author1.id == author2.id

    all_authors = test_db_session.exec(select(Author).where(Author.key == "alice smith")).all()
    assert len(all_authors) == 1


def test_hash_normalization_and_duplicate_warning(test_db_session):
    body_text = "Exciting breakthrough in reasoning models today!\nCheck it out."
    normalized = normalize_body_for_hash(body_text)
    assert normalized == "exciting breakthrough in reasoning models today check it out"

    hash_val = compute_body_hash(body_text)
    hash_variant = compute_body_hash("  exciting breakthrough in reasoning models today, check it out...  ")
    assert hash_val == hash_variant

    author = get_or_create_author(test_db_session, "Alice Smith")
    posted_post = Post(
        url="https://www.linkedin.com/posts/alice-activity-7111222333-abcd",
        raw_text="Selected raw text",
        post_body=body_text,
        post_body_hash=hash_val,
        status=PostStatus.POSTED.value,
        final_comment="My earlier posted comment.",
        author_id=author.id,
    )
    test_db_session.add(posted_post)
    test_db_session.commit()

    # Match by body hash
    match_by_hash = find_prior_posted_post(test_db_session, body_hash=hash_variant)
    assert match_by_hash is not None
    assert match_by_hash.id == posted_post.id
    assert match_by_hash.final_comment == "My earlier posted comment."

    # Match by URL activity ID
    match_by_url = find_prior_posted_post(
        test_db_session,
        url="https://www.linkedin.com/feed/update/urn:li:activity:7111222333/",
    )
    assert match_by_url is not None
    assert match_by_url.id == posted_post.id

    # Drafted posts must NOT match
    drafted_hash = compute_body_hash("Drafted content")
    drafted_post = Post(
        url="https://www.linkedin.com/posts/bob-activity-999999-xyz",
        raw_text="Drafted raw text",
        post_body="Drafted content",
        post_body_hash=drafted_hash,
        status=PostStatus.DRAFTED.value,
    )
    test_db_session.add(drafted_post)
    test_db_session.commit()

    assert find_prior_posted_post(test_db_session, body_hash=drafted_hash) is None
    assert find_prior_posted_post(test_db_session, url="https://www.linkedin.com/posts/bob-activity-999999-xyz") is None


def test_feed_url_without_activity_id_does_not_match(test_db_session):
    author = get_or_create_author(test_db_session, "Feed Author")
    feed_post_body = "Feed post text discussing system architecture."
    feed_hash = compute_body_hash(feed_post_body)
    feed_posted = Post(
        url="https://www.linkedin.com/feed/",
        raw_text="Feed selection",
        post_body=feed_post_body,
        post_body_hash=feed_hash,
        status=PostStatus.POSTED.value,
        final_comment="Feed comment",
        author_id=author.id,
    )
    test_db_session.add(feed_posted)
    test_db_session.commit()

    diff_feed_hash = compute_body_hash("Completely different post body from another creator.")
    assert find_prior_posted_post(test_db_session, body_hash=diff_feed_hash, url="https://www.linkedin.com/feed/") is None
    assert find_prior_posted_post(test_db_session, url="https://www.linkedin.com/feed/") is None


def test_find_prior_posted_post_null_and_empty_guards(test_db_session):
    null_hash_post = Post(
        url="https://example.com/no-hash",
        raw_text="Raw text",
        post_body_hash=None,
        status=PostStatus.POSTED.value,
        final_comment="Prior comment",
    )
    test_db_session.add(null_hash_post)
    test_db_session.commit()

    # Calling with no arguments must match nothing
    assert find_prior_posted_post(test_db_session) is None

    # Calling with body_hash=None must never match a row whose hash is NULL
    assert find_prior_posted_post(test_db_session, body_hash=None) is None
    assert find_prior_posted_post(test_db_session, body_hash=None, url=None) is None


@pytest.mark.parametrize(
    "start_state,target_state",
    [
        (PostStatus.CAPTURED, PostStatus.DRAFTED),
        (PostStatus.CAPTURED, PostStatus.SKIPPED),
        (PostStatus.DRAFTED, PostStatus.POSTED),
        (PostStatus.DRAFTED, PostStatus.SKIPPED),
        (PostStatus.DRAFTED, PostStatus.DRAFTED),
        (PostStatus.SKIPPED, PostStatus.CAPTURED),
        (PostStatus.SKIPPED, PostStatus.DRAFTED),
    ],
)
def test_valid_status_transitions(test_db_session, start_state, target_state):
    post = Post(url="https://example.com", raw_text="text", status=start_state.value)
    test_db_session.add(post)
    test_db_session.commit()

    comment_arg = "Final comment" if target_state == PostStatus.POSTED else None
    updated = transition_post_status(test_db_session, post.id, target_state, final_comment=comment_arg)
    assert updated.status == target_state.value


@pytest.mark.parametrize(
    "start_state,invalid_target",
    [
        (PostStatus.CAPTURED, PostStatus.CAPTURED),
        (PostStatus.CAPTURED, PostStatus.POSTED),
        (PostStatus.DRAFTED, PostStatus.CAPTURED),
        (PostStatus.POSTED, PostStatus.CAPTURED),
        (PostStatus.POSTED, PostStatus.DRAFTED),
        (PostStatus.POSTED, PostStatus.POSTED),
        (PostStatus.POSTED, PostStatus.SKIPPED),
        (PostStatus.SKIPPED, PostStatus.POSTED),
        (PostStatus.SKIPPED, PostStatus.SKIPPED),
    ],
)
def test_hardcoded_invalid_status_transitions(test_db_session, start_state, invalid_target):
    post = Post(url="https://example.com", raw_text="text", status=start_state.value)
    test_db_session.add(post)
    test_db_session.commit()

    with pytest.raises(InvalidStatusTransitionError):
        transition_post_status(test_db_session, post.id, invalid_target)


def test_posting_requires_non_empty_final_comment(test_db_session):
    post = Post(url="https://example.com", raw_text="text", status=PostStatus.DRAFTED.value)
    test_db_session.add(post)
    test_db_session.commit()

    with pytest.raises(ValueError, match="final_comment is required"):
        transition_post_status(test_db_session, post.id, PostStatus.POSTED, final_comment=None)

    with pytest.raises(ValueError, match="final_comment is required"):
        transition_post_status(test_db_session, post.id, PostStatus.POSTED, final_comment="   ")

    updated = transition_post_status(test_db_session, post.id, PostStatus.POSTED, final_comment="Valid final comment")
    assert updated.status == PostStatus.POSTED.value
    assert updated.final_comment == "Valid final comment"
    assert updated.posted_at is not None


def test_posting_increments_comments_posted_and_updates_last_interaction_at(test_db_session):
    author = get_or_create_author(test_db_session, "Increment Author")
    assert author.comments_posted == 0
    assert author.last_interaction_at is None

    post = Post(url="https://example.com/p", raw_text="raw", status=PostStatus.DRAFTED.value, author_id=author.id)
    test_db_session.add(post)
    test_db_session.commit()

    transition_post_status(test_db_session, post.id, PostStatus.POSTED, final_comment="First comment")
    test_db_session.refresh(author)

    assert author.comments_posted == 1
    assert author.last_interaction_at is not None


def test_posting_nameless_author_does_not_increment_comments_posted(test_db_session):
    author = get_or_create_author(test_db_session, "")
    assert author.key == "unknown"
    assert author.comments_posted == 0
    assert author.last_interaction_at is None

    post = Post(url="https://example.com/nameless", raw_text="raw", status=PostStatus.DRAFTED.value, author_id=author.id)
    test_db_session.add(post)
    test_db_session.commit()

    transition_post_status(test_db_session, post.id, PostStatus.POSTED, final_comment="Anonymous comment")
    test_db_session.refresh(author)

    assert author.comments_posted == 0
    assert author.last_interaction_at is None


def test_author_prior_comments_ordering_limit_and_author_isolation(test_db_session):
    author1 = get_or_create_author(test_db_session, "Target Author")
    author2 = get_or_create_author(test_db_session, "Other Author")

    # Insert middle, newest, oldest so auto-increment IDs (1, 2, 3) are uncorrelated with dates
    post_middle = Post(
        url="https://example.com/2",
        raw_text="Post 2",
        status=PostStatus.POSTED.value,
        final_comment="Middle comment",
        posted_at=datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc),
        author_id=author1.id,
    )
    test_db_session.add(post_middle)
    test_db_session.commit()

    post_newest = Post(
        url="https://example.com/1",
        raw_text="Post 1",
        status=PostStatus.POSTED.value,
        final_comment="Newest comment",
        posted_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        author_id=author1.id,
    )
    test_db_session.add(post_newest)
    test_db_session.commit()

    post_oldest = Post(
        url="https://example.com/3",
        raw_text="Post 3",
        status=PostStatus.POSTED.value,
        final_comment="Oldest comment",
        posted_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        author_id=author1.id,
    )
    test_db_session.add(post_oldest)
    test_db_session.commit()

    # Confirm IDs are middle (1), newest (2), oldest (3)
    assert post_middle.id < post_newest.id < post_oldest.id

    # Drafted post and NULL-comment post for author1 must be excluded
    post_drafted = Post(
        url="https://example.com/draft",
        raw_text="Post draft",
        status=PostStatus.DRAFTED.value,
        final_comment="Draft comment text",
        posted_at=datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc),
        author_id=author1.id,
    )
    post_null_comment = Post(
        url="https://example.com/null",
        raw_text="Post null",
        status=PostStatus.POSTED.value,
        final_comment=None,
        posted_at=datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc),
        author_id=author1.id,
    )
    # Other author comment must be excluded
    post_other = Post(
        url="https://example.com/4",
        raw_text="Post 4",
        status=PostStatus.POSTED.value,
        final_comment="Comment to another person",
        posted_at=datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc),
        author_id=author2.id,
    )
    test_db_session.add_all([post_drafted, post_null_comment, post_other])
    test_db_session.commit()

    # Limit to 2: retrieves the 2 newest posted comments for author1 only (Newest, then Middle)
    comments = get_recent_author_comments(test_db_session, author1.id, limit=2)
    assert len(comments) == 2
    assert comments[0] == "Newest comment"
    assert comments[1] == "Middle comment"
    assert "Draft comment text" not in comments
    assert "Comment to another person" not in comments


def test_merge_authors_comprehensive(test_db_session):
    author1 = get_or_create_author(test_db_session, "Primary Author")
    author1.comments_posted = 3
    author1.notes = "Primary notes."
    test_db_session.add(author1)

    author2 = get_or_create_author(test_db_session, "Secondary Author")
    author2.comments_posted = 2
    author2.notes = "Secondary notes."
    test_db_session.add(author2)
    test_db_session.commit()

    post1 = Post(url="https://example.com/sec1", raw_text="text1", status=PostStatus.POSTED.value, author_id=author2.id)
    post2 = Post(url="https://example.com/sec2", raw_text="text2", status=PostStatus.DRAFTED.value, author_id=author2.id)
    test_db_session.add_all([post1, post2])
    test_db_session.commit()

    author2_id = author2.id
    merged = merge_authors(test_db_session, author1.id, author2.id)

    # Comments summed, notes concatenated, posts reassigned
    assert merged.id == author1.id
    assert merged.comments_posted == 5
    assert "Primary notes." in merged.notes
    assert "Secondary notes." in merged.notes

    test_db_session.refresh(post1)
    test_db_session.refresh(post2)
    assert post1.author_id == author1.id
    assert post2.author_id == author1.id

    # Secondary author deleted
    assert test_db_session.get(Author, author2_id) is None


def test_merge_authors_same_author_does_not_delete(test_db_session):
    author = get_or_create_author(test_db_session, "Self Merge Author")
    author.comments_posted = 3
    author.notes = "Original notes."
    test_db_session.add(author)
    test_db_session.commit()
    author_id = author.id

    merged = merge_authors(test_db_session, author_id, author_id)
    assert merged.id == author_id
    assert merged.comments_posted == 3
    assert merged.notes == "Original notes."
    assert test_db_session.get(Author, author_id) is not None


def test_author_merging_with_none_last_interaction(test_db_session):
    author_primary = get_or_create_author(test_db_session, "Author One")
    author_secondary = get_or_create_author(test_db_session, "Author Two")

    author_primary.last_interaction_at = None
    author_secondary.last_interaction_at = None
    test_db_session.add_all([author_primary, author_secondary])
    test_db_session.commit()

    merged = merge_authors(test_db_session, author_primary.id, author_secondary.id)
    assert merged.last_interaction_at is None

    author_third = get_or_create_author(test_db_session, "Author Three")
    interaction_time = datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc)
    author_third.last_interaction_at = interaction_time
    test_db_session.add(author_third)
    test_db_session.commit()

    merged2 = merge_authors(test_db_session, merged.id, author_third.id)
    assert merged2.last_interaction_at == interaction_time


def test_outreach_candidate_logic(test_db_session):
    author = get_or_create_author(test_db_session, "Carol Danvers")

    # None opportunity score is False
    author.comments_posted = 5
    assert is_outreach_candidate(author, max_opportunity_score=None) is False

    # comments_posted = 1 must NOT be candidate even with high opportunity score
    author.comments_posted = 1
    assert is_outreach_candidate(author, max_opportunity_score=9) is False

    # comments_posted = 2 with opportunity < 7 is False
    author.comments_posted = 2
    assert is_outreach_candidate(author, max_opportunity_score=6) is False

    # comments_posted = 2 with opportunity >= 7 is True
    assert is_outreach_candidate(author, max_opportunity_score=7) is True
    assert is_outreach_candidate(author, max_opportunity_score=10) is True

    # Override takes precedence
    author.outreach_override = False
    assert is_outreach_candidate(author, max_opportunity_score=10) is False

    author.outreach_override = True
    assert is_outreach_candidate(author, max_opportunity_score=1) is True


def test_services_llm_error_stores_error_message(test_db_session, monkeypatch):
    post = Post(url="https://example.com/post", raw_text="Sample text")
    test_db_session.add(post)
    test_db_session.commit()

    import services
    from llm import LLMError
    from unittest.mock import MagicMock
    monkeypatch.setattr(services, "analyze_and_draft_post", MagicMock(side_effect=LLMError("Simulated LLM API failure")))

    with pytest.raises(LLMError, match="Simulated LLM API failure"):
        services.generate_inline_drafts(post.id, test_db_session)

    test_db_session.refresh(post)
    assert post.status == PostStatus.CAPTURED.value
    assert post.error_message == "Simulated LLM API failure"


def test_services_runtime_error_propagates_and_stores_no_error_message(test_db_session, monkeypatch):
    post = Post(url="https://example.com/post2", raw_text="Sample text 2")
    test_db_session.add(post)
    test_db_session.commit()

    import services
    from unittest.mock import MagicMock
    monkeypatch.setattr(services, "analyze_and_draft_post", MagicMock(side_effect=RuntimeError("Programming bug or unexpected crash")))

    with pytest.raises(RuntimeError, match="Programming bug or unexpected crash"):
        services.generate_inline_drafts(post.id, test_db_session)

    test_db_session.refresh(post)
    assert post.status == PostStatus.CAPTURED.value
    assert post.error_message is None


def make_fake_analysis_result(
    author_name: str = "Alice Founder",
    headline: str = "CEO at AI Corp",
    post_body: str = "Insights on scaling AI pipelines.",
    draft_prefix: str = "Draft",
) -> InlineAnalysisResult:
    return InlineAnalysisResult(
        author_name=author_name,
        author_headline_or_company=headline,
        post_body=post_body,
        existing_comment_points=["Valid point on latency"],
        relevance_score=8,
        opportunity_score=9,
        topic_tags=["AI", "MLOps"],
        one_line_reason="High opportunity AI founder discussion",
        drafts=[
            SingleDraft(angle=DraftAngle.EXPERIENCE, text=f"{draft_prefix} Experience angle comment", adds_beyond_existing="adds exp"),
            SingleDraft(angle=DraftAngle.QUESTION, text=f"{draft_prefix} Question angle comment", adds_beyond_existing="adds q"),
            SingleDraft(angle=DraftAngle.COUNTERPOINT, text=f"{draft_prefix} Counterpoint angle comment", adds_beyond_existing="adds counter"),
        ],
    )


def test_generate_inline_drafts_new_author_single_call(test_db_session, monkeypatch):
    import services
    from unittest.mock import MagicMock
    post = Post(url="https://example.com/post_new", raw_text="Raw post text")
    test_db_session.add(post)
    test_db_session.commit()

    fake_result = make_fake_analysis_result(author_name="Brand New Author")
    mock_llm = MagicMock(return_value=fake_result)
    monkeypatch.setattr(services, "analyze_and_draft_post", mock_llm)

    response = services.generate_inline_drafts(post.id, test_db_session)
    assert mock_llm.call_count == 1
    assert response.author_name == "Brand New Author"
    assert len(response.drafts) == 3


def test_generate_inline_drafts_repeat_author_two_calls_replaces_drafts(test_db_session, monkeypatch):
    import services
    from unittest.mock import MagicMock

    author = get_or_create_author(test_db_session, "Repeat Author", "CTO")
    author.comments_posted = 3
    test_db_session.add(author)
    test_db_session.commit()

    prior_post = Post(
        url="https://example.com/prior",
        raw_text="prior",
        status=PostStatus.POSTED.value,
        final_comment="Prior interaction comment",
        posted_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        author_id=author.id,
    )
    test_db_session.add(prior_post)

    post = Post(url="https://example.com/repeat_post", raw_text="Repeat post text")
    test_db_session.add(post)
    test_db_session.commit()

    result1 = make_fake_analysis_result(author_name="Repeat Author", draft_prefix="Pass1")
    result2 = make_fake_analysis_result(author_name="Repeat Author", draft_prefix="Pass2")
    mock_llm = MagicMock(side_effect=[result1, result2])
    monkeypatch.setattr(services, "analyze_and_draft_post", mock_llm)

    response = services.generate_inline_drafts(post.id, test_db_session)
    assert mock_llm.call_count == 2
    second_prompt = mock_llm.call_args_list[1][0][0]
    assert "Prior interaction comment" in second_prompt

    assert response.drafts[0].text.startswith("Pass2")
    saved_drafts = test_db_session.exec(select(Draft).where(Draft.post_id == post.id)).all()
    assert len(saved_drafts) == 3
    assert all(d.text.startswith("Pass2") for d in saved_drafts)


def test_generate_inline_drafts_deadline_exceeded_skips_second_call(test_db_session, monkeypatch):
    import services
    from unittest.mock import MagicMock
    import time

    author = get_or_create_author(test_db_session, "Busy Author", "Engineer")
    author.comments_posted = 2
    test_db_session.add(author)
    test_db_session.commit()

    prior_post = Post(
        url="https://example.com/prior_busy",
        raw_text="prior",
        status=PostStatus.POSTED.value,
        final_comment="Busy author prior comment",
        posted_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        author_id=author.id,
    )
    test_db_session.add(prior_post)

    post = Post(url="https://example.com/busy_post", raw_text="Busy text")
    test_db_session.add(post)
    test_db_session.commit()

    result1 = make_fake_analysis_result(author_name="Busy Author", draft_prefix="Pass1")
    mock_llm = MagicMock(return_value=result1)
    monkeypatch.setattr(services, "analyze_and_draft_post", mock_llm)

    clock_values = [0.0, 100.0, 100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock_values.pop(0) if clock_values else 100.0)

    response = services.generate_inline_drafts(post.id, test_db_session)
    assert mock_llm.call_count == 1
    assert response.drafts[0].text.startswith("Pass1")


def test_generate_inline_drafts_duplicate_warning_flag(test_db_session, monkeypatch):
    import services
    from unittest.mock import MagicMock

    author = get_or_create_author(test_db_session, "Dup Author")
    body_text = "Unique discussion text on distributed systems."
    body_hash = compute_body_hash(body_text)

    posted_post = Post(
        url="https://example.com/earlier",
        raw_text="earlier text",
        post_body=body_text,
        post_body_hash=body_hash,
        status=PostStatus.POSTED.value,
        final_comment="Already commented on this post yesterday.",
        posted_at=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
        author_id=author.id,
    )
    test_db_session.add(posted_post)

    new_post = Post(url="https://example.com/new_clip", raw_text=body_text)
    test_db_session.add(new_post)
    test_db_session.commit()

    fake_result = make_fake_analysis_result(author_name="Dup Author", post_body=body_text)
    monkeypatch.setattr(services, "analyze_and_draft_post", MagicMock(return_value=fake_result))

    response = services.generate_inline_drafts(new_post.id, test_db_session)
    assert response.duplicate_warning is not None
    assert response.duplicate_warning.final_comment == "Already commented on this post yesterday."


def test_generate_inline_drafts_drafts_saved_and_status_drafted(test_db_session, monkeypatch):
    import services
    from unittest.mock import MagicMock

    post = Post(url="https://example.com/save_post", raw_text="Save post text")
    test_db_session.add(post)
    test_db_session.commit()

    fake_result = make_fake_analysis_result(author_name="Saved Author")
    monkeypatch.setattr(services, "analyze_and_draft_post", MagicMock(return_value=fake_result))

    response = services.generate_inline_drafts(post.id, test_db_session)
    test_db_session.refresh(post)

    assert post.status == PostStatus.DRAFTED.value
    assert post.error_message is None
    assert post.author_id is not None

    db_drafts = test_db_session.exec(select(Draft).where(Draft.post_id == post.id)).all()
    assert len(db_drafts) == 3
    assert {d.angle for d in db_drafts} == {"experience", "question", "counterpoint"}


