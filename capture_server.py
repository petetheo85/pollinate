"""Local FastAPI server for capturing LinkedIn post selections from the Chrome extension."""

from contextlib import asynccontextmanager
import hmac
from typing import Generator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import Session, func, select

import config
from config import BIND_HOST, CAPTURE_PORT, MAX_SELECTION_CHARS, validate_settings
from db import InvalidStatusTransitionError, get_engine, init_db, transition_post_status
from llm import LLMError
from models import DraftRequest, DraftResponse, Post, PostPostedRequest, PostStatus
from services import generate_inline_drafts


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Initializes and validates configuration and database before serving requests."""
    validate_settings()
    init_db()
    yield


app = FastAPI(title="Pollinate Capture Server", lifespan=lifespan)

# Allow requests from Chrome extensions via regex
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_capture_token(
    x_capture_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Validates capture bearer token using constant-time comparison to prevent timing attacks."""
    token = x_capture_token
    if not token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing capture token.",
        )

    # Constant-time comparison defends against side-channel timing analysis
    expected = config.CAPTURE_TOKEN.encode("utf-8")
    actual = token.encode("utf-8")
    if not hmac.compare_digest(actual, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid capture token.",
        )
    return token



def get_db_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


@app.get("/count", dependencies=[Depends(verify_capture_token)])
def get_actionable_count(session: Session = Depends(get_db_session)):
    count = session.exec(
        select(func.count(Post.id)).where(
            Post.status.in_([PostStatus.CAPTURED.value, PostStatus.DRAFTED.value])
        )
    ).one()
    return {"count": count}


@app.post("/draft", response_model=DraftResponse, dependencies=[Depends(verify_capture_token)])
@app.post("/capture", response_model=DraftResponse, dependencies=[Depends(verify_capture_token)])
def draft_post(
    req: DraftRequest,
    session: Session = Depends(get_db_session),
):
    """Captures post selection and generates inline comment drafts synchronously in threadpool."""
    if len(req.selected_text) > MAX_SELECTION_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Selection exceeds maximum character limit of {MAX_SELECTION_CHARS}.",
        )

    # Raw text is saved immediately in captured state before LLM inference
    post = Post(
        url=req.url,
        raw_text=req.selected_text,
        page_title=req.page_title or "",
        status=PostStatus.CAPTURED.value,
    )
    session.add(post)
    session.commit()
    session.refresh(post)

    try:
        return generate_inline_drafts(post.id, session)
    except LLMError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc), "post_id": post.id},
        )
    except Exception:
        # Unexpected errors preserve the captured post without persisting or leaking internal exception text
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "post_id": post.id},
        )


@app.post("/posts/{post_id}/regenerate", response_model=DraftResponse, dependencies=[Depends(verify_capture_token)])
def regenerate_post(
    post_id: int,
    session: Session = Depends(get_db_session),
):
    """Regenerates drafts for an existing post synchronously in threadpool."""
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post {post_id} not found.",
        )

    if post.status == PostStatus.POSTED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot regenerate drafts for a posted post.",
        )

    try:
        return generate_inline_drafts(post.id, session)
    except LLMError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc), "post_id": post.id},
        )
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "post_id": post.id},
        )


@app.post("/posts/{post_id}/skip", dependencies=[Depends(verify_capture_token)])
def skip_post(
    post_id: int,
    session: Session = Depends(get_db_session),
):
    """Transitions post status to skipped."""
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post {post_id} not found.",
        )

    try:
        updated = transition_post_status(session, post_id, PostStatus.SKIPPED)
        return {"post_id": updated.id, "status": updated.status}
    except InvalidStatusTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )


@app.post("/posts/{post_id}/posted", dependencies=[Depends(verify_capture_token)])
def mark_post_posted(
    post_id: int,
    req: PostPostedRequest,
    session: Session = Depends(get_db_session),
):
    """Transitions post status to posted with the chosen final comment."""
    if not req.final_comment or not req.final_comment.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="final_comment cannot be empty.",
        )

    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post {post_id} not found.",
        )

    try:
        updated = transition_post_status(
            session,
            post_id,
            PostStatus.POSTED,
            final_comment=req.final_comment,
        )
        return {"post_id": updated.id, "status": updated.status}
    except InvalidStatusTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("capture_server:app", host=BIND_HOST, port=CAPTURE_PORT, reload=False)
