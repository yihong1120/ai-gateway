# Controlled official-document workflow

## Stable document names (3.12.5)

The grouping response names each distinct attachment once from its own formal
heading, with a page reference checked against that attachment. All mother letters
use that same name and keep their own original attachment labels; different
mother-letter abbreviations no longer create different names for the same pages.
Unknown headings remain unnamed rather than being summarized. The existing
grouping request supplies these names; no extra naming request is made.

PDF/Word filenames use the verified full subject, independent of the model's
optional `filename_subject` fragment. Filename length limits apply deterministic
truncation with an ellipsis, without rewriting the original subject or attachment
heading. Attachment filenames use the parent's date/reference, original attachment
label and the attachment's formal title; they do not append the mother's subject
again. Each parent's date and reference still remain distinct. Existing exports
and earlier chat responses are unchanged.

## Chat attachment relationships (3.12.4)

Each letter keeps its own reference number, date and main PDF/Word output.
The model identifies the mother's complete attachment list in source order,
including numbered letters as well as tables/drawings. Each entry retains the
original attachment label and name, supported by a quote from the mother.
A mere citation is not an enclosure relationship. Missing original labels stay
unnumbered; export positions do not become invented attachment numbers.

An attached letter links to its group's existing main PDF/Word, with the parent
relationship also displayed under that letter. Forward references resolve after
all groups export, without regenerating the attached letter. Shared tables keep
their existing per-parent filenames while reusing rendered content. Relationships
are extracted in the existing grouping request; adding these links adds no model call.
Previously generated downloads/chat messages are not rewritten.

## Dashboard history pagination

Each automatic-history card includes Taipei timestamps (seconds): verified
personal receipt time (currently unavailable in the connector), the separately
labelled EIP folder record time, first pipeline detection, and last processing
checkpoint. These display fields never supply calendar deadlines. The additive
`task_timeline` table preserves first detection across rescans and explicit task
row replacement; processing checkpoints update only the last-processing field.
Pre-migration history keeps unknown timestamps NULL rather than inferring them
from the mutable task `updated` field. For those records, the UI clearly labels
that existing field as record update time, not processing completion time.
Cards show at most two recorded timestamps by default: EIP folder time and
last processing time (falling back to explicitly labelled record update time).
There is no time-details disclosure or repeated missing-field notice. All display
times remain fixed to Asia/Taipei; first-detection and receipt metadata remain
available in the existing backend contract without appearing on these cards.
This presentation change does not modify timestamps or task/calendar records.

Automatic status/download history is paged server-side, default 5 records per
page (UI choices 5/10/20). `GET /api/v1/eip/status` accepts `automatic_page`
(one-based) and `automatic_page_size` (1–20) and returns `automatic_pagination`.
Only the current verified user's requested page is decrypted; the total count
and rows share a read snapshot. Stable update-time/ID ordering reaches history
beyond the previous 20/100-record display caps. Empty/out-of-range pages are
clamped. Polling retains the page, older responses cannot overwrite newer
navigation, and failed navigation retains the current list. Pagination never
changes task phases, scheduling, import receipts or download authorization.

## EIP calendar semantics (3.11)

Since 3.11.3, the model is instructed to generate action-only titles (no
`古坑工程｜` prefix); the verified full project remains in the event description and is
matched against the current EIP project list before import.  The title ends in `[AI產生]`.
Calendar descriptions display a concise action/project/date header followed by
all selected original passages, grouped as main-document contents and related
attachment contents. Internal `S1`/line-range labels are used for validation,
not shown in the calendar. Only balanced, known OCR text-format wrappers
(`underline`, `text`, bold/italic text variants) are unwrapped, preserving their
contents. Money, numbers, unknown commands, malformed syntax and unrecognized
formulae are not erased. The same bounded normalization is applied to metadata
and source evidence to prevent formatting-only subject validation failures.
Raw source objects and original PDFs are unchanged; this does not authorize
the model to rewrite formal paragraphs. Existing events are updated only by
explicit, snapshot-checked operator repair; their dates, privacy, custom fields
and write receipts remain protected. Replacement CSVs retain the old files for
recovery and bind both versions' deduplication receipts to the same event.

