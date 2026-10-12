"""Tests for capture_server HTTP endpoints, authentication, and error handling."""

import inspect
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import capture_server
import config
from db import get_engine, init_db
from llm import LLMError
from models import (
    Draft,
    DraftAngle,
    DraftResponse,
    Post,
    PostStatus,
    SingleDraft,
)


VALID_TEST_TOKEN = "valid-test-capture-token-12345"


@pytest.fixture
def server_client(tmp_path, monkeypatch):
    """Provides a TestClient connected to an isolated test database with valid settings."""
    db_file = tmp_path / "server_test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "CAPTURE_TOKEN", VALID_TEST_TOKEN)
    monkeypatch.setattr(config, "LLM_API_KEY", "dummy-api-key-123456789012345")
    monkeypatch.setattr(config, "MODEL_NAME", "dummy-model")

    init_db()

    # Override get_db_session to use the fresh engine for this test
    def get_test_db():
        with Session(get_engine(str(db_file))) as session:
            yield session

    capture_server.app.dependency_overrides[capture_server.get_db_session] = get_test_db
    client = TestClient(capture_server.app, raise_server_exceptions=False)
    yield client, db_file
    capture_server.app.dependency_overrides.clear()


def auth_headers(token: str = VALID_TEST_TOKEN) -> dict[str, str]:
    return {"X-Capture-Token": token}


def test_every_endpoint_returns_401_without_valid_token(server_client):
    client, _ = server_client

    endpoints = [
        ("GET", "/count", None),
        ("POST", "/draft", {"selected_text": "text", "url": "https://linkedin.com/post/1"}),
        ("POST", "/posts/1/regenerate", None),
        ("POST", "/posts/1/skip", None),
        ("POST", "/posts/1/posted", {"final_comment": "great post"}),
        ("GET", "/profile", None),
        ("PUT", "/profile", {"content": "test"}),
    ]

    for method, path, json_data in endpoints:
        # Missing token
        resp_missing = client.request(method, path, json=json_data)
        assert resp_missing.status_code == 401, f"{method} {path} without token returned {resp_missing.status_code}"

        # Invalid token
        resp_invalid = client.request(method, path, json=json_data, headers={"X-Capture-Token": "invalid-token"})
        assert resp_invalid.status_code == 401, f"{method} {path} with invalid token returned {resp_invalid.status_code}"


def test_draft_rejects_oversize_selection_with_400(server_client):
    client, _ = server_client
    oversize_text = "x" * (config.MAX_SELECTION_CHARS + 1)
    payload = {
        "selected_text": oversize_text,
        "url": "https://www.linkedin.com/posts/oversize-1",
    }
    resp = client.post("/draft", json=payload, headers=auth_headers())
    assert resp.status_code == 400
    assert "maximum character limit" in resp.json()["detail"]


def test_draft_llm_error_returns_502_with_post_id_and_keeps_captured_post(server_client, monkeypatch):
    client, db_file = server_client

    def fake_generate_drafts(post_id: int, session: Session):
        post = session.get(Post, post_id)
        post.error_message = "LLM upstream rate limit reached"
        session.add(post)
        session.commit()
        raise LLMError("LLM upstream rate limit reached")

    monkeypatch.setattr(capture_server, "generate_inline_drafts", fake_generate_drafts)

    payload = {
        "selected_text": "valid short selection",
        "url": "https://www.linkedin.com/posts/test-llm-error",
    }
    resp = client.post("/draft", json=payload, headers=auth_headers())
    assert resp.status_code == 502

    body = resp.json()
    assert "post_id" in body
    captured_post_id = body["post_id"]

    # Verify captured post is preserved in database with error message
    with Session(get_engine(str(db_file))) as session:
        post = session.get(Post, captured_post_id)
        assert post is not None
        assert post.status == PostStatus.CAPTURED.value
        assert post.error_message == "LLM upstream rate limit reached"


def test_draft_other_exception_returns_500_keeps_captured_post_and_does_not_leak_exception(server_client, monkeypatch):
    client, db_file = server_client

    def fake_generate_drafts_crash(post_id: int, session: Session):
        raise RuntimeError("super_secret_database_credential_leak")

    monkeypatch.setattr(capture_server, "generate_inline_drafts", fake_generate_drafts_crash)

    payload = {
        "selected_text": "valid short selection",
        "url": "https://www.linkedin.com/posts/test-other-error",
    }
    resp = client.post("/draft", json=payload, headers=auth_headers())
    assert resp.status_code == 500

    body = resp.json()
    assert "post_id" in body
    assert "super_secret" not in resp.text

    captured_post_id = body["post_id"]
    with Session(get_engine(str(db_file))) as session:
        post = session.get(Post, captured_post_id)
        assert post is not None
        assert post.status == PostStatus.CAPTURED.value
        assert post.error_message is None


