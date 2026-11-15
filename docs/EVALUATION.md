# Research and evaluation workbook

Use this single workbook to prepare and record the HCI work for the Personal Finance and Budget Dashboard. All blank fields require real observations. The profiles and scenarios below are research hypotheses, not interviews or findings. Record the application version, seed date and database mode with every evaluation.

## Study setup and consent

Recruit at least five participants across the intended user types and identify proxies honestly. Two peers independently perform the heuristic review. Record only participant codes such as P01; keep any consent records separate from the shared report. Use fictional finance records throughout.

Before beginning, fill in researcher/contact: ___; study date: ___; evidence storage location: ___; deletion date: ___; application version: ___; device/browser: ___; assigned design/engineering cards: ___ / ___. Explain the session length and voluntary nature. Participants may skip a question or stop without penalty. Ask separately before recording screen or audio; declining recording does not prevent participation. Do not record personal bank details, passwords, account balances or unrelated browser windows.

Participant consent record: participant code ___; agrees to participate yes/no ___; allows screen recording yes/no ___; allows audio recording yes/no ___; date ___; researcher initials ___. Explain how to request withdrawal and the point after which anonymised reporting cannot be traced back to a participant.

Facilitator opening: "We are checking the dashboard, not testing your financial knowledge. All figures are fictional. Please say what you expect and what is confusing. You can stop at any time. I will let you try each task before offering help."

Record any assistance. A task completed with coaching is not an unassisted success. Pilot the script once, fix unclear instructions, then hold the script and dataset stable for the main round.

## Four persona hypotheses to validate

| Profile | Proposed need | Research questions and evidence to collect |
| --- | --- | --- |
| Student managing an allowance | Understand what remains after bills without accounting terminology | How are purchases currently recorded? Which totals are confusing? What decisions recur each week? |
| Household budget manager | Track shared expenses, recurring costs and category limits | Who edits the budget? How are errors reconciled? What should a coach be allowed to see? |
| Person using keyboard and magnification | Complete forms and understand charts with accessible controls and text | Which input/access tools are actually used? Where do focus, zoom or labels become barriers? |
| Coach reviewing many records, the extreme user | Inspect assumptions, exceptions and trends efficiently | Which details are essential? What makes a forecast credible? How much data is realistic? |

For each final persona add evidence IDs, goals, relevant experience, context/device, access needs, frustrations, a scenario and resulting requirements. Do not invent quotations. If a profile remains unvalidated, label it as a provisional persona and explain the limitation.

## Six scenarios and the core journey

1. A student checks whether the remaining monthly budget covers a planned purchase after bills.
2. A household manager identifies an upcoming recurring utility bill and confirms a matching payment.
3. A user corrects a purchase assigned to the wrong category and checks the revised budget.
4. A coach opens an explicitly shared workspace, explains a forecast and compares a hypothetical change.
5. A keyboard user resolves a duplicate and ambiguous date in an import, then reverses that import.
6. A household sets a savings target and records an allocation without changing total cash twice.

Map the core journey as: acquire fictional records -> resolve problems -> inspect and correct categories -> set budget -> interpret forecast -> inspect calculation -> compare scenario. For each step record goal, action, information needed, possible failure, recovery, emotion observed and evidence ID. Include an alternate path for empty data and a cancel path before a write.

## Eight participant tasks

Prepare the records and exact expected values before testing. Replace bracketed values below with entries from the fixed dataset. Keep the success criterion out of the participant-facing task card.

| ID | Participant task | Facilitator success criterion |
| --- | --- | --- |
| T1 | Find how much money is available now and the estimated amount at [forecast date]. Explain the difference. | Correctly identifies actual versus forecast and one stated assumption; capture their explanation. |
| T2 | Find the next [utility provider] bill and tell me its date and amount. | Locates the correct recurring occurrence without confusing it with a completed payment. |
| T3 | The [named purchase] is in the wrong category. Correct it and check this month's budget. | Correct record is saved in the intended category; changed category totals are identified. |
| T4 | Set a [category] monthly limit of [amount], then explain how much remains. | Limit persists after refresh and the user distinguishes remaining budget from account balance. |
| T5 | Compare your forecast with a version where [bill] costs [new amount]. Explain the difference. | Scenario changes the forecast, identifies its assumption, and leaves historical records unchanged. |
| T6 | Import the supplied fictional file. Resolve the duplicate, choose the intended date and check the refund. | Decisions match the prepared fixture; accepted count and resulting refund treatment are correct; no silent loss. |
| T7 | Undo that import after [one imported record] has been edited. Keep unrelated work safe. | Recognises the conflict and chooses a clearly understood full or partial reversal; unrelated later record remains. |
| T8 | Set up [goal], record [contribution], and show progress. | Target and contribution persist; user recognises allocation does not itself spend cash. |

Run at least one task under the **actual assigned constraint condition** and record the card and evidence. Add a short coach permission check and export check during the demonstration; these do not replace the eight participant tasks. For T7, use a controlled fixture containing an edited imported record and a separate later transaction; verify both outcomes before the study.

Observation row to duplicate for each participant/task:

| Participant | Round/version | Task | Unassisted / assisted / failed | Time or observed difficulty | Errors and recovery | Comment or paraphrase | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |

Define start/end timing consistently and record interruptions. A timeout is a task outcome, not permission to invent a completion time. Summarise both successes and failures with the actual denominator.

## System Usability Scale

