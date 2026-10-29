"""AI extraction step: one proposal in, one JSON record of stated facts out.

The model only reads and records. Code then checks every quote against the
source text and scans for hidden instructions. Scoring happens in score.py.

Setup:
    pip install -r requirements.txt
    cp .env.example .env            # add your API key
    # put each proposal's extracted text in samples/A.txt ... samples/E.txt
    # (samples/ is git-ignored: the brief asks us not to commit proposal text)

Usage:
    python extract.py --dry-run     # print the exact prompt for the first sample, no API call
    python extract.py               # extract all samples -> out/extracted/*.json
    python score.py --extractions out/extracted
"""
import argparse
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
PROMPT_VERSION = "extract-v1"

# Phrases that usually mean "text aimed at the AI or the reviewer, not a fact".
# A cheap second net in case the model misses one. Hits are flagged, never obeyed.
INJECTION_PATTERNS = [
    r"\b(ignore|disregard)\b.{0,40}\b(instruction|rule|criteria)",
    r"\b(automated|AI|LLM|language model)\b.{0,30}\b(review|system|evaluator|assistant)",
    r"\brank (this|it|us)\b.{0,20}\b(first|top|highest)",
    r"\b(full|maximum|perfect) (marks|score|points)\b",
    r"\bpre-?approved\b",
]

TYPE_TO_JSON = {"string": "string", "number": "number", "integer": "integer",
                "boolean": "boolean", "enum": "string"}


# ---------- prompt building ----------

def field_list(cfg):
    lines = []
    for f in cfg["extraction_fields"]:
        t = f["type"] + (f" - one of: {', '.join(f['options'])}" if f["type"] == "enum" else "")
        lines.append(f"- {f['id']} ({t}): {f['question']}")
    return "\n".join(lines)


def build_messages(cfg, proposal_id, filename, text, as_of):
    system = (HERE / "prompts/system.txt").read_text().strip()
    user = (HERE / "prompts/user.txt").read_text().strip()
    fills = {
        "category_name": cfg["category_name"],
        "project_description": cfg["project"]["description"],
        "as_of_date": as_of,
        "field_list": field_list(cfg),
        "proposal_id": proposal_id,
        "filename": filename,
        "proposal_text": text.strip(),
    }
    for k, v in fills.items():
        user = user.replace("{{" + k + "}}", str(v))
    return system, user


def tool_schema(cfg):
    """JSON schema generated from the admin's field list - nothing hard-coded."""
    props = {}
    for f in cfg["extraction_fields"]:
        value = {"type": [TYPE_TO_JSON[f["type"]], "null"]}
        if f["type"] == "enum":
            value["enum"] = f["options"] + [None]
        props[f["id"]] = {
            "type": "object",
            "properties": {
                "value": value,
                "status": {"type": "string", "enum": ["stated", "not_stated", "ambiguous"]},
                "evidence": {"type": ["string", "null"],
                             "description": "Exact quote from the proposal, max 40 words"},
                "page": {"type": ["string", "null"]},
                "note": {"type": "string"},
            },
            "required": ["value", "status", "evidence", "page", "note"],
        }
    return {
        "name": "record_extraction",
        "description": "Record the facts stated in one proposal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {"type": "object", "properties": props,
                           "required": list(props)},
                "embedded_instructions": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "location": {"type": "string"},
                        "quote": {"type": "string"},
                        "summary": {"type": "string"}},
                        "required": ["location", "quote", "summary"]}},
            },
            "required": ["fields", "embedded_instructions"],
        },
    }


# ---------- checks done by code after the AI call ----------

def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def verify_evidence(record, text):
    """A quote must appear word for word in the document, or the field is flagged."""
    doc = norm(text)
    for f in record["fields"].values():
        if f.get("status") in ("stated", "ambiguous"):
            f["evidence_verified"] = bool(f.get("evidence")) and norm(f["evidence"]) in doc
    return record


def scan_injection(record, text):
    found = {norm(e.get("quote", "")) for e in record["embedded_instructions"]}
    for line in text.splitlines():
        if any(re.search(p, line, re.I) for p in INJECTION_PATTERNS):
            if not any(norm(line) in q or q in norm(line) for q in found if q):
                record["embedded_instructions"].append(
                    {"location": "rule scan", "quote": line.strip()[:200],
                     "summary": "Matched an instruction-like pattern (caught by code, not the model)"})
    return record


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default=HERE / "criteria/commercial-solar.v1.json")
    ap.add_argument("--samples", default=HERE / "samples")
    ap.add_argument("--out", default=HERE / "out/extracted")
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = json.loads(Path(a.criteria).read_text())
    files = sorted(Path(a.samples).glob("*.txt"))
    if not files:
        raise SystemExit(f"No samples found in {a.samples}/ (expected A.txt ... E.txt)")

    if a.dry_run:
        system, user = build_messages(cfg, files[0].stem, files[0].name,
                                      files[0].read_text(), a.as_of)
        print("=== SYSTEM ===\n" + system + "\n\n=== USER ===\n" + user)
        print("\n=== TOOL SCHEMA ===\n" + json.dumps(tool_schema(cfg), indent=2))
        return

    from dotenv import load_dotenv
    import anthropic
    load_dotenv(HERE / ".env")
    client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    criteria_version = f"{cfg['category_id']}.v{cfg['version']}"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = HERE / "out/cache"
    cache.mkdir(parents=True, exist_ok=True)

    for path in files:
        text = path.read_text()
        key = hashlib.sha256("|".join(
            [text, PROMPT_VERSION, model, criteria_version, a.as_of]).encode()).hexdigest()[:16]
        cached = cache / f"{key}.json"
        if cached.exists():                  # same inputs -> same answer, no second AI call
            record = json.loads(cached.read_text())
            print(f"{path.stem}: cached")
        else:
            system, user = build_messages(cfg, path.stem, path.name, text, a.as_of)
            resp = client.messages.create(
                model=model, max_tokens=4000, temperature=0, system=system,
                tools=[tool_schema(cfg)],
                tool_choice={"type": "tool", "name": "record_extraction"},
                messages=[{"role": "user", "content": user}])
            raw = next(b.input for b in resp.content if b.type == "tool_use")
            record = {"proposal": path.stem, "source": path.name,
                      "prompt_version": PROMPT_VERSION, "model": model,
                      "criteria_version": criteria_version, "cache_key": key, **raw}
            cached.write_text(json.dumps(record, indent=2))
            print(f"{path.stem}: extracted ({resp.usage.input_tokens} in / "
                  f"{resp.usage.output_tokens} out tokens)")

        record = scan_injection(verify_evidence(record, text), text)
        (out / f"{path.stem}.json").write_text(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