The one-time source-date, preview and document-gate recoveries are retired.
At cleanup, the configured database had no matching review tasks and no pending
`calendar_reanalysis` jobs. Rediscovery leaves `needs_review` tasks unchanged;
the durable import journal remains. Failed hourly tasks require manual review.

CM_Import's Description preview also uppercases ASCII letters (verified with
read-only production probes). Preview validation accepts only exact text or
that exact ASCII-uppercase transform; the write payload is reconstructed from
the original CSV Description, never the uppercased preview. Date/numeric/text
changes still fail validation, and post-write detail verification remains
case-sensitive. Existing downloadable CSV/PDF/Word bytes are not rewritten.

Since 3.11.1, newly generated EIP event titles end with the literal suffix
`[AI產生]`. The model supplies `project_name` with independent source-line evidence;
code keeps it in the description and uses it only for a live, unique EIP-project match.
If absent, the event remains private rather than guessing a project.
EIP's 200-byte limit may
shorten only the task text, preserving the complete suffix and storing the
full title in the description. Relative reminder imports preserve that suffix
as well. PDF/Word names, existing CSVs and previously imported events are not
bulk rewritten by this change.

Live acceptance on 2026-09-08 found that EIP automatically expands the share
list to project members when `proj_id` is assigned, even with `public=0` and
an owner-only `pub`. The current PM_List member set is read alongside project
choices, sent explicitly, and verified on readback; unexpected recipients fail
validation. A unique source-matched project is assigned automatically, while
missing or ambiguous matches remain private.
The post-import attachment edit preserves the verified project and recipients.
Interrupted `assigning_projects` tasks become `needs_review` at restart and
unknown project writes are not replayed.

One source-bounded LLM request reads the entire authorized attachment group.
Calendar contract v2 separates file naming metadata from per-event semantics:

- Each event has an LLM-generated concise title, action, date/time, location,
  current/historical/cancelled/review disposition and field-level source/line
  references. Different fields may reference different related attachments.
- Code resolves original text and checks ISO/ROC dates and time values against
  cited lines; damaged or ambiguous values require review. This is provenance
  and value validation, not a guarantee of the LLM's semantic judgment.
- Description preserves selected complete source passages with readable document
  and attachment headings, without internal source line IDs. Only locally recorded
  layout-marker lines and recognized text-format wrappers are removed. Original PDFs and
  editable Word exports remain independent of generated calendar titles.
- Current source tasks with past dates retain those dates. Historical references
  and cancelled events do not become new entries; conflicts block that group's
  calendar output. No event means no CSV/import. Missing end times stay empty
  until the existing opt-in importer applies its labeled reservation duration.
- `complete` describes all requested outputs; `calendar_ready` independently
  requires a saved CSV and successful calendar validation without unresolved
  reminders. Hourly imports additionally require `complete`; any incomplete
  document output stops the run before calendar import. Manual workflows retain
  their structured partial-output reporting.
- Existing task completion and the durable write journal still prevent replay.
  Unknown writes require reconciliation, not retries with a differently worded
  title. Completed tasks are not regenerated solely to update their titles.
- EIP's CSV preview URL-decodes cell values. Upload transport protects `%` and
  `+` exactly once, preserving engineering chainage such as `0K+100` and literal
  percent sequences. Commit text escapes plus signs as well, and old wire-hash
  aliases remain recognized; preview/detail comparison is not relaxed.
- Explicit repairs of existing private all-day events omit time parameters,
  matching the official form. A failed update must be read back and reconciled;
  it is never blindly resent or replaced with a new event.

Tests: `test_eip_calendar_semantics.py`, `test_eip_group_processing.py`,
`test_eip_automation.py`, plus existing connector and document tests. Live case
repairs must preserve prior file hashes, check existing owned events before any
write, and verify the event detail after import.

