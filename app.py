"""Streamlit application for Pollinate posted history, author CRM, and voice profile."""

import urllib.parse
from typing import Optional

import streamlit as st
from sqlmodel import Session, desc, func, select


from db import (
    get_engine,
    init_db,
    is_outreach_candidate,
    merge_authors,
)
from llm import LLMError
from models import (
    Author,
    Post,
    PostStatus,
)
from services import (
    format_local_datetime as format_dt,
    generate_author_dm_draft,
    load_profile,
    save_profile,
)

# Page configuration
st.set_page_config(
    page_title="Pollinate",
    page_icon="🐝",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom minimal CSS for clean spacing and badges
st.markdown(
    """
    <style>
    .badge-pill {
        display: inline-block;
        font-size: 11px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 12px;
        margin-right: 6px;
        background: rgba(245, 158, 11, 0.15);
        color: #f59e0b;
        border: 1px solid rgba(245, 158, 11, 0.4);
    }
    .badge-pill-success {
        background: rgba(63, 185, 80, 0.15);
        color: #3fb950;
        border: 1px solid rgba(63, 185, 80, 0.4);
    }
    .badge-pill-candidate {
        background: rgba(239, 68, 68, 0.15);
        color: #ef4444;
        border: 1px solid rgba(239, 68, 68, 0.4);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_db():
    engine = get_engine()
    init_db(engine)
    return engine


def is_specific_post_url(url: Optional[str]) -> bool:
    """Returns True if url points to a distinct post rather than the general feed root."""
    if not url:
        return False
    clean = url.strip().lower().rstrip("/")
    if clean in (
        "https://www.linkedin.com/feed",
        "http://www.linkedin.com/feed",
        "https://linkedin.com/feed",
        "http://linkedin.com/feed",
    ):
        return False
    return any(marker in clean for marker in ("/posts/", "urn:li:activity", "/activity/", "/update/"))


# Top-level navigation
st.sidebar.title("🐝 Pollinate")
nav_choice = st.sidebar.radio(
    "Navigation",
    ["Posted History", "Author CRM", "User Profile"],
)


# Posted history
if nav_choice == "Posted History":
    st.header("Posted History")

    with Session(get_db()) as session:
        posted_count = session.exec(
            select(func.count(Post.id)).where(Post.status == PostStatus.POSTED.value)
        ).one()

    st.metric("Total Comments Posted", posted_count)

    search_query = st.text_input("Filter by author or comment text", "")

    with Session(get_db()) as session:
        stmt = select(Post).where(Post.status == PostStatus.POSTED.value).order_by(desc(Post.posted_at))
        posted_posts = session.exec(stmt).all()

    if search_query:
        posted_posts = [
            p for p in posted_posts
            if (p.final_comment and search_query.lower() in p.final_comment.lower())
            or (p.post_body and search_query.lower() in p.post_body.lower())
        ]

    if not posted_posts:
        st.info("No posted comments recorded yet.")
    else:
        for p in posted_posts:
            with st.container(border=True):
                with Session(get_db()) as session:
                    author = session.get(Author, p.author_id) if p.author_id else None
                    author_name = author.name if author else "Unknown Author"
                    author_headline = author.headline_or_company if author else ""

                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(f"### {author_name}")
                    if author_headline:
                        st.caption(author_headline)
                    st.caption(f"Posted: {format_dt(p.posted_at)}")
                with c2:
                    if is_specific_post_url(p.url):
                        st.link_button("View Post on LinkedIn", p.url)
                    else:
                        st.caption("📍 Captured from feed")
                        if author and author.name and author.key != "unknown":
                            encoded = urllib.parse.quote(author.name)
                            st.link_button("Find Author on LinkedIn", f"https://www.linkedin.com/search/results/all/?keywords={encoded}")

                st.markdown("**Your Posted Comment:**")
                st.info(p.final_comment or "No comment recorded.")

                with st.expander("Original Post Excerpt"):
                    st.write(p.post_body or p.raw_text)


# Author CRM
elif nav_choice == "Author CRM":
    st.header("Author CRM & Relationship Tracker")

    with Session(get_db()) as session:
        all_authors = session.exec(select(Author).order_by(desc(Author.comments_posted))).all()
        total_authors = len(all_authors)
        repeat_authors = sum(1 for a in all_authors if a.comments_posted >= 1)

        author_max_opp = {}
        for a in all_authors:
            max_val = session.exec(
                select(func.max(Post.opportunity_score)).where(Post.author_id == a.id)
            ).one_or_none()
            author_max_opp[a.id] = max_val or 0

        outreach_candidates = [a for a in all_authors if is_outreach_candidate(a, author_max_opp.get(a.id))]

    col1, col2, col3 = st.columns(3)
    col1.metric("Tracked Authors", total_authors)
    col2.metric("Interacted Authors (≥1 comment)", repeat_authors)
    col3.metric("Outreach Candidates", len(outreach_candidates))

    f1, f2 = st.columns([2, 1])
    search_author = f1.text_input("Filter authors by name or headline", "")
    show_candidates_only = f2.checkbox("Show Outreach Candidates Only", value=False)

    displayed_authors = all_authors
    if show_candidates_only:
        displayed_authors = outreach_candidates
    if search_author:
        displayed_authors = [
            a for a in displayed_authors
            if search_author.lower() in a.name.lower()
            or (a.headline_or_company and search_author.lower() in a.headline_or_company.lower())
        ]

    if not displayed_authors:
        st.info("No author records match your filter.")
    else:
        for author in displayed_authors:
            max_opp = author_max_opp.get(author.id, 0)
            is_candidate = is_outreach_candidate(author, max_opp)

            with st.container(border=True):
                ac1, ac2 = st.columns([3, 1])
                with ac1:
                    st.markdown(f"### {author.name}")
                    if author.headline_or_company:
                        st.caption(author.headline_or_company)
                with ac2:
                    st.metric("Comments Posted", author.comments_posted)

                badges = []
                if is_candidate:
                    badges.append("<span class='badge-pill badge-pill-candidate'>🔥 Outreach Candidate</span>")
                if author.last_interaction_at:
                    badges.append(f"<span class='badge-pill'>Last Interaction: {format_dt(author.last_interaction_at)}</span>")
                if badges:
                    st.markdown("".join(badges), unsafe_allow_html=True)

                current_override = "Auto (Calculated)"
                if author.outreach_override is True:
                    current_override = "Force Candidate"
                elif author.outreach_override is False:
                    current_override = "Dismiss"

                o_choice = st.selectbox(
                    "Outreach Candidate Status",
                    ["Auto (Calculated)", "Force Candidate", "Dismiss"],
                    index=["Auto (Calculated)", "Force Candidate", "Dismiss"].index(current_override),
                    key=f"outreach_override_{author.id}",
                )
                new_override = None if o_choice == "Auto (Calculated)" else (o_choice == "Force Candidate")
                if new_override != author.outreach_override:
                    with Session(get_db()) as s:
                        db_author = s.get(Author, author.id)
                        db_author.outreach_override = new_override
                        s.add(db_author)
                        s.commit()
                    st.success("Updated outreach status!")
                    st.rerun()

                # Author Notes
                notes_key = f"notes_{author.id}"
                new_notes = st.text_area("Relationship & Context Notes", value=author.notes, key=notes_key, height=90)
                if st.button("Save Notes", key=f"save_notes_{author.id}"):
                    with Session(get_db()) as s:
                        db_author = s.get(Author, author.id)
                        db_author.notes = new_notes.strip()
                        s.add(db_author)
                        s.commit()
                    st.success("Notes saved!")
                    st.rerun()

                # Direct Message (DM) Drafting
                if is_candidate or author.last_dm_draft:
                    st.markdown("#### Direct Message (DM) Outreach")
                    if author.last_dm_draft:
                        st.info(f"**Last DM Draft:**\n\n{author.last_dm_draft}")

                    if st.button("Generate DM Draft", key=f"btn_dm_{author.id}"):
                        with st.spinner("Drafting authentic peer-to-peer message..."):
                            try:
                                with Session(get_db()) as s:
                                    draft_text = generate_author_dm_draft(author.id, s)
                                st.success("DM Draft generated!")
                                st.rerun()
                            except LLMError as exc:
                                st.error(f"Failed to generate DM: {exc}")

                # Author Merge Tool
                with st.expander("Merge Duplicate Author"):
                    other_authors = [a for a in all_authors if a.id != author.id]
                    if other_authors:
                        merge_target = st.selectbox(
                            "Select author to absorb into this record",
                            options=other_authors,
                            format_func=lambda a: f"{a.name} ({a.headline_or_company or 'No headline'})",
                            key=f"merge_target_{author.id}",
                        )
                        if st.button("Merge Author Into This Record", key=f"btn_merge_{author.id}"):
                            with Session(get_db()) as s:
                                merge_authors(s, primary_author_id=author.id, secondary_author_id=merge_target.id)
                            st.success(f"Merged {merge_target.name} into {author.name}!")
                            st.rerun()


# User profile
elif nav_choice == "User Profile":
    st.header("User Profile & Voice Guidelines")
    st.caption("This profile is loaded directly into Gemini prompts to anchor all drafts in your authentic experience.")

    current_profile = load_profile()
    profile_input = st.text_area(
        "Profile Markdown (context/profile.md)",
        value=current_profile,
        height=450,
    )

    if st.button("Save Profile", type="primary"):
        save_profile(profile_input)
        st.success("Profile saved successfully to context/profile.md!")
