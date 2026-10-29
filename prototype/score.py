"""Rules engine: turns extracted facts into gates, scores, flags and a ranking.

No AI here. Same extraction + same criteria version => same output, every time.

Usage:
    python score.py                                   # uses the hand-checked fixtures
    python score.py --extractions out/extracted       # uses a live extraction run
    python score.py --out ../ranking.json --report out/report.md
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).parent


# ---------- small helpers ----------

def val(ext, field):
    """Return (value, status) for a field; missing fields count as not stated."""
    f = ext["fields"].get(field) or {"value": None, "status": "not_stated"}
    return f.get("value"), f.get("status", "not_stated")


def check(ext, c):
    """One mandatory check -> 'pass' | 'fail' | 'unclear'."""
    v, status = val(ext, c["field"])
    if status == "ambiguous":
        return "unclear"
    if c["op"] == "present":
        return "pass" if v not in (None, "") else "fail"
    if v is None:
        return "fail"  # the criterion asks for it to be stated in the proposal
    ops = {"==": v == c["value"], ">=": v >= c["value"], "<=": v <= c["value"]}
    return "pass" if ops[c["op"]] else "fail"


def linear(v, zero_at, full_at):
    """0..100 on a straight line; works for higher-is-better and lower-is-better."""
    if v is None:
        return 0.0
    t = (v - zero_at) / (full_at - zero_at)
    return max(0.0, min(1.0, t)) * 100


def score_part(ext, p):
    v, _ = val(ext, p["field"])
    return linear(v, p["zero_at"], p["full_at"])


# ---------- one proposal ----------

def evaluate(ext, cfg, as_of_year):
    r = {"proposal": ext["proposal"], "gates": [], "constraints": [], "criteria": [],
         "flags": [], "notes": []}

    # 1. Mandatory pass/fail gates
    for g in cfg["mandatory"]:
        results = [check(ext, c) for c in g["checks"]]
        outcome = "fail" if "fail" in results else "unclear" if "unclear" in results else "pass"
        r["gates"].append({"id": g["id"], "name": g["name"], "result": outcome,
                           "detail": "; ".join(n[0].lower() + n[1:] for n in (val_note(ext, c["field"]) for c in g["checks"]) if n)})
        if outcome == "unclear":
            r["flags"].append(f"Mandatory check '{g['name']}' is unclear")

    # 2. Project constraints (budget ceiling, deadline)
    for c in cfg["project"]["constraints"]:
        v, _ = val(ext, c["field"])
        if v is None:
            res = "unknown"
            note = val_note(ext, c["field"])
            r["flags"].append(f"'{c['label']}' can't be confirmed"
                              + (f" ({note[0].lower() + note[1:]})" if note else " (not stated)"))
        else:
            res = "pass" if check(ext, c) == "pass" else "fail"
        r["constraints"].append({"id": c["id"], "label": c["label"], "result": res})

    # 3. Integrity: instructions hidden in the document
    for e in ext.get("embedded_instructions", []):
        r["flags"].append(f"hidden instruction to automated reviewers at {e['location']} "
                          "(ignored by scoring; a person must decide what to do about it)")

    # 4. Evidence that could not be found in the source text (live runs only)
    for fid, f in ext["fields"].items():
        if f.get("evidence_verified") is False:
            r["flags"].append(f"Evidence for '{fid}' not found in the source document")

    # 5. Weighted criteria
    total = 0.0
    for w in cfg["weighted"]:
        m = w["method"]
        if m in ("scale_up", "scale_down"):
            raw = score_part(ext, w)
        elif m == "average":
            raw = sum(score_part(ext, p) for p in w["parts"]) / len(w["parts"])
        elif m == "checklist":
            raw = 100 * sum(1 for f in w["fields"] if val(ext, f)[0] is True) / len(w["fields"])
        else:
            raise ValueError(f"unknown method {m}")
        pts = raw * w["weight"] / 100
        total += pts
        r["criteria"].append({"id": w["id"], "name": w["name"], "raw": round(raw, 1),
                              "points": round(pts, 2), "max": w["weight"]})

        # non-blocking notes for the reviewer
        if "window_field" in w:
            since, _ = val(ext, w["window_field"])
            start = as_of_year - w["window_years"]
            if since is not None and since < start:
                r["notes"].append(f"{w['name']}: count covers {since} onward, but the window "
                                  f"starts {start}; some projects may be too old (reviewer to confirm)")
        fields = w.get("fields") or [w.get("field")] + [p["field"] for p in w.get("parts", [])]
        for fid in filter(None, fields):
            v, status = val(ext, fid)
            if status == "ambiguous":
                r["notes"].append(f"{w['name']}: '{fid}' is ambiguous - {val_note(ext, fid)}")

    # 6. Status
    failed = [g["name"] + (f" ({g['detail']})" if g["detail"] else "")
              for g in r["gates"] if g["result"] == "fail"]
    failed += [c["label"] for c in r["constraints"] if c["result"] == "fail"]
    if failed:
        r["status"], r["score"] = "ineligible", None
        r["failed"] = failed
    else:
        r["status"] = "flagged" if r["flags"] else "ranked"
        r["score"] = round(total, 1)
    r["points"] = {c["id"]: c["points"] for c in r["criteria"]}
    return r


def val_note(ext, field):
    return (ext["fields"].get(field) or {}).get("note", "")


# ---------- the batch ----------

def rank(results, cfg):
    def key(r):
        k = [-r["score"]]
        for t in cfg["tie_break"]:
            if t == "proposal_id":
                k.append(r["proposal"])
            elif t == "price":
                k.append(-r["points"]["price"])
            else:
                k.append(-r["points"][t])
        return k

    ranked = sorted([r for r in results if r["status"] == "ranked"], key=key)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for r in results:
        if r["status"] != "ranked":
            r["rank"] = None
        if r["status"] == "flagged":
            # where it would land if a reviewer clears the flag
            r["would_rank"] = 1 + sum(1 for x in ranked if key(x) < key(r))
    return results


def reason(r, cfg):
    names = {"experience": "Experience", "price": "Price", "timeline": "Timeline",
             "warranty": "Warranty", "team": "Team"}
    parts = " + ".join(f"{names.get(c['id'], c['name'])} {c['points']:.1f}/{c['max']}"
                       for c in r["criteria"])
    if r["status"] == "ineligible":
        return "Ineligible: fails mandatory " + "; ".join(r["failed"]) + "."
    if r["status"] == "flagged":
        f = r["flags"][0]
        return (f"Flagged: {f[0].lower() + f[1:]}. Provisional score {r['score']:.1f} "
                f"would place it {ordinal(r['would_rank'])} if a reviewer clears it.")
    return f"All mandatory checks passed; {r['score']:.1f} = {parts}."


def ordinal(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default=HERE / "criteria/commercial-solar.v1.json")
    ap.add_argument("--extractions", default=HERE / "fixtures/extracted")
    ap.add_argument("--as-of-year", type=int, default=2026)
    ap.add_argument("--out", default=HERE / "out/ranking.json")
    ap.add_argument("--report", default=HERE / "out/report.md")
    a = ap.parse_args()

    cfg = json.loads(Path(a.criteria).read_text())
    exts = [json.loads(p.read_text()) for p in sorted(Path(a.extractions).glob("*.json"))]
    results = rank([evaluate(e, cfg, a.as_of_year) for e in exts], cfg)

    order = {"ranked": 0, "flagged": 1, "ineligible": 2}
    results.sort(key=lambda r: (order[r["status"]], r["rank"] or 99, -(r["score"] or 0), r["proposal"]))
    out = [{"proposal": r["proposal"], "rank": r["rank"], "status": r["status"],
            "score": r["score"], "reason": reason(r, cfg)} for r in results]

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=2) + "\n")
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(report(results, cfg))
    print(json.dumps(out, indent=2))


def report(results, cfg):
    lines = [f"# Score breakdown - {cfg['category_name']} (criteria v{cfg['version']})", ""]
    for r in results:
        lines.append(f"## Proposal {r['proposal']} - {r['status']}"
                     + (f", rank {r['rank']}" if r["rank"] else "")
                     + (f", score {r['score']}" if r["score"] is not None else ""))
        lines.append("")
        lines.append("| Check | Result |\n|---|---|")
        for g in r["gates"]:
            lines.append(f"| {g['name']} (mandatory) | {g['result']} |")
        for c in r["constraints"]:
            lines.append(f"| {c['label']} (project limit) | {c['result']} |")
        lines.append("")
        if r["score"] is not None:
            lines.append("| Criterion | Raw 0-100 | Points |\n|---|---|---|")
            for c in r["criteria"]:
                lines.append(f"| {c['name']} | {c['raw']} | {c['points']:.2f} / {c['max']} |")
            lines.append("")
        for f in r["flags"]:
            lines.append(f"- FLAG: {f}")
        for n in r["notes"]:
            lines.append(f"- Note: {n}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