`official_document_workflow` is the document control layer, with bounded document review, for the
Open WebUI official-document Pipe.  It deliberately does **not** replace the
existing OCR, cloud document extraction, PDF/DOCX generation, or Open WebUI
file-storage code.

The flow is intentionally bounded:

```text
latest user message + current upload scope
  -> instruction-planning Agent (strict JSON)
  -> Pydantic policy validation
  -> existing deterministic document pipeline
  -> deterministic calendar transforms
  -> optional short-lived deadline-choice clarification
  -> EIP preview / later confirmed adapter
```

Only the last user message is available to the planner.  It never receives
previous chat turns, other attachment IDs, OCR text, or document content.
Document semantics remain the responsibility of the existing source-validated
cloud extraction.

## Supported user-requested changes

The planner has a deliberately small JSON contract:

- choose whether to generate the renamed PDF, editable DOCX, and calendar CSV;
- add an all-day CSV reminder a bounded number of days before or after a
  **verified document deadline**;
- request an EIP preview or a future write-after-confirmation flow.

It cannot choose a model, read a URL or other attachment, run a tool, delete
or overwrite a source file, invent a document date, or change source-derived
document facts.  Relative date arithmetic and CSV writing are deterministic
Python code, not language-model output.

If a single-target reminder has more than one verified deadline, it produces
a clarification instead of choosing one. For a server-issued same-chat
follow-up, a separate strict resolver receives only the newest reply plus a
numbered list of verified dates; it can select one, all, or ask again. The
pending record is process-memory-only, has a bounded TTL and payload size, is
bound to the user, server chat, and original attachment fingerprint, and
re-checks file ownership before a replacement CSV is written. A new upload,
expiry, source-file loss, or service restart invalidates it. A relative
reminder must have an output destination: CSV, or an EIP
preview/confirmed-sync request.

## EIP calendar safety

The live per-user connector is in `infrastructure/eip/`; see
[`docs/eip-connector.md`](../docs/eip-connector.md) for the dashboard, sessions,
RPC contract, explicit confirmation, and verified import limitations.
The planner itself remains side-effect-free; only the authenticated dashboard
confirmation route or an explicitly opted-in hourly worker may write to EIP.

The hourly worker is deterministic: encrypted per-user credentials, unread
inbox discovery, durable per-document checkpoints, existing source-validated
processing, and the shared calendar validation/idempotency journal. It cannot
be enabled by an LLM response or by instructions inside a document.

The current package produces a side-effect-free EIP sync preview.  A live
adapter must be supplied separately and must:

1. use server-side credentials only;
2. create an idempotency journal before and after each remote write;
3. require an explicit confirmation token by default;
4. never use the CSV `Name` column as the EIP event owner;
5. operate over HTTPS, or be restricted to a trusted internal network/VPN if
   the legacy EIP deployment remains HTTP-only.

The Open WebUI Pipe does not keep EIP credentials, confirmation tokens, or a
remote-write journal. It links its CSV to the separate authenticated EIP
dashboard for account linking, inspection, and explicit confirmation.
## Layout

- `application/`: manual handling, scheduled pipeline and rerun orchestration.
- `domain/calendar/`: evidence and calendar semantics.
- `infrastructure/eip/`, `infrastructure/storage/`, `infrastructure/office/`:
  remote EIP, PostgreSQL persistence and document conversion boundaries.
- `api/`: FastAPI router and the dashboard assets.

The scheduled workflow uses PostgreSQL only; it does not read SQLite files.

Instruction planning, document analysis and deadline-choice resolution call their
model adapters directly and validate the response with Pydantic. The EIP batch
uses bounded async loops for group iteration and at most one format correction.
OCR page/context objects are used during source extraction without being retained
in persistent state. Source spans use the existing validated dictionary contract;
there is no separate unused `SourceSpan` dataclass.

