"""Random-scenario battery for the Pinflow agent loop.

COSTS REAL LLM TOKENS (one multi-turn Opus conversation per scenario) and
COMMITS to whatever schematic KiCad has open — point KiCad at a disposable
sandbox project before running.

Usage: cd services/api && .venv/bin/python scripts/scenario_battery.py

Drives varied realistic conversations through the live SSE API, auto-answering
gates, attaching datasheets when the agent asks, and classifying outcomes:

  CLEAN    — conversation ended normally, no breaker/max-turns, file intact
  DEGRADED — ended normally but a tool reported a limitation it admitted
  SPIRAL   — the repeated-failure breaker tripped
  MAXTURNS — hit the turn cap
  BROKEN   — schematic file no longer parses (restored from backup)
  ERROR    — transport/LLM error

After each scenario: discard any leftover stage, validate the .kicad_sch
still parses (restore from per-scenario backup if not).
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:8787"
SCH = None  # resolved from /kicad/active-project at startup
KCLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"

SCENARIOS = [
    {"name": "replicate-ldo",
     "text": "Add a second AP2112K-3.3 LDO just like the existing one, fed from the same +5V, but call its output +3V3_B."},
    {"name": "led-indicator",
     "text": "Add a power-indicator LED on +3V3 with a 1k series resistor to GND."},
    {"name": "edit-pullup",
     "text": "Change the EN pull-up resistor on the LDO to 47k."},
    {"name": "whats-here",
     "text": "What's currently in my schematic? Anything look wrong?"},
    {"name": "run-erc",
     "text": "Run ERC and summarize what needs fixing."},
    {"name": "remove-stub",
     "text": "Remove the CC pulldown resistors from the USB connector."},
    {"name": "vague-ask",
     "text": "i want usb power but like 1.8 volts for my sensor idk"},
    {"name": "boost-cold-datasheet",
     "text": "Add an MT3608 boost converter taking +5V up to 12V at 1A.",
     "pdf": "/tmp/MT3608.pdf", "pdf_mpn": "MT3608"},
    {"name": "npn-switch",
     "text": "Add a low-side NPN switch: SOT-23 NPN, base driven through a 1k resistor from a net called CTRL, collector to a net called LOAD, emitter to GND."},
    {"name": "weird-rails",
     "text": "Add a 100nF decoupling capacitor between a net called MYRAIL_X and a net called RETURNX. Both are power rails on my board."},
]

MAX_RESUMES = 6
MAX_FOLLOWUPS = 2


def sse_events(resp):
    kind = None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            kind = line[7:].strip()
        elif line.startswith("data: ") and kind:
            try:
                yield kind, json.loads(line[6:])
            except json.JSONDecodeError:
                pass
            kind = None


def stream(client, path, body):
    """POST an SSE endpoint, return (events, error)."""
    events = []
    try:
        with client.stream("POST", API + path, json=body, timeout=600) as r:
            for kind, data in sse_events(r):
                events.append((kind, data))
    except Exception as e:
        return events, f"{type(e).__name__}: {e}"
    return events, None


def pick_answer(question, options):
    opts = [str(o) for o in (options or [])]
    for want in ("Confirm", "Proceed", "Yes"):
        for o in opts:
            if o.strip().lower().startswith(want.lower()):
                return o
    if opts:
        return opts[0]
    return "Go ahead with whatever you think is best."


def wants_datasheet(events):
    texts = [d.get("text", "") for k, d in events if k == "ai"]
    blob = " ".join(texts).lower()
    return ("datasheet" in blob and ("attach" in blob or "pdf" in blob))


def file_parses():
    cp = subprocess.run(
        [KCLI, "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", "/tmp/_probe.net", str(SCH)],
        capture_output=True, timeout=120)
    return Path("/tmp/_probe.net").is_file() and cp.returncode == 0


def run_scenario(client, sc):
    backup = Path(f"/tmp/backup_{sc['name']}.kicad_sch")
    shutil.copy(SCH, backup)
    summary = {"name": sc["name"], "events": 0, "resumes": 0, "followups": 0,
               "suspensions": [], "system": [], "errors": []}

    events, err = stream(client, "/agent/chat", {"user_text": sc["text"]})
    if err:
        summary["errors"].append(err)
    conv = next((d.get("conversation_id") for k, d in events if k == "meta"), None)
    summary["conv"] = conv

    pdf_sent = False
    while True:
        summary["events"] += len(events)
        for k, d in events:
            if k == "system":
                summary["system"].append(d.get("text", ""))

        suspended = any(k == "suspended" for k, _ in events)
        if suspended and summary["resumes"] < MAX_RESUMES:
            q = next((d for k, d in reversed(events)
                      if k == "ai" and d.get("questions")), None)
            qq = (q or {}).get("questions") or [{}]
            question = qq[0].get("q", "")
            options = qq[0].get("options", [])
            ans = pick_answer(question, options)
            summary["suspensions"].append(
                f"Q: {question[:70]} -> {ans[:40]}")
            events, err = stream(client, "/agent/chat/resume",
                                 {"conversation_id": conv, "answer": ans})
            summary["resumes"] += 1
            if err:
                summary["errors"].append(err)
                break
            continue

        if (not suspended and sc.get("pdf") and not pdf_sent
                and wants_datasheet(events)
                and summary["followups"] < MAX_FOLLOWUPS):
            with open(sc["pdf"], "rb") as f:
                up = client.post(API + "/agent/attachments",
                                 data={"conversation_id": conv},
                                 files={"files": (Path(sc["pdf"]).name, f,
                                                  "application/pdf")},
                                 timeout=120)
            aid = up.json()["attachments"][0]["attachment_id"]
            pdf_sent = True
            events, err = stream(client, "/agent/chat", {
                "user_text": f"here is the {sc.get('pdf_mpn','')} datasheet",
                "conversation_id": conv,
                "attachment_ids": [aid]})
            summary["followups"] += 1
            if err:
                summary["errors"].append(err)
                break
            continue
        break

    # post-scenario hygiene: drop any leftover stage so it can't leak forward
    try:
        client.post(API + "/schematic/discard",
                    json={"schematic_path": str(SCH)}, timeout=30)
    except Exception:
        pass

    broken = not file_parses()
    if broken:
        shutil.copy(backup, SCH)

    sys_blob = " ".join(summary["system"])
    if broken:
        verdict = "BROKEN"
    elif summary["errors"]:
        verdict = "ERROR"
    elif "max turns" in sys_blob:
        verdict = "MAXTURNS"
    elif "Stopped:" in sys_blob:
        verdict = "SPIRAL"
    else:
        verdict = "CLEAN"
    summary["verdict"] = verdict
    return summary


def main():
    global SCH
    client = httpx.Client()
    proj = client.get(API + "/kicad/active-project", timeout=30).json()
    if not proj.get("detected") or not proj.get("schematic_path"):
        sys.exit("No active KiCad project detected — open a DISPOSABLE "
                 "sandbox project in KiCad first (commits write to it).")
    SCH = Path(proj["schematic_path"])
    print(f"target schematic: {SCH}  (scenarios will commit to this file!)",
          flush=True)
    results = []
    for sc in SCENARIOS:
        t0 = time.time()
        s = run_scenario(client, sc)
        s["secs"] = round(time.time() - t0, 1)
        results.append(s)
        line = (f"[{s['verdict']:8s}] {s['name']:22s} conv={s.get('conv')} "
                f"resumes={s['resumes']} followups={s['followups']} {s['secs']}s")
        print(line, flush=True)
        for x in s["system"]:
            print(f"    system: {x[:140]}", flush=True)
        for x in s["errors"]:
            print(f"    error: {x[:140]}", flush=True)
        for x in s["suspensions"]:
            print(f"    gate: {x}", flush=True)
    Path("/tmp/scenario_results.json").write_text(json.dumps(results, indent=1))
    bad = [r for r in results if r["verdict"] not in ("CLEAN",)]
    print(f"\nDONE: {len(results)} scenarios, "
          f"{len(results) - len(bad)} clean, {len(bad)} flagged", flush=True)


if __name__ == "__main__":
    main()
