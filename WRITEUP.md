# Write-up: AI Proposal Triage

**In one line:** AI reads each proposal and records its facts with quotes; code applies the admin's criteria; a person resolves flags and makes the award. Flow: [diagram/process-flow.png](diagram/process-flow.png).

## 1. AI vs. rules

**AI (one step): extraction.** Proposals are free text that say the same thing many ways ("all-in", "licensed subcontractor, to be confirmed"). That needs language understanding, so one AI call per proposal turns it into a fixed list of facts, each with an exact quote.

**Code and rules (everything else):** PDF-to-text, quote checking, a scan for hidden instructions, mandatory gates, project limits (budget, 16 weeks), weighted scores, ranking, tie-breaks, the one-line reasons and the audit log.

**Why there:** eligibility and scoring are policy. Policy must do exactly what the admin configured, repeat exactly, and be explainable formula by formula; "score this out of 100" asked of an AI is none of those. "What price does it state, and where?" is narrow and checkable: the quote is in the document or it is not.

## 2. Consistency and explainability

- **Scores are formulas** over stored facts, so the same facts always give the same score.
- **Extraction is pinned:** fixed model version, fixed prompt version, temperature 0, forced JSON output, one proposal per call.
- **Results are cached** by document hash + prompt + model + criteria version, so ranking the same proposals twice gives **an identical result**. A deliberate re-extraction (new model or prompt) is a new logged version; changed facts are shown as a diff before they count.
- **Absolute scales**, not "relative to the cheapest bid" (price: full marks at $195k or less, zero at the $260k ceiling), so a score never moves because another bid was added or disqualified.
- **The reviewer sees** each gate's result and each criterion's points, formula and deciding quote, plus flags and notes. The one-line reason shows the arithmetic, e.g. *A: 76.0 = Experience 30/30 + Price 8.5/25 + Timeline 10/15 + Warranty 12.5/15 + Team 15/15.*

## 3. Human in the loop

1. **After upload:** the reviewer confirms the category and that every file became readable text (scanned PDFs go through OCR and are flagged).
2. **Review screen:** nothing flagged is ranked automatically. The reviewer resolves each flag (clear, exclude, or ask the bidder to clarify), can correct any extracted fact by pointing to the right passage (code re-scores instantly), and can override a gate or position only with a written reason.
3. **Award:** the ranking is a recommendation. A named person records the award, which may differ from rank 1 with a reason.

**Audit log (append-only):** uploader and file hashes; criteria, model and prompt versions; raw AI output; quote-check results; every correction (old value, new value, who, why); flag resolutions; overrides with reasons; every scoring run; the award and approver. Any ranking can be reproduced from the log.

## 4. Changing criteria

- Criteria are **versioned**: an edit creates v2 and v1 never changes. Each scoring run records its version, so old results stay explainable.
- Open batches show **"criteria changed since scoring"** with old and new scores side by side. A reviewer chooses to re-score; nothing changes silently.
- **Weights or formulas changed:** re-score from stored facts. Instant, no AI cost.
- **New or reworded question:** only that field is re-extracted for each proposal.
- After an award the batch is **locked** to its version. Mid-evaluation edits are logged with who and why, since changing criteria after seeing bids can be used to favour a bidder.

## 5. Risks and safeguards

| Risk | How the design guards against it |
|---|---|
| **Wrong extraction** (misread number, wrong field) | Every value has a verbatim quote; code checks the quote exists; reviewer sees value and quote side by side and can correct it. |
| **Made-up facts** | "Not stated" is a valid answer; invented quotes fail the check; missing data is never guessed. D's timeline stays empty and is flagged, not filled in. |
| **Manipulation / prompt injection** | Proposal text is wrapped as untrusted data. The AI has no tools and no say over scores, so obeying an injection could not change the rank. A code pattern scan is a second net; any hit flags the bid for a person (C). Hidden white text is extracted too, so it is caught the same way. |
| **Bias** | The AI never judges quality, so polished writing or a known brand earns nothing. Same formulas for every bid. Criteria can be biased too (experience thresholds hurt newer firms), so they are reviewed at setup. |
| **Vague criteria** | "Monitoring plan" could mean system monitoring or progress reports (C). Admin writes a precise question per criterion; ambiguous answers surface as reviewer notes. |
| **Over-trust** | Flagged bids are never auto-ranked; the award needs a named approver. |
| **Cost** | One AI call per proposal, caching, re-scoring without AI, upload size limits. |
| **Confidential bids** | API with no training on customer data, retention limits, project-team access only. |

## Assumptions

- The **$260k ceiling and 16-week limit are hard project limits**: breaking one makes a bid ineligible; not stating one gets a flag (D).
- **"Licence held by the bidder"** means a subcontractor's licence does not pass (B).
- **"Last 5 years"** is counted from 2021 because proposals give years only. A count starting earlier (C, "since 2020") is scored as stated, with a note for the reviewer.
- **Scale anchors** (experience full at 10 projects, price at $195k, timeline at 10 weeks, workmanship at 15 years) are admin settings I chose and would confirm.
- **"Monitoring"** means performance monitoring after installation, not construction progress reports.

Result on the sample set (A 1st, E 2nd, C and D flagged, B ineligible): [ranking.json](ranking.json), explained in [README.md](README.md).