Grouped analysis accepts only an explicit integer `calendar_contract_version=2`;
missing or obsolete versions produce a review error rather than invoking the old
single-document event/description parser. Manual, hourly and company-agent
entry points call `process_with_team` directly. Searchable OCR PDF generation
helpers are removed; legacy preference fields are discarded when read and are absent from new
settings and output schemas. OCR source extraction and editable Word generation still share the
existing layout parsers.


Storage constructors now use `Journal()` and `AutomationStore(key_path)`; the
obsolete local database-directory argument is removed. All storage clients use
the shared PostgreSQL schema, with table creation and upgrades tracked in
`schema_migrations`. On first startup, an unversioned database is adopted in one
transaction under an advisory lock; existing encrypted payloads, receipts and
known timestamps are preserved. Later starts check the version without repeating
DDL or historical backfills. Failed upgrades roll back and can be retried;
newer schemas are rejected rather than downgraded. ProcessingCache tables are
created by the same migration, not on every cache instance.

The removed `searchable_ocr_pdf` plan field is accepted only as discarded legacy
input; unrelated unknown plan fields still fail validation. Old
`create_searchable_ocr_pdf` settings are ignored by the Pipe's settings model.
Neither retired field appears in new schemas or planner instructions.

Imports are grouped at module scope (standard library, third-party, local).
Mutually dependent workflow/connector modules use module references so that
functions are resolved at call time. The launcher prepares the environment, then
Uvicorn loads the ASGI application factory by its module path. All Python files in
`official_document_workflow` keep imports at module scope, including WebUI models
and storage. Load the application through this launcher so environment setup
precedes those imports. Offline tests replace WebUI services during application
module loading and patch the imported bindings when exercising file access.

The one-off existing-event backfill, title repair and description repair tools
have been retired: no application entry point or hourly worker used them. The
shared verified edit builder remains in use for project assignment and original
PDF attachment uploads. Clarification state still expires automatically on access;
its unused explicit purge API was removed and capacity eviction is shared by
both insertion and restoration.

### Shared boundaries and batch execution

`infrastructure/eip/calendar_csv.py` owns CSV validation and transport formatting;
it and the calendar replay/write helpers import without WebUI or application code.
The Pipe exporter and CSV parser share `domain/calendar/csv_schema.py`. Replay
receives its authorized CSV reader explicitly from the calling application.

`infrastructure/storage/database.py` owns EIP connection settings and transaction
cleanup, retaining tuple rows for the journal and dictionary rows for accounts.
Preview and replay status reads are batched; the final write reservation remains
transactional. CSV history queries select only the owner's CSV IDs and metadata,
without loading unrelated files or truncating history.

`application/automation.py` schedules accounts and handles run/task failures.
`application/automatic_task.py` handles download, analysis, calendar preparation,
verified writes and completion. The first failed document stops the account run
and is marked `needs_review`; it is never scheduled for automatic retry. Remaining
untouched documents are eligible on the next hourly run. Old retry checkpoints
are retired to review at startup. Ambiguous-write protection remains. Batch and
document processing run sequentially, including bounded repair and semantic review.

Calendar preview shares one wire-alias builder for ordinary and cross-day events,
then checks all canonical and legacy receipts in one batch. Final reservations
still recheck every alias transactionally before writing. Automatic create,
attachment and project writes share the same authorization/expiry guard; none
refreshes or replays a write after session expiry. Active-binding checks query
only the owner, generation and enabled flag instead of loading account secrets.

The scheduler retains one outer failure boundary to record the stopped task/run
and keep hourly scheduling alive. Session closure, database rollback and calendar
receipt checks remain. Per-document catch-and-continue, retry-phase selection,
cloud-throttle continuation and delayed failure retries have been removed.

Route handlers call `manual_session` directly. `Netask.automatic_documents`
contains the scan implementation and accepts an optional folder tuple for
inbox-only reads; there is no separate `pending_inbox` or `_scan_folders` wrapper.
Calendar evidence resolves spans directly without an intermediate forwarding
function. Authorization, transaction and write-receipt boundaries remain explicit.
