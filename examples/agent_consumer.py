"""Local Node-consumer equivalent using synthetic data and the existing demo workers."""

import argparse
import asyncio
import io
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def sample_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 20 TL 50 740 Td (Name: Synthetic Candidate) Tj T* "
        b"(Skill: Python) Tj T* (Skill: SQL) Tj T* "
        b"(Project: University web project) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


async def wait_for(client, url, state, ready):
    async with asyncio.timeout(60):
        while True:
            response = await client.get(url)
            response.raise_for_status()
            record = response.json()
            if record[state] in ready:
                return record
            if record[state] == "failed":
                raise RuntimeError(f"Synthetic job failed: {record.get('error_code')}")
            await asyncio.sleep(0.5)


async def run(base_url, *, transport=None, event_hooks=None, verbose=True):
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This example only uses the explicit localhost synthetic demo")
    headers = {"X-Service-Key": "sid-local-demo-only", "X-Demo-User": "demo-candidate"}
    prefix = "consumer-" + uuid4().hex
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=100,
        transport=transport,
        event_hooks=event_hooks,
        trust_env=False,
    ) as client:
        status = await client.get("/demo/status")
        status.raise_for_status()
        if status.json()["ai_mode"] != "simulated":
            raise ValueError("Restart app.demo in simulated mode before running fixture checks")
        imported = await client.post(
            "/agent/parse-cv",
            content=sample_pdf(),
            headers={"Content-Type": "application/pdf", "Idempotency-Key": prefix + "-cv"},
        )
        imported.raise_for_status()
        receipt = imported.json()
        review = await wait_for(client, receipt["poll_url"], "status", {"review"})
        assert review["requires_human_approval"] and review["facts"]
        accepted = await client.post(
            receipt["poll_url"] + "/accept",
            json={"expected_version": review["version"], "confirmed": True},
        )
        accepted.raise_for_status()
        profile_id = accepted.json()["profile_id"]
        conversation = await client.post(
            "/api/v1/conversations",
            json={"language": "fr", "profile_id": profile_id, "profile_version": 1},
        )
        conversation.raise_for_status()
        chat_payload = {
            "conversation_id": conversation.json()["id"],
            "expected_revision": 0,
            "message": "Je cherche un stage PFE Python à Sfax",
        }
        turn = await client.post(
            "/agent/chat", json=chat_payload, headers={"Idempotency-Key": prefix + "-chat"}
        )
        turn.raise_for_status()
        replay = await client.post(
            "/agent/chat", json=chat_payload, headers={"Idempotency-Key": prefix + "-chat"}
        )
        replay.raise_for_status()
        assert replay.json() == turn.json()
        assert [row["id"] for row in turn.json()["response"]["results"]] == ["fixture-python-pfe"]
        pack = await client.post(
            "/agent/generate-pack",
            json={
                "profile_id": profile_id,
                "profile_version": 1,
                "offer_id": "fixture-python-pfe",
                "offer_version": 1,
                "language": "fr",
            },
            headers={"Idempotency-Key": prefix + "-pack"},
        )
        pack.raise_for_status()
        await wait_for(client, pack.json()["poll_url"], "state", {"done"})
        draft = await client.get(pack.json()["draft_url"])
        draft.raise_for_status()
        assert draft.json()["requires_human_approval"]
        assert receipt["sync_status"] == "not_integrated"
        assert not turn.json()["submitted"] and not pack.json()["submitted"]
        if verbose:
            print(
                "PASS: synthetic CV review/acceptance, offer search/replay and pack worker handoff."
            )
            print("Profile sync, real private matching and submission remain disabled.")
        return {
            "profile_id": profile_id,
            "conversation_id": conversation.json()["id"],
            "import_id": receipt["import_id"],
            "draft_id": pack.json()["draft_id"],
            "turn": turn.json(),
            "pack": pack.json(),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    asyncio.run(run(args.base_url))


if __name__ == "__main__":
    main()
