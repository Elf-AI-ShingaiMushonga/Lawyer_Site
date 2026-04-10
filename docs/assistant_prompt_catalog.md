# Assistant Prompt Catalog

This catalog frames the assistant from a working lawyer's perspective.

Status meanings:
- `Supported`: the assistant can handle the prompt directly today.
- `Partial`: the assistant can help materially, but not complete the full workflow end-to-end.
- `Native workflow`: the assistant keeps this in the underlying product workflow for safety or because the capability is not yet implemented directly.

## Matter Intelligence

| Prompt | Status |
| --- | --- |
| "Where do things stand on this matter?" | Supported |
| "What are the next deadlines on this matter?" | Supported |
| "What should I focus on next?" | Supported |
| "Summarize this matter for partner review." | Supported |
| "Build a chronology of this matter." | Supported |
| "Show me the recent matter activity." | Supported |
| "Show me the recent notes and documents on this file." | Supported |
| "Show me the key parties and their roles." | Supported |
| "Show me the recent client communications on this file." | Supported |
| "What changed on this matter recently?" | Partial |

## Case Construction And Legal Analysis

| Prompt | Status |
| --- | --- |
| "Construct the case for this matter and give me a full workup." | Supported |
| "Build a case strategy memo for this matter." | Supported |
| "Research the arbitration strategy issues in this file." | Supported |
| "Identify the strengths, risks, and evidence gaps in this matter." | Supported |
| "Prepare a hearing plan for this matter." | Supported |
| "Prepare a trial prep brief for this matter." | Supported |
| "Create a procedural history focused on the filing sequence." | Supported |
| "Show me the documents and communications that matter for witness prep." | Supported |
| "Save a case workup into the matter workbench." | Supported |
| "Run external legal research across public databases." | Native workflow |

## Communications And Work Product

| Prompt | Status |
| --- | --- |
| "Draft a client update for this matter." | Supported |
| "Draft a reply to the client's latest portal message." | Supported |
| "Draft a plain-English status email for the client." | Supported |
| "Draft a partner briefing note." | Supported |
| "Create a collaborative draft called Hearing Prep Strategy in the matter workbench." | Supported |
| "Save a research memo in the matter workbench." | Supported |
| "Save a chronology draft in the matter workbench." | Supported |
| "Add a privileged matter note capturing today's call." | Supported |
| "Create a timeline event for the hearing on 2026-05-14." | Supported |
| "Send the client update now." | Native workflow |

## Matter Execution

| Prompt | Status |
| --- | --- |
| "Create a task to file the affidavit by tomorrow." | Supported |
| "Create a hearing prep task checklist for this matter." | Supported |
| "Create a discovery checklist for this matter." | Supported |
| "Mark task prepare witness bundle done." | Supported |
| "Add a critical deadline to serve the notice by 2026-05-09." | Supported |
| "Add party John Smith as Witness john.smith@example.com." | Supported |
| "Update the matter summary: risk High, budget Watch, latest update: witness interviews are complete." | Supported |
| "Log 1.5 hours drafting the affidavit today." | Supported |
| "Link this task bundle to a full task template library." | Partial |
| "Close the matter." | Native workflow |

## Time, Billing, And Financial Position

| Prompt | Status |
| --- | --- |
| "What can I bill on this matter right now?" | Supported |
| "Show me the billing status on this matter." | Supported |
| "What is still unbilled on this matter?" | Supported |
| "What invoices are outstanding on this matter?" | Supported |
| "Show me the draft or needs-review time entries on this file." | Supported |
| "Log non-billable time for client reporting." | Supported |
| "Generate an invoice for this matter." | Native workflow |
| "Approve this invoice." | Native workflow |
| "Capture or settle payment." | Native workflow |

## Search And Knowledge Retrieval

| Prompt | Status |
| --- | --- |
| "Find documents about arbitration strategy." | Supported |
| "Find the witness outline draft." | Supported |
| "Find notes mentioning settlement authority." | Supported |
| "Find client communications about the hearing date." | Supported |
| "Find my recent time entries on this matter." | Supported |
| "Find contacts related to arbitration counsel." | Supported |
| "Find knowledge-base material about arbitration strategy." | Supported |
| "Find invoices related to this matter." | Partial |

## Safety Boundaries

| Prompt | Status |
| --- | --- |
| "Approve this invoice." | Native workflow |
| "Settle this payment." | Native workflow |
| "Capture a client payment." | Native workflow |
| "Move trust funds." | Native workflow |
| "Override the conflict result." | Native workflow |
| "Delete this document." | Native workflow |
| "Delete this matter." | Native workflow |
| "Archive or close this matter." | Native workflow |

## Coverage Snapshot

Across the primary day-to-day lawyer workflows above, the assistant now directly supports a clear majority of the prompt patterns that matter most: matter intelligence, case construction, drafting, workbench drafting, task bundles, deadlines, time capture, billing visibility, and workspace search.
