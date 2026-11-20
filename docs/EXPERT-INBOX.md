# Interview two domain experts

The production planner needs a date on which both equipment and QA review are available. Maintenance knows the equipment date. QA knows the review date. This opt-in interview exercises evidence gathering through the company's existing SMTP/IMAP service. Each date is a seeded scenario assumption about expert availability. The resulting recommendation has no authority to release equipment or stock.

In a provisioned, active recurring run, initialize a case before a future management phase. The host can run these commands with the same run path used by the company generator.

```sh
run=/path/to/your/private/run
interview() { python3 -m norvale.agents.expert_inbox --run "$run" --case shift-review "$@"; }
interview init
interview ask MAINT_LEAD
interview ask QA_MGR
```

The planner chooses whom to contact from the question's directory. The daily management phase checks for incoming questions and each addressed expert replies using its own mailbox. Continue the ordinary generator as usual. The planner's mailbox now holds the shared conversation evidence across both specialists.

```sh
interview inbox
interview grade 2027-02-04 '<first native Message-ID>' '<second native Message-ID>'
```

Use the dates and IDs from your run. Both resources must be available, so the earliest feasible date is the later of the replies. The owner grades the date and both citations. Before both replies are observable, the supported answer is `unknown`. Message dates bound this check to the current simulated clock.

The owner process holds the case seed, grader and native credentials. Give the investigating agent only `question`, `ask(expert)` and `inbox` tool results. Run it without controller filesystem access. The expert handlers answer deterministic structured questions; they never receive the other role's briefing. Corroboration using an operational ERP or QMS record is a separate assessment.

The case resides in `RUN/private/expert-inbox-v1/CASE` (mode 0700). Keep it out of learner exports. A restart reopens the same seed, version and native messages. A send intent recorded before SMTP prevents automatic duplicate sends. If delivery remains uncertain, reconcile the sender's mail service before authorizing a retry. Archive this directory outside the run to disable future handling and move it back to resume. Existing mail remains evidence. Removing the opt-in code by reverting the feature commit preserves existing application records.

Offline check from the repository root:

```sh
python3 scripts/check_expert_inbox.py
```

This checks the role dialogue, native-ID contract, replay, visibility cutoff and evidence-based grade through in-memory mail transports. An isolated native SMTP/IMAP exercise on simulation day 2027-01-01 used three commissioned employees and four messages. The planner received Jan 2 from Maintenance and Jan 4 from Quality. The private grader accepted Jan 4 with both citations and rejected a missing citation. Reopening the case recovered the same two request IDs and two reply IDs. Native email header folding exposed a leading whitespace discrepancy, which the final implementation reconciles. This exercised the same respondent callable used by management, invoked separately in an opening clock window. A later recurring horizon was outside this check.
