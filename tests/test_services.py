"""Tests for services module: profile management, inline drafting orchestration, and DM drafting."""

from datetime import datetime, timezone

import pytest
from sqlmodel import SQLModel, Session

import config
from db import get_engine, get_or_create_author
import llm
from models import Author, DMDraftResult, Post, PostStatus
from services import (
    format_local_datetime,
    generate_author_dm_draft,
    load_profile,
    save_profile,
)


@pytest.fixture
def test_db_session(tmp_path):
    db_file = tmp_path / "test_services.db"
    engine = get_engine(db_path=str(db_file))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_profile_load_and_save(tmp_path, monkeypatch):
    test_profile = tmp_path / "profile.md"
    monkeypatch.setattr(config, "PROFILE_PATH", test_profile)
    monkeypatch.setattr("services.PROFILE_PATH", test_profile)

    assert load_profile() == ""

    save_profile("# Engineer Profile\nExpertise in distributed systems.")
    assert test_profile.exists()
    assert load_profile() == "# Engineer Profile\nExpertise in distributed systems."


def test_generate_author_dm_draft_success(test_db_session, monkeypatch):
    author = get_or_create_author(test_db_session, "Ada Lovelace", "Computing Pioneer")
    author.notes = "Discussed analytical engines in comments."
    test_db_session.add(author)
    test_db_session.commit()

    # Create a prior posted comment for context
    post = Post(
        url="https://linkedin.com/ada/post-1",
        raw_text="Analytical engine architectures",
        status=PostStatus.POSTED.value,
        final_comment="Great point regarding conditional branching in mechanical computing.",
        author_id=author.id,
    )
    test_db_session.add(post)
    test_db_session.commit()

    def fake_generate_structured(prompt: str, schema):
        assert "Ada Lovelace" in prompt
        assert "Computing Pioneer" in prompt
        assert "conditional branching" in prompt
        return DMDraftResult(
            dm_text="Hi Ada, enjoyed our exchange on branching architectures. Would love to stay connected here."
        )

    monkeypatch.setattr("services.generate_structured", fake_generate_structured)

    dm_text = generate_author_dm_draft(author.id, test_db_session)
    assert "Hi Ada" in dm_text

    test_db_session.refresh(author)
    assert author.last_dm_draft == dm_text


def test_generate_author_dm_draft_nonexistent_author_raises(test_db_session):
    with pytest.raises(ValueError, match="Author 9999 not found"):
        generate_author_dm_draft(9999, test_db_session)


def test_format_local_datetime_none_returns_na():
    assert format_local_datetime(None) == "N/A"


def test_format_local_datetime_converts_utc_to_local():
    utc_dt = datetime(2026, 10, 12, 23, 36, 0, tzinfo=timezone.utc)
    expected_local = utc_dt.astimezone().strftime("%b %d, %Y %H:%M")
    assert format_local_datetime(utc_dt) == expected_local


def test_format_local_datetime_handles_naive_utc():
    naive_dt = datetime(2026, 10, 12, 23, 36, 0)
    expected_local = naive_dt.replace(tzinfo=timezone.utc).astimezone().strftime("%b %d, %Y %H:%M")
    assert format_local_datetime(naive_dt) == expected_local

