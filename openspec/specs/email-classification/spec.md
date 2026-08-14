# email-classification Specification

## Purpose

Classifies fetched emails by job-search priority so the user can see at a glance what needs their attention, without listing what does not. Covers the batched classification tool contract, the category criteria, classification-input invariance, per-email failure fallback, and the completeness guarantee.

## Requirements

### Requirement: Batched classification in a single tool call

The classification tool SHALL accept a list of emails in one invocation and SHALL return exactly one result per input email, in the same order as the input. The classification agent SHALL classify a set of emails with a single tool invocation, not one invocation per email.

#### Scenario: Twenty emails, one tool call

- **WHEN** the classification agent is given 20 fetched emails to prioritize
- **THEN** it invokes the classification tool once with all 20 emails and receives 20 results, one per email, in input order

#### Scenario: Single email still works

- **WHEN** the classification tool is invoked with a list containing one email
- **THEN** it returns a list containing exactly one classification result for that email

### Requirement: Per-email classification result shape

Each classification result SHALL contain a `classification` value that is one of `urgent`, `action_needed`, `fyi`, or `spam`; an `action_items` list of strings (empty when there are none); and a `deadline` string (empty when none is stated).

#### Scenario: Result fields present for every email

- **WHEN** the classification tool returns results for a batch
- **THEN** every result contains a valid `classification` category, an `action_items` list, and a `deadline` string

### Requirement: Batching does not change what is classified

Each email in a batch SHALL be evaluated independently, against the same criteria and the same email text as single-email classification: a batch of emails SHALL be classified to the same categories as if each were classified alone. Classification SHALL treat urgency as "the user's job search needs them now", not general time-sensitivity. Interview scheduling requests, assessments with due dates, and deadline-bound application asks SHALL be `urgent`; non-time-boxed direct recruiter outreach and live-application form steps SHALL be `action_needed`; verification codes, security alerts, rejections, no-action status updates, digests, marketing, and receipts SHALL be `fyi` even when they appear time-sensitive; unsolicited or malicious content with no informational value SHALL be `spam`.

#### Scenario: Time-pressured non-job email in a batch

- **WHEN** a batch contains an expiring verification code alongside an interview scheduling request
- **THEN** the verification code is classified `fyi` and the interview request is classified `urgent`

#### Scenario: Batch and single-email classification use an identical prompt

- **WHEN** the same email is classified alone and as part of a batch
- **THEN** the per-email prompt its content flows through is byte-identical in both cases — the provable invariant behind category agreement. (That the categories themselves agree across live runs is an eval-level expectation verified by `./run_evals.sh`, not a unit-test assertion.)

### Requirement: Classification input is the provided content

The classification tool SHALL classify exactly the email content supplied by the caller (sender, subject, and the body or snippet as given) and SHALL NOT fetch or re-fetch email content itself. What the classifier evaluates is determined by the caller, so the fetched-content contract stays unchanged by batching.

#### Scenario: Snippet-only email is classified from the snippet

- **WHEN** an email in the batch carries only a snippet rather than a full body
- **THEN** the classification is based on that snippet, exactly as single-email classification behaves today

### Requirement: Per-email failure fallback within a batch

When a classification result for an individual email cannot be obtained or parsed, that email SHALL receive the safe default (`fyi`, empty `action_items`, empty `deadline`), and the remaining emails in the batch SHALL keep their actual classifications. The tool SHALL return a result for every input email rather than raising, even when every email's classification fails.

#### Scenario: One email's classification fails

- **WHEN** the classification of one email in a batch fails or returns unparseable output while the rest succeed
- **THEN** the affected email gets `fyi` with empty action items and deadline, and every other email keeps its returned classification

#### Scenario: Every classification fails

- **WHEN** classification fails for every email in the batch
- **THEN** the tool returns one safe-default result per input email instead of raising

### Requirement: Completeness of the classified summary

The classification agent SHALL account for every email it was given exactly once in its grouped summary: each email appears under `urgent` or `action_needed`, is counted in the FYI total, or is explicitly reported as unclassifiable. The agent SHALL never return an empty response or silently drop an email. The count-reconciliation rule now guards only the summary stage: the batched tool contract (one result per input email, in input order) structurally removed the per-email tool-call loop the rule originally protected, so summary-stage drops are the only failure mode left for it to catch.

#### Scenario: All inputs accounted for

- **WHEN** the classification agent summarizes a batch of N emails
- **THEN** the emails listed under Urgent and Action Needed, plus the FYI count, plus any explicitly noted unclassifiable emails, total N
