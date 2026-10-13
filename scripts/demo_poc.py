"""End-to-end demonstration of spec §27 against the running stack (docker compose up).

    python -m scripts.demo_poc [--api http://localhost:8000] [--wait-minutes 10]

1. Plays three Payment Pending cases through the HTTP API (engineer asks the agent; an operator runs
   tools on the case; the case is resolved with an outcome note) plus one noise case.
2. Waits for the worker's Memory Writer to retain each case (and skip the noise).
3. Waits for Hindsight to consolidate an observation across the cases.
4. Opens a NEW session and asks what to investigate first - the answer must combine official
   procedure (RAG) with learned experience (Hindsight), with provenance visible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from hindsight_client import Hindsight

from app.config import get_settings

CASES = json.loads((Path(__file__).resolve().parents[1] / "data" / "cases" / "poc_cases.json").read_text("utf-8"))


def banner(t: str) -> None:
    print(f"\n{'=' * 100}\n{t}\n{'=' * 100}")


async def main(api: str, wait_minutes: int, api_key: str | None) -> None:
    headers = {"X-API-Key": api_key} if api_key else {}
    run = int(time.time())
    async with httpx.AsyncClient(base_url=api, headers=headers, timeout=300) as http:
        (await http.get("/health/ready")).raise_for_status()
        sessions = {}
        for case in CASES["cases"]:
            banner(f"CASE {case['name']}")
            (await http.post("/v1/sim/reset", json=case["sim"])).raise_for_status()
            conv = f"demo-{run}-{case['name']}"
            reply = None
            for msg in case["messages"]:
                print(f"\nENGINEER: {msg}")
                reply = (await http.post("/v1/messages", json={"conversation_id": conv, "text": msg})).json()
                print(f"\nAGENT:\n{reply['answer']}")
            sid = reply["session_id"]
            sessions[case["name"]] = sid
            for t in case["operator_tools"]:
                r = (await http.post(f"/v1/sessions/{sid}/tools", json=t)).json()
                print(f"\nOPERATOR ran [{r['cite']}] {t['name']}({t['arguments']}) -> {r['status']}"
                      f"{': ' + str(r['output']) if r['output'] else ''}{': ' + r['error'] if r['error'] else ''}")
            closed = (await http.post(f"/v1/sessions/{sid}/resolve", json={"note": case["resolve_note"]})).json()
            print(f"\nCASE CLOSED -> {closed['status']}")

        banner("MEMORY WRITER: waiting for the worker to process closed cases")
        deadline = time.time() + wait_minutes * 60
        pending = dict(sessions)
        while pending and time.time() < deadline:
            for name, sid in list(pending.items()):
                job = (await http.get(f"/v1/sessions/{sid}")).json()["memory_job"]
                if job and job["status"] in ("done", "skipped", "failed"):
                    print(f"  {name}: {job['status']} - {job['reason']}"
                          f"{' (document ' + job['document_id'] + ')' if job.get('document_id') else ''}")
                    pending.pop(name)
            await asyncio.sleep(3)
        if pending:
            print(f"  still pending after {wait_minutes} min: {list(pending)}")

        banner("HINDSIGHT: waiting for consolidation into observations")
        s = get_settings()
        hs = Hindsight(base_url=s.hindsight_url, timeout=60)
        found = []
        while time.time() < deadline:
            r = await hs.arecall(bank_id=s.hindsight_bank_id, query="payment pending consumer restart",
                                 types=["observation"])
            found = [(x.text, len(x.source_fact_ids or [])) for x in r.results
                     if "consumer" in x.text.lower() and "restart" in x.text.lower()]
            if found:
                break
            await asyncio.sleep(10)
        for text, n in found:
            print(f"  OBSERVATION (from {n} facts): {text}")
        if not found:
            print("  no matching observation yet - consolidation may still be running (see UI on :9999)")
        await hs.aclose()

        banner("NEW SESSION")
        (await http.post("/v1/sim/reset", json={"stalled": True, "pending": 2})).raise_for_status()
        q = CASES["new_session_question"]
        print(f"ENGINEER: {q}")
        reply = (await http.post("/v1/messages", json={"conversation_id": f"demo-{run}-new", "text": q})).json()
        print(f"\nAGENT:\n{reply['answer']}")
        print(f"\nverification: {reply['verification']}")
        print(f"sources: {json.dumps(reply['sources'], indent=2)}")
        print(f"\nfull trace: GET {api}/v1/sessions/{reply['session_id']}/trace")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--wait-minutes", type=int, default=10)
    p.add_argument("--api-key", default=None)
    a = p.parse_args()
    asyncio.run(main(a.api, a.wait_minutes, a.api_key))
