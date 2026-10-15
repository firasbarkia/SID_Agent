"""Local test console: uv run --group demo streamlit run streamlit_app.py."""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pandas as pd
import streamlit as st

from app.demo import DEMO_SERVICE_KEY, DEMO_USERS
from examples.simulation_checks import run_checks

SAMPLE_PROFILE = (
    "Name: Demo Candidate\nEmail: demo@example.test\nLocation: Sfax\n"
    "Headline: Computer science student seeking a PFE internship\n"
    "Skill: Python\nSkill: SQL\nEducation: Computer science degree in progress\n"
    "Project: Python inventory application with a SQL database"
)
SAMPLE_JOB = "PFE internship in Sfax requiring Python and SQL for web development."

st.set_page_config(page_title="SID · Test workspace", page_icon="🧪", layout="wide")


def api(method, path, *, body=None, data=None, new_job=False, quiet=False):
    headers = {"X-Service-Key": DEMO_SERVICE_KEY, "X-Demo-User": user}
    if new_job:
        headers["Idempotency-Key"] = "console-" + uuid4().hex
    if data is not None:
        headers["Content-Type"] = "application/pdf"
    try:
        response = httpx.request(
            method,
            base + path,
            headers=headers,
            json=body,
            content=data,
            timeout=210 if path == "/demo/knowledge/setup" else 100,
            trust_env=False,
        )
        try:
            result = response.json() if response.content else None
        except ValueError:
            result = {"detail": response.text[:1000]}
    except httpx.HTTPError as exc:
        if not quiet:
            st.error(f"Could not reach the demo API: {exc}")
        return None
    st.session_state["last_call"] = {
        "method": method,
        "path": path,
        "status": response.status_code,
        "response": result,
    }
    if response.is_error:
        if not quiet:
            st.error(f"HTTP {response.status_code}: {json.dumps(result, ensure_ascii=False)}")
        return None
    return result if result is not None else {"ok": True}


def inspect(label, value):
    with st.expander(label):
        st.json(value)


def choose(label, items, key):
    if not items:
        st.info(f"No {label.lower()} yet for this test user.")
        return None
    by_id = {item["id"]: item for item in items}
    preferred = ws.get(key)
    widget_key = f"{user}-{key}"
    if preferred in by_id and preferred != ws.get(f"{key}:selected"):
        st.session_state[widget_key] = preferred
    selected = st.selectbox(
        label,
        list(by_id),
        index=list(by_id).index(preferred) if preferred in by_id else 0,
        format_func=lambda item: (
            f"{item[:12]} · {by_id[item].get('status', 'profile')} · "
            f"v{by_id[item].get('version', by_id[item].get('revision', 0))}"
        ),
        key=widget_key,
    )
    ws[key] = ws[f"{key}:selected"] = selected
    return selected