def test_posts_posted_with_empty_final_comment_returns_4xx_and_changes_no_state(server_client):
    client, db_file = server_client

    with Session(get_engine(str(db_file))) as session:
        post = Post(
            url="https://linkedin.com/post/drafted",
            raw_text="sample text",
            status=PostStatus.DRAFTED.value,
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        post_id = post.id

    for empty_val in ["", "   "]:
        resp = client.post(
            f"/posts/{post_id}/posted",
            json={"final_comment": empty_val},
            headers=auth_headers(),
        )
        assert resp.status_code == 400

    with Session(get_engine(str(db_file))) as session:
        fresh_post = session.get(Post, post_id)
        assert fresh_post.status == PostStatus.DRAFTED.value
        assert fresh_post.final_comment is None
        assert fresh_post.posted_at is None


def test_invalid_transition_returns_409(server_client):
    client, db_file = server_client

    # A post in CAPTURED status cannot transition directly to POSTED
    with Session(get_engine(str(db_file))) as session:
        post = Post(
            url="https://linkedin.com/post/captured",
            raw_text="sample text",
            status=PostStatus.CAPTURED.value,
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        post_id = post.id

    resp = client.post(
        f"/posts/{post_id}/posted",
        json={"final_comment": "Valid comment but invalid transition"},
        headers=auth_headers(),
    )
    assert resp.status_code == 409


def test_skip_works(server_client):
    client, db_file = server_client

    with Session(get_engine(str(db_file))) as session:
        post = Post(
            url="https://linkedin.com/post/to-skip",
            raw_text="sample text",
            status=PostStatus.CAPTURED.value,
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        post_id = post.id

    resp = client.post(f"/posts/{post_id}/skip", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == PostStatus.SKIPPED.value

    with Session(get_engine(str(db_file))) as session:
        fresh = session.get(Post, post_id)
        assert fresh.status == PostStatus.SKIPPED.value


def test_regenerate_replaces_drafts(server_client, monkeypatch):
    client, db_file = server_client

    with Session(get_engine(str(db_file))) as session:
        post = Post(
            url="https://linkedin.com/post/to-regen",
            raw_text="sample text",
            status=PostStatus.DRAFTED.value,
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        post_id = post.id

        initial_draft = Draft(
            post_id=post_id,
            angle=DraftAngle.EXPERIENCE.value,
            text="Initial draft text",
            adds_beyond_existing="First idea",
        )
        session.add(initial_draft)
        session.commit()

    # Mock generate_inline_drafts to replace drafts in DB
    def fake_regenerate(target_id: int, session: Session, guidance: Optional[str] = None) -> DraftResponse:
        session.exec(Draft.__table__.delete().where(Draft.post_id == target_id))
        new_db_draft = Draft(
            post_id=target_id,
            angle=DraftAngle.QUESTION.value,
            text="Brand new regenerated draft",
            adds_beyond_existing="Second idea",
        )
        session.add(new_db_draft)
        session.commit()
        return DraftResponse(
            post_id=target_id,
            author_name="Alice",
            author_headline="VP Engineering",
            post_body="Clean body",
            relevance_score=8,
            opportunity_score=9,
            one_line_reason="High alignment",
            drafts=[
                SingleDraft(
                    angle=DraftAngle.QUESTION,
                    text="Brand new regenerated draft",
                    adds_beyond_existing="Second idea",
                )
            ],
        )

    monkeypatch.setattr(capture_server, "generate_inline_drafts", fake_regenerate)

    resp = client.post(f"/posts/{post_id}/regenerate", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["drafts"][0]["text"] == "Brand new regenerated draft"

    with Session(get_engine(str(db_file))) as session:
        db_drafts = session.exec(select(Draft).where(Draft.post_id == post_id)).all()
        assert len(db_drafts) == 1
        assert db_drafts[0].text == "Brand new regenerated draft"


def test_llm_calling_endpoints_are_sync_def():
    # FastAPI dispatches sync def endpoints to worker threads, preventing event loop blocking
    assert not inspect.iscoroutinefunction(capture_server.draft_post)
    assert not inspect.iscoroutinefunction(capture_server.regenerate_post)


def test_regenerate_passes_guidance(server_client, monkeypatch):
    client, db_file = server_client

    with Session(get_engine(str(db_file))) as session:
        post = Post(
            url="https://linkedin.com/post/guidance-test",
            raw_text="sample text",
            status=PostStatus.DRAFTED.value,
        )
        session.add(post)
        session.commit()
        session.refresh(post)
        post_id = post.id

    captured_guidance = []

    def fake_regenerate(target_id: int, session: Session, guidance: Optional[str] = None) -> DraftResponse:
        captured_guidance.append(guidance)
        return DraftResponse(
            post_id=target_id,
            author_name="Alice",
            author_headline="Dev",
            post_body="Body",
            relevance_score=8,
            opportunity_score=8,
            one_line_reason="Reason",
            drafts=[
                SingleDraft(
                    angle=DraftAngle.QUESTION,
                    text="Guided draft",
                    adds_beyond_existing="Idea",
                )
            ],
        )

    monkeypatch.setattr(capture_server, "generate_inline_drafts", fake_regenerate)

    resp = client.post(
        f"/posts/{post_id}/regenerate",
        json={"guidance": "Focus on memory footprint"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert captured_guidance == ["Focus on memory footprint"]


def test_get_and_update_profile(server_client, tmp_path, monkeypatch):
    client, _ = server_client
    test_profile_path = tmp_path / "test_profile.md"
    test_profile_path.write_text("# Initial Profile", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", test_profile_path)
    monkeypatch.setattr("services.PROFILE_PATH", test_profile_path)

    # GET profile
    resp = client.get("/profile", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["content"] == "# Initial Profile"

    # PUT profile
    resp_put = client.put(
        "/profile",
        json={"content": "# Updated Profile\n- Focus: AI Systems"},
        headers=auth_headers(),
    )
    assert resp_put.status_code == 200
    assert "Focus: AI Systems" in resp_put.json()["content"]
    assert test_profile_path.read_text(encoding="utf-8") == "# Updated Profile\n- Focus: AI Systems"