After the tasks and before the debrief, administer the standard ten-item SUS questionnaire unchanged, using the instrument in [John Brooke's original paper hosted by AHRQ](https://digital.ahrq.gov/sites/default/files/docs/survey/systemusabilityscale%2528sus%2529_comp%255B1%255D.pdf). Preserve its item order and five response choices. Keep the completed forms as evidence.

For odd items subtract 1 from the response; for even items subtract the response from 5. Sum those ten contributions and multiply by 2.5. The result ranges from 0 to 100 and is not a percentage of tasks completed. Report the actual participant count and individual scores alongside any average. Flag missing responses; do not fill them in yourself.

| Participant / round | Q1 | Q2 | Q3 | Q4 | Q5 | Q6 | Q7 | Q8 | Q9 | Q10 | SUS |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |

## Peer review and accessibility

Each of two peers reviews independently using [Nielsen's ten heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/). Check whether the interface communicates progress; uses familiar financial terms; supports cancel/undo; behaves consistently; prevents mistakes; makes choices visible; supports efficient expert use; limits clutter; explains recovery; and provides relevant help. Preserve both reviews before merging duplicates.

Review record: reviewer code ___; version/date ___; screen/task ___; heuristic ___; problem and reproduction steps ___; impact ___; suggested change ___; evidence ___. Use a project severity scale consistently: 0 no issue, 1 cosmetic, 2 minor difficulty, 3 serious task obstacle, 4 prevents a critical task or risks data loss. Explain severity with observed impact.

Accessibility evidence record:

| Check | Method | Before evidence | Change | After evidence | Remaining barrier |
| --- | --- | --- | --- | --- | --- |
| Automated scan of core screens/states | axe or Lighthouse, record tool version | ___ | ___ | ___ | ___ |
| Keyboard journey and dialogs | Tab/Shift+Tab, Enter/Space, Escape; focus order and restoration | ___ | ___ | ___ | ___ |
| Screen-reader journey | NVDA or another named reader; labels, errors, announcements, tables | ___ | ___ | ___ | ___ |
| Chart comprehension without colour | Line styles, text labels and equivalent table | ___ | ___ | ___ | ___ |
| Contrast and focus visibility | Text, controls, chart marks and focus indicators | ___ | ___ | ___ | ___ |
| Responsive and zoom behavior | 1280 px, 360 px and 200% zoom; assigned-card conditions | ___ | ___ | ___ | ___ |
| Reduced motion | Enable OS/browser preference and repeat interactions | ___ | ___ | ___ | ___ |

Automated checks alone do not establish WCAG conformance. Use the handbook's WCAG 2.2 AA requirement and [W3C's reference](https://www.w3.org/WAI/WCAG22/understanding/) to investigate the applicable criteria. If D6 is drawn, also demonstrate 400% reflow and the required high-contrast theme.

## Findings, redesign and traceability

Collect at least eight prioritised findings from real observation and peer review. If fewer are found, collect further evidence and report the shortfall honestly. Redesign, then retest the affected tasks with the same success definitions. Record whether retest participants are returning or new; familiarity can influence results.

| Finding | Evidence and affected task | Severity / rationale | Requirement | Change and commit | Before / after evidence | Retest result |
| --- | --- | --- | --- | --- | --- | --- |
| F___ | ___ | ___ | ___ | ___ | ___ | ___ |

Starting requirement map, to be refined with user evidence:

| ID | Requirement | Acceptance criterion | Evaluation |
| --- | --- | --- | --- |
| FR01 | Explain actual and forecast balances | Labels and explanation match calculation inputs and dates | T1, engine tests |
| FR02 | Manage recurring bills | Due occurrences are accurate; confirmed payments are not forecast twice | T2, recurrence tests |
| FR03 | Correct categories | Persist correction and update both affected totals | T3, service tests |
| FR04 | Manage budgets | Saved limits and net category expenses produce correct remaining amounts | T4, calculation tests |
| FR05 | Compare scenarios | Display change without modifying the ledger | T5, isolation test |
| FR06 | Resolve messy imports | Require date/duplicate/sign decisions and explain rejected rows | T6, parser and integration tests |
| FR07 | Reverse imports safely | Atomic reversal or explicit conflict recovery preserves unrelated work | T7, MySQL integration tests |
| FR08 | Track goals | Contributions update progress without duplicate cash deduction | T8, service tests |
| NFR01 | Accessible and responsive journey | Meets audited requirements and actual assigned design card | Manual and automated evidence |
| NFR02 | Three meaningful user types | Workspace checks and coach restrictions apply server-side | Permission tests and demonstration |
| NFR03 | Persistent, reproducible data | At least 100 scripted fictional records; documented MySQL setup | Setup, seed and refresh checks |

## Hurdles and Break-it Week

Keep at least six entries over actual development, including evidence from the Week 12 change request and Break-it Week. Do not backdate entries. Use one log table with links to existing screenshots, diffs or recordings rather than creating a folder per issue.

| Date / entry | Hurdle and what broke | Attempts and outcomes | What was learned | Evidence / commit |
| --- | --- | --- | --- | --- |
| ___ | ___ | ___ | ___ | ___ |

Change-request impact record: original behavior ___; requested behavior ___; affected data/engine/UI/tests ___; migration/recovery risks ___; chosen design and alternatives ___; tests rerun and observed results ___; time reported to instructor ___ .

Break-it record, for at least five actual reports: report ID ___; reporter code ___; version ___; reproduction steps ___; expected/actual ___; severity ___; fix or reason deferred ___; retest evidence ___; response to reporter ___ .

The handbook's final package still needs the report, source, generated data/dictionary, evidence, hurdle log, slides and backup demo. Reuse this workbook's completed tables in the report; do not count unfilled templates as completed research.