def simulation():
    st.title("AI pipeline simulation")
    st.write(
        "Test the integration APIs with synthetic users and offers, then inspect each response."
    )
    st.caption(
        "Context routing → advice / offer search → response + memory → pack for human review. "
        "Provider responses are scripted; MongoDB, PDF workers and reference retrieval are real."
    )
    if status["ai_mode"] != "simulated":
        st.info("Start with python -m scripts.simulation start to use the simulation scenarios.")
        return
    st.subheader("1. Verify the complete flow")
    st.write(
        "The checks import and accept a sample CV, search offers, change location, test advice, "
        "generate a pack and verify access restrictions. "
        "They use both candidates and the recruiter. "
        "Each run creates new synthetic records and automatically accepts only its sample CV."
    )
    if st.button("Run pipeline checks", type="primary"):
        with st.spinner("Testing API behavior and background workers..."):
            st.session_state["simulation_report"] = asyncio.run(run_checks(base))
    report = st.session_state.get("simulation_report")
    if report:
        if report["passed"]:
            st.success(f"All {len(report['checks'])} scenarios passed.")
        else:
            st.error("A scenario failed. Inspect the report before continuing.")
        st.dataframe(report["checks"], hide_index=True, width="stretch")
        st.download_button(
            "Download scenario report",
            json.dumps(report, indent=2, ensure_ascii=False),
            "sid-simulation-report.json",
            mime="application/json",
        )
        inspect("Requests, responses and created record IDs", report)
    st.subheader("2. Try the conversation yourself")
    st.write(
        "Start a session for the identity selected in the sidebar. Candidate sessions create "
        "and accept a synthetic Python/SQL profile. Recruiter searches return matching_unavailable."
    )
    if st.button("Start synthetic session"):
        payload = {"language": language}
        if DEMO_USERS[user] == "CANDIDATE":
            profile = api("POST", "/api/v1/profiles", body={"content": SAMPLE_PROFILE})
            if not profile:
                return
            accepted = api(
                "POST",
                f"/api/v1/profiles/{profile['id']}/accept",
                body={
                    "expected_version": profile["version"],
                    "confirmed": True,
                },
            )
            if not accepted:
                return
            ws["profile"] = profile["id"]
            payload.update(profile_id=profile["id"], profile_version=profile["version"])
        conversation = api("POST", "/api/v1/conversations", body=payload)
        if conversation:
            ws["simulation_conversation"] = ws["conversation"] = conversation["id"]
            ws.pop("simulation_turn", None)
            st.rerun()
    conversation_id = ws.get("simulation_conversation")
    if not conversation_id:
        st.info("Start a synthetic session to send prompts.")
        return
    history = api("GET", f"/api/v1/conversations/{conversation_id}")
    if not history:
        return
    prompts = {
        "Find a PFE in Sfax": ("Je cherche un stage PFE Python à Sfax", "fixture-python-pfe"),
        "Change location": (
            "Tunis instead",
            "After the Sfax search: fixture-tunis-pfe; keep PFE + Python",
        ),
        "No results": ("Find Python PFE Sousse", "no_matches, empty results"),
        "Clarify locations": ("Find Python PFE in Sfax or Tunis", "clarification_required"),
        "Ask for advice": ("Advice on Python skills for my CV", "answered, with cited references"),
        "Recruiter search": ("Find Python candidates", "As recruiter: matching_unavailable"),
    }
    selected = st.selectbox("Scenario", list(prompts))
    st.caption("Expected: " + prompts[selected][1])
    with st.form("simulation_prompt"):
        message = st.text_area("Prompt", prompts[selected][0], key=f"sim-prompt:{user}:{selected}")
        if st.form_submit_button("Send through /agent/chat", type="primary"):
            turn = api(
                "POST",
                "/agent/chat",
                new_job=True,
                body={
                    "conversation_id": conversation_id,
                    "expected_revision": history["revision"],
                    "message": message,
                },
            )
            if turn:
                ws["simulation_turn"] = turn
                st.rerun()
    turn = ws.get("simulation_turn")
    if turn:
        response = turn["response"]
        st.write(f"**{response['intent']} → {response['status']}** · Revision {turn['revision']}")
        st.write(response["text"])
        st.caption("Saved memory: " + ", ".join(response["keywords"]))
        if response["results"]:
            st.dataframe(
                [
                    {
                        "ID": row["id"],
                        "Title": row["title"],
                        "Location": row["location"],
                        "Contract": row["contract"],
                        "Synthetic similarity": row["ranking_score"],
                    }
                    for row in response["results"]
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "Ranking scores are synthetic similarity values, not compatibility percentages."
            )
        inspect("Full integration response", turn)
    st.caption("Open Conversation in the sidebar for full history; Applications for saved packs.")


def profiles():
    st.title("Profile workspace")
    st.caption("Create a profile or import a CV, then accept the version you want to use.")
    with st.expander("Create a profile", expanded=not workspace["profiles"]):
        with st.form("create_profile"):
            content = st.text_area("Candidate profile", SAMPLE_PROFILE, height=220)
            if st.form_submit_button("Save profile", type="primary"):
                result = api("POST", "/api/v1/profiles", body={"content": content})
                if result:
                    ws["profile"] = result["id"]
                    st.rerun()
    profile_id = choose("Profiles", workspace["profiles"], "profile")
    if not profile_id:
        return
    profile = api("GET", f"/api/v1/profiles/{profile_id}")
    if not profile:
        return
    st.write(
        f"Version **{profile['version']}** · Accepted version: "
        f"**{profile.get('validated_version') or 'not accepted'}**"
    )
    with st.form(f"edit-profile-{profile_id}-{profile['version']}"):
        text = st.text_area("Review and edit", profile["content"], height=240)
        if st.form_submit_button("Save new version"):
            if api(
                "PUT",
                f"/api/v1/profiles/{profile_id}",
                body={"expected_version": profile["version"], "content": text},
            ):
                st.rerun()
    confirmed = st.checkbox(
        "I reviewed this saved profile version", key=f"accept-{profile_id}-{profile['version']}"
    )
    if st.button("Accept saved profile", disabled=not confirmed, type="primary"):
        if api(
            "POST",
            f"/api/v1/profiles/{profile_id}/accept",
            body={"expected_version": profile["version"], "confirmed": True},
        ):
            st.rerun()
    if profile.get("structured_data"):
        result = api("GET", f"/api/v1/profiles/{profile_id}/evaluation")
        if result:
            st.write(f"Profile completeness: **{result['score']}/100**")
            st.dataframe(result["breakdown"], hide_index=True)
    version = st.number_input("Historical version", 1, profile["version"], value=1)
    if st.button("Read profile history"):
        result = api("GET", f"/api/v1/profiles/{profile_id}/versions/{version}")
        if result:
            inspect("Profile version", result)


def cv_imports():
    st.title("CV import and review")
    st.caption(
        "Text PDFs, up to 5 MiB and 10 pages. Simulated extraction reads Field: value lines."
    )
    st.download_button("Download sample CV", sample_pdf(), "sid-demo-cv.pdf", "application/pdf")
    with st.form("upload_cv"):
        file = st.file_uploader("Upload a CV", type=["pdf"])
        if st.form_submit_button("Import PDF", type="primary") and file is not None:
            result = api(
                "POST",
                f"/agent/parse-cv?language={language}",
                data=file.getvalue(),
                new_job=True,
            )
            if result:
                ws["cv"] = result["import_id"]
                st.rerun()
    import_id = choose("CV imports", workspace["cv_imports"], "cv")
    if not import_id:
        return
    cv = api("GET", f"/api/v1/cv-imports/{import_id}")
    if not cv:
        return
    st.write(f"Status: **{cv['status']}** · Version **{cv['version']}**")
    if cv.get("error_code"):
        st.error(cv["error_code"])
    if cv["status"] == "queued":
        st.info("The extraction worker is processing this PDF. Refresh to check progress.")
    if st.button("Refresh import"):
        st.rerun()
    for warning in cv.get("warnings", []):
        st.warning(warning)
    if cv["status"] in {"review", "accepted"}:
        rows = [{"field": f["field"], "value": f["value"]} for f in cv["facts"]]
        with st.form(f"cv-facts-{import_id}-{cv['version']}"):
            edited = st.data_editor(
                pd.DataFrame(rows, columns=["field", "value"]),
                num_rows="dynamic",
                hide_index=True,
                column_config={
                    "field": st.column_config.SelectboxColumn(
                        "Field",
                        options=[
                            "name",
                            "email",
                            "phone",
                            "location",
                            "headline",
                            "skill",
                            "education",
                            "experience",
                            "project",
                            "language",
                        ],
                        required=True,
                    )
                },
                width="stretch",
            )
            if st.form_submit_button("Save corrections", disabled=cv["status"] == "accepted"):
                facts = [
                    {"field": row["field"], "value": row["value"]}
                    for row in edited.to_dict("records")
                    if row.get("field") and row.get("value")
                ]
                if api(
                    "PUT",
                    f"/api/v1/cv-imports/{import_id}",
                    body={"expected_version": cv["version"], "facts": facts},
                ):
                    st.rerun()
        consent = st.checkbox(
            "I reviewed these saved CV facts", key=f"cv-consent-{import_id}-{cv['version']}"
        )
        if st.button(
            "Accept CV and create profile",
            disabled=not consent or cv["status"] == "accepted",
            type="primary",
        ):
            result = api(
                "POST",
                f"/api/v1/cv-imports/{import_id}/accept",
                body={"expected_version": cv["version"], "confirmed": True},
            )
            if result:
                ws["profile"] = result["profile_id"]
                st.rerun()
        if cv.get("profile_id"):
            st.success("Accepted. The profile is available in Profile workspace and Applications.")
        inspect("Page quotes, provenance and evaluation", cv)
        version = st.number_input("CV history version", 1, cv["version"], value=1)
        if st.button("Read CV history"):
            result = api("GET", f"/api/v1/cv-imports/{import_id}/versions/{version}")
            if result:
                inspect("Saved CV version", result)
    if st.checkbox("Delete this CV import and its source PDF", key=f"delete-cv-{import_id}"):
        if st.button("Delete CV import"):
            if api("DELETE", f"/api/v1/cv-imports/{import_id}"):
                st.rerun()


@st.cache_data
def sample_pdf():
    # A tiny text-only PDF, kept dependency-free and suitable for the real parser.
    lines = SAMPLE_PROFILE.splitlines()
    content = (
        "BT /F1 11 Tf 50 780 Td 16 TL "
        + " ".join(f"({line.replace('(', '').replace(')', '')}) Tj T*" for line in lines)
        + " ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream".encode(),
    ]
    pdf, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(pdf)


def applications():
    st.title("Application review")
    st.caption("Generate a letter or dossier, review evidence, then record approval.")
    accepted = [p for p in workspace["profiles"] if p.get("validated_version") == p["version"]]
    if accepted:
        corpus = json.loads(
            (Path(__file__).parent / "data/contracts/offers-v1.json").read_text("utf-8")
        )
        offers = {
            row["id"]: row
            for row in corpus["offers"]
            if row["status"] == "OPEN" and row["visibility"] == "PUBLIC"
        }
        with st.expander(
            "Generate a synthetic offer pack through /agent/generate-pack", expanded=True
        ):
            with st.form("synthetic_pack"):
                profile_id = st.selectbox("Accepted profile for pack", [p["id"] for p in accepted])
                offer_id = st.selectbox(
                    "Synthetic offer",
                    list(offers),
                    format_func=lambda key: f"{offers[key]['title']} · {offers[key]['location']}",
                )
                if st.form_submit_button("Queue synthetic pack", type="primary"):
                    profile = next(p for p in accepted if p["id"] == profile_id)
                    result = api(
                        "POST",
                        "/agent/generate-pack",
                        new_job=True,
                        body={
                            "profile_id": profile_id,
                            "profile_version": profile["version"],
                            "offer_id": offer_id,
                            "offer_version": offers[offer_id]["version"],
                            "language": language,
                        },
                    )
                    if result:
                        ws["draft"], ws["task"] = result["draft_id"], result["task_id"]
                        st.rerun()
    with st.expander("Generate an application", expanded=not workspace["drafts"]):
        if not accepted:
            st.info("Accept a profile first in Profile workspace or CV import.")
        else:
            with st.form("generate_draft"):
                profile_id = st.selectbox("Accepted profile", [p["id"] for p in accepted])
                kind = st.radio("Generate", ["Dossier", "Cover letter"], horizontal=True)
                job = st.text_area("Job description", SAMPLE_JOB, height=100)
                company = st.text_area("Company context", "Demo software company in Tunisia.")
                if st.form_submit_button("Queue generation", type="primary"):
                    profile = next(p for p in accepted if p["id"] == profile_id)
                    path = "application-dossiers" if kind == "Dossier" else "application-drafts"
                    result = api(
                        "POST",
                        f"/api/v1/{path}",
                        new_job=True,
                        body={
                            "profile_id": profile_id,
                            "profile_version": profile["version"],
                            "job_description": job,
                            "company_context": company,
                            "language": language,
                        },
                    )
                    if result:
                        ws["draft"], ws["task"] = result["draft_id"], result["task_id"]
                        st.rerun()
    draft_id = choose("Drafts", workspace["drafts"], "draft")
    if not draft_id:
        return
    draft = api("GET", f"/api/v1/application-drafts/{draft_id}")
    if not draft:
        return
    st.write(f"Status: **{draft['status']}** · Version **{draft['version']}**")
    if st.button("Refresh draft"):
        st.rerun()
    if ws.get("task"):
        task = api("GET", f"/api/v1/tasks/{ws['task']}")
        if task and task.get("draft_id") == draft_id:
            inspect("Generation task", task)
    if draft.get("error_code"):
        st.error(draft["error_code"])
    if not draft.get("cover_letter"):
        st.info("Generation runs in the background. Refresh in a moment.")
        return
    with st.form(f"letter-{draft_id}-{draft['version']}"):
        letter = st.text_area("Cover letter", draft["cover_letter"], height=280)
        if st.form_submit_button("Save letter revision"):
            if api(
                "PUT",
                f"/api/v1/application-drafts/{draft_id}",
                body={"expected_version": draft["version"], "cover_letter": letter},
            ):
                st.rerun()
    st.download_button("Download saved letter", draft["cover_letter"], "cover-letter.txt")
    analysis = draft.get("dossier_analysis")
    if analysis:
        st.subheader("Evidence matrix")
        st.dataframe(
            analysis["evidence"],
            hide_index=True,
            width="stretch",
            column_order=["requirement", "assessment", "candidate_quote", "job_quote"],
        )
        for suggestion in analysis.get("cv_suggestions", []):
            st.write(suggestion["suggestion"])
        with st.expander("Correct evidence"):
            st.caption("Keep requirements fixed. Quotes must occur in the pinned source texts.")
            with st.form(f"evidence-{draft_id}-{draft['version']}"):
                text = st.text_area(
                    "Evidence JSON", json.dumps(analysis["evidence"], indent=2), height=240
                )
                reason = st.text_input("Correction reason")
                if st.form_submit_button("Save evidence correction"):
                    try:
                        evidence = json.loads(text)
                    except ValueError:
                        st.error("Enter a valid JSON array.")
                    else:
                        if api(
                            "PUT",
                            f"/api/v1/application-drafts/{draft_id}/evidence",
                            body={
                                "expected_version": draft["version"],
                                "evidence": evidence,
                                "reason": reason,
                            },
                        ):
                            st.rerun()
    with st.form(f"approve-{draft_id}-{draft['version']}"):
        destination = st.text_input("Test destination ID", "demo-offer")
        consent = st.checkbox("I reviewed the saved letter, evidence and destination")
        submitted = st.form_submit_button("Record approval", type="primary")
        if submitted and not consent:
            st.error("Review the saved content and tick the confirmation before approval.")
        elif submitted:
            result = api(
                "POST",
                f"/api/v1/application-drafts/{draft_id}/approvals",
                body={
                    "expected_version": draft["version"],
                    "confirmed": True,
                    "destination_id": destination,
                },
            )
            if result:
                st.success("Approval recorded. Application submission is disabled.")
                inspect("Approval record", result)
    inspect("Sources, version and provider details", draft)
    version = st.number_input("Draft history version", 1, draft["version"], value=1)
    if st.button("Read draft history"):
        result = api("GET", f"/api/v1/application-drafts/{draft_id}/versions/{version}")
        if result:
            inspect("Saved draft version", result)


def preview():
    st.title("Quick letter preview")
    st.caption("Generate immediately without creating a saved profile or application.")
    with st.form("preview"):
        profile = st.text_area("Candidate profile", SAMPLE_PROFILE, height=180)
        job = st.text_area("Job description", SAMPLE_JOB)
        company = st.text_area("Company context", "Demo software company in Tunisia.")
        if st.form_submit_button("Generate preview", type="primary"):
            with st.spinner("Generating..."):
                result = api(
                    "POST",
                    "/api/v1/application-drafts/preview",
                    body={
                        "candidate_profile": profile,
                        "job_description": job,
                        "company_context": company,
                        "language": language,
                    },
                )
            if result:
                ws["preview"] = result
    if ws.get("preview"):
        st.text(ws["preview"]["cover_letter"])
        inspect("Preview response", ws["preview"])


def conversations():
    st.title("Recruitment conversation")
    st.caption(
        "Advice uses references. Offer search uses synthetic fixtures; "
        "recruiter matching is unavailable."
    )
    with st.expander("Start a conversation", expanded=not workspace["conversations"]):
        options = {"No profile": None}
        if DEMO_USERS[user] == "CANDIDATE":
            options.update(
                {
                    p["id"]: p
                    for p in workspace["profiles"]
                    if p.get("validated_version") == p["version"]
                }
            )
        selected = st.selectbox("Conversation profile", list(options))
        if st.button("New conversation"):
            payload = {"language": language}
            if options[selected]:
                payload.update(profile_id=selected, profile_version=options[selected]["version"])
            result = api("POST", "/api/v1/conversations", body=payload)
            if result:
                ws["conversation"] = result["id"]
                st.rerun()
    conversation_id = choose("Conversations", workspace["conversations"], "conversation")
    if not conversation_id:
        return
    history = api("GET", f"/api/v1/conversations/{conversation_id}")
    if not history:
        return
    st.caption(
        f"Revision {history['revision']} · Keywords: {', '.join(history.get('keywords', []))}"
    )
    for turn in history.get("turns", []):
        with st.chat_message("user"):
            st.write(turn["message"])
        with st.chat_message("assistant"):
            st.write(turn["response"]["text"])
            if turn["response"].get("results"):
                inspect("Synthetic offer results", turn["response"]["results"])
            if turn["response"].get("references"):
                inspect("References", turn["response"]["references"])
    message = st.chat_input("Ask about Python skills, a CV, or a career direction")
    if message:
        with st.spinner("Preparing a response..."):
            result = api(
                "POST",
                "/agent/chat",
                new_job=True,
                body={
                    "conversation_id": conversation_id,
                    "expected_revision": history["revision"],
                    "message": message,
                },
            )
        if result:
            st.rerun()
    if st.checkbox("Delete this conversation", key=f"delete-chat-{conversation_id}"):
        if st.button("Delete conversation history"):
            if api("DELETE", f"/api/v1/conversations/{conversation_id}"):
                st.rerun()


def knowledge():
    st.title("Reference search")
    st.caption(
        "Search a small public demo reference set. Similarity is not a compatibility percentage."
    )
    if st.button("Prepare demo reference index", type="primary"):
        with st.spinner("Importing reference records and building the local index..."):
            result = api("POST", "/demo/knowledge/setup")
        if result:
            st.success("Reference search is ready.")
            inspect("Index setup", result)
    with st.form("knowledge_search"):
        query = st.text_input("Search query", "Python software development")
        source = st.selectbox("Source", ["All", "esco", "rome", "onet", "geonames"])
        limit = st.slider("Maximum results", 1, 10, 5)
        if st.form_submit_button("Search references"):
            payload = {"query": query, "limit": limit}
            if source != "All":
                payload["source"] = source
            result = api("POST", "/api/v1/knowledge/search", body=payload)
            if result:
                ws["search"] = result
    if ws.get("search"):
        for record in ws["search"]["results"]:
            label = record["labels"].get(language) or next(iter(record["labels"].values()))
            st.subheader(label)
            st.caption(f"{record['source']} · {record['kind']} · {record.get('score', '')}")
            inspect("Reference and source metadata", record)
        if not ws["search"]["results"]:
            st.info("No references found.")
    if st.button("Check knowledge status"):
        result = api("GET", "/api/v1/knowledge/status")
        if result:
            st.json(result)


def operations():
    st.title("Operations")
    st.write("MongoDB queues and background workers are real in both AI modes.")
    if st.button("Refresh operations"):
        st.rerun()
    result = api("GET", "/api/v1/operations/status")
    if result:
        st.dataframe(
            [
                {"queue": name, **value["counts"], "expired_leases": value["expired_leases"]}
                for name, value in result["queues"].items()
            ],
            hide_index=True,
        )
        inspect("Provider circuits and configuration", result)
    readiness = api("GET", "/health/ready", quiet=True)
    if readiness:
        inspect("Readiness", readiness)
    st.info(
        "Synthetic offer search is enabled. Real private matching and submission remain disabled."
    )
    st.link_button("Open API documentation", base + "/docs")


with st.sidebar:
    st.title("SID Agent")
    st.caption("Local test workspace")
    base = os.getenv("SID_DEMO_API_URL", "http://127.0.0.1:8001").rstrip("/")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        st.error("The test console requires a localhost demo API URL.")
        st.stop()
    user = st.selectbox(
        "Test identity",
        list(DEMO_USERS),
        format_func=lambda value: f"{value.removeprefix('demo-').replace('-', ' ').title()}",
    )
    language = st.selectbox("Output language", ["en", "fr"])
    pages = {"Simulation": simulation, "Quick preview": preview}
    if DEMO_USERS[user] == "CANDIDATE":
        pages.update(
            {"Profile workspace": profiles, "CV import": cv_imports, "Applications": applications}
        )
    pages.update(
        {"Conversation": conversations, "Reference search": knowledge, "Operations": operations}
    )
    page = st.radio("Workspace", list(pages))
    st.caption(f"API: {base}")
    st.caption("No login. Synthetic users, separate sid_demo database.")

status = api("GET", "/demo/status", quiet=True)
if not status or status.get("demo") is not True:
    st.title("Start your local test workspace")
    st.info("The demo API is not available yet. Start the databases and demo server.")
    st.code(
        "uv run --group demo python -m scripts.simulation start",
        language="powershell",
    )
    st.button("Retry connection")
    st.stop()
if status["ai_mode"] == "simulated":
    st.warning("LOCAL DEMO · Authentication bypassed · AI responses are simulated")
else:
    st.warning("LOCAL DEMO · Authentication bypassed · Live AI calls use your provider account")
ws = st.session_state.setdefault(f"workspace:{user}", {})
workspace = api("GET", "/demo/workspace")
if workspace is None:
    st.stop()
pages[page]()
with st.sidebar:
    if "last_call" in st.session_state:
        with st.expander("Last API response"):
            st.json(st.session_state["last_call"])
