# installment-ledger

A small backend service for installment loans. You create a loan for a
principal amount over a fixed number of installments, then pay it down over
time. Every loan and every payment is recorded in a double entry ledger, and
the service is built to behave correctly when the same request arrives twice or
when two payments land at the same moment.

The point of the project is correctness under retries and concurrency, not
breadth of features.

## Schema

```mermaid
erDiagram
    loans ||--o{ installments : "has"
    loans ||--o{ payments : "receives"
    loans ||--o{ ledger_entries : "records"
    payments ||--|{ ledger_entries : "writes a balanced pair"

    loans {
        uuid id PK
        bigint principal_cents
        int apr_bps
        int installment_count
        text cadence
        date start_date
        int term_days
        bigint interest_cents
        bigint total_cents
        text status
    }
    installments {
        uuid id PK
        uuid loan_id FK
        int sequence
        date due_date
        bigint amount_cents
        bigint paid_cents
    }
    payments {
        uuid id PK
        uuid loan_id FK
        bigint amount_cents
        uuid transaction_id
    }
    ledger_entries {
        uuid id PK
        uuid transaction_id
        uuid loan_id FK
        text account
        text direction
        bigint amount_cents
    }
    idempotency_keys {
        uuid id PK
        text key
        text endpoint
        text request_hash
        int response_status
        json response_body
    }
```

## Invariants, and where each one is enforced

The distinction matters: anything enforced only in application code can be
bypassed by a bad migration, a script, or a second service writing to the same
database. Anything enforced in the database cannot.

| Invariant | Enforced by | Where |
|---|---|---|
| Installments sum exactly to the total payable | application | `app/schedule.py::distribute_cents`, asserted as a property over the whole reachable input space in `tests/test_schedule_properties.py` |
| Every transaction's debits equal its credits | **database trigger** | `migrations/versions/0001_initial_schema.py`, so it holds even for a write that bypasses this service |
| Amounts are integer cents, never float | **database column type** plus application | `bigint` columns, strict integer schemas in `app/schemas.py` |
| One idempotency key yields at most one effect | **database unique index** | unique index on `idempotency_keys`, so two concurrent inserts cannot both win |
| A payment never exceeds the outstanding balance | application, under a row lock | `app/services/payments.py`, with `SELECT ... FOR UPDATE` on the loan |
| Two concurrent payments cannot interleave on one loan | **database row lock** | `SELECT ... FOR UPDATE`, verified in `tests/test_concurrency.py` with real threads |
| No installment is paid beyond its amount, or below zero | application | `app/services/payments.py`, asserted after a 24 thread storm in `tests/test_concurrency.py` |

## What idempotency does and does not guarantee

**It does:** a repeated request with the same `Idempotency-Key` on the same
endpoint returns the stored result instead of performing the action again. The
key is held in a unique index, so two threads arriving at once cannot both
insert; one wins and the other is served the stored response. This is verified
under contention with 24 threads sharing one key.

**It does not:** make the operation safe to replay with a *different* body. The
request body is hashed and stored alongside the key, so reusing a key with a
changed body is rejected rather than silently returning the old result. It also
does not expire. Keys are kept forever, with no cleanup job, so a long lived
deployment would need one.

## Design choices

**Money is integer cents.** Every stored amount and every amount on the wire is
an integer number of cents, held in a Postgres `bigint`. No float appears
anywhere. The API takes the APR as integer basis points (`apr_bps`, where 1200
means 12.00 percent) so that even the rate never arrives as a float. Amount
fields are strict integers: a JSON string or a number with a decimal point is
rejected rather than quietly coerced.

**One rounding rule.** `Decimal` is used in exactly one place, computing
interest, and the only rounding rule is `ROUND_HALF_UP` to whole cents. It is
applied once, to the total interest, never to individual installments, so
rounding cannot accumulate across a schedule.

**Simple interest, no compounding, no late fees.** Interest is
`principal * apr_bps / 10000 * term_days / 365`, where `term_days` runs from
the loan start date to the final due date. There is no compounding and there
are no late fees or penalties of any kind. That is a deliberate choice: it
keeps the arithmetic auditable and it keeps the amount owed a function of the
schedule alone rather than of when a payment happened to arrive. A service that
needed late fees would need a clock, a grace period policy and a way to reopen
a closed loan, and none of that is here.

**Installments always sum to the total.** The total payable is split with
`base = total // n` and `remainder = total % n`, and the first `remainder`
installments get one extra cent each. So 1000 cents over 3 installments becomes
334, 333, 333. Leftover cents go to the earliest installments, which means the
sum is exactly the total by construction and the borrower pays the rounding
first rather than last.

**Double entry ledger with the invariant in the database.** Creating a loan
debits `loan_receivable` for the whole amount payable and credits
`funding_payable` for the principal plus `interest_income` for the interest. A
payment debits `cash` and credits `loan_receivable`. Amounts are stored
positive and the direction carries the sign. A deferred constraint trigger
(`ledger_entries_must_balance`) sums each transaction at commit time and raises
if it does not net to zero, so an unbalanced transaction cannot be committed
even if the service layer is bypassed entirely. The tests prove this by writing
raw SQL: inserting one side of a transaction, deleting one side of a balanced
pair, and changing one amount all fail.

**Idempotency rests on a unique index.** Both POST endpoints require an
`Idempotency-Key` header. The key row carries a sha256 hash of the endpoint
plus the validated request body, and it is inserted in the same transaction as
the write it guards. A concurrent request with the same key blocks on the
unique index during its own insert, and when the first transaction commits it
gets a unique violation, rolls back, and reads the stored response instead of
writing a second time. Three consequences fall out of that:

* the same key with the same body returns the original response, byte for byte,
  because the response body is stored as written and returned verbatim;
* the same key with a different body returns 409 `idempotency_key_conflict`,
  and so does reusing a key on a different endpoint or a different loan;
* a request that fails rolls its key row back with it, so a rejected attempt
  does not burn the key and the caller can retry.

Responses are stored in a `json` column rather than `jsonb` on purpose, because
`jsonb` reorders object keys and that would make a replay merely equivalent
instead of identical.

**Row locking for concurrent payments.** A payment takes
`SELECT ... FOR UPDATE` on the loan row before it reads the balance, so a
second payment on the same loan waits and then sees the first one's effect
rather than deciding against a stale balance. Lock order is always the
idempotency row first and the loan row second, so two payments cannot deadlock
against each other. Payments on different loans do not contend at all.

**Payment application.** A payment is rejected if it exceeds the outstanding
balance. Whatever is paid fills the oldest unpaid installment first and carries
the remainder forward, and each landing is recorded in `payment_allocations`,
so partial payments are tracked installment by installment. When the last cent
is paid the loan moves to `closed` and refuses further payments.

## Running locally

### One command

```bash
docker compose up -d --build
bash scripts/demo.sh
```

That builds the API image, starts Postgres 18.6, runs the migration and serves
on `http://127.0.0.1:8000`. No `.env` and no secrets are involved.

`scripts/demo.sh` then walks the whole API and prints what came back: it creates
a loan, checks the installments sum exactly to the total, takes a payment,
retries with the same idempotency key to show the stored result returning rather
than a second payment, fires eight concurrent payments, re-checks every
invariant, and finishes by having an overpayment refused with a stable error
code.

Interactive API docs are at `http://127.0.0.1:8000/docs`, and the raw schema at
`/openapi.json`.

### Without Docker for the application

You need Docker for Postgres and Python 3.12.

```bash
docker compose up -d postgres
```

That starts Postgres 18.6 and publishes it on port 5433, which is the port the
default `DATABASE_URL` in `app/config.py` points at. Port 5433 is used so the
container does not collide with a Postgres already installed on the host. Set
`DATABASE_URL` yourself to point somewhere else.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

The service then answers on `http://127.0.0.1:8000`.

## Running tests

The tests need a real Postgres. They run the migration themselves and clear the
tables between tests, and the concurrency tests start a uvicorn server in a
background thread and drive it from real OS threads over HTTP.

```bash
docker compose up -d
.venv/bin/pytest
```

To run exactly what CI runs, from a clean export of `HEAD` in a throwaway
virtualenv with the pinned dependencies:

```bash
bash scripts/clean_export_check.sh
```

### What the suite covers

256 tests. Alongside the example based tests:

* **17 property based tests** (`tests/test_schedule_properties.py`, Hypothesis)
  assert the schedule invariants across the whole reachable input space rather
  than at chosen points: that installments always sum to the total, that no two
  differ by more than a cent, that the remainder always goes to the earliest
  installments, that interest is monotonic in term length, and that a loan
  starting on the 31st never produces a 31st of February.
* **8 concurrency tests** (`tests/test_concurrency.py`) run real OS threads
  against real Postgres through real HTTP, lined up on a barrier so they contend
  on purpose. The heaviest fires 24 simultaneous payments where a third share one
  idempotency key, then asserts all six invariants at once: exactly one payment
  per shared key, money applied matching the payments allowed to exist,
  outstanding never negative, installments still summing to the total, no
  installment over applied, and the ledger still balancing.

### What was measured

The suite is 256 tests and passes in about 10 seconds. That number comes from
the clean export run described above, against an empty database, on this
machine and setup:

* Apple M5 (`Mac17,2`), 10 cores, 16 GB RAM, macOS 26.6.2
* Python 3.12.15 from Homebrew, in a fresh virtualenv
* the pinned dependencies in `requirements.txt`, including ruff 0.16.10 and
  pytest 9.1.1
* Postgres 18.6 in Docker 29.7.2, running locally on the same machine

The concurrency tests use 8 threads, and the heaviest uses 24. **No throughput
or latency benchmark was run, so this README makes no claim about either.**
The timing above is how long the test suite takes, which is not a measure of
how fast the service is.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Reports whether the database is reachable |
| `POST` | `/loans` | Creates a loan and its schedule. Requires `Idempotency-Key` |
| `GET` | `/loans/{id}` | Returns the loan, its schedule and its outstanding balance |
| `POST` | `/loans/{id}/payments` | Applies a payment. Requires `Idempotency-Key` |
| `GET` | `/loans/{id}/ledger` | Returns the ledger entries grouped by transaction |

Errors all use one shape:

```json
{"error": {"code": "overpayment", "message": "...", "details": {}}}
```

The codes are `validation_error`, `loan_not_found`, `loan_closed`,
`overpayment`, `missing_idempotency_key`, `idempotency_key_conflict` and
`idempotency_request_in_flight`.

### Example

```bash
curl -X POST http://127.0.0.1:8000/loans \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-loan-1' \
  -d '{"principal_cents":120000,"apr_bps":0,"installment_count":4,"cadence":"biweekly","start_date":"2026-01-15"}'
```

That returns a 201 with four installments of 30000 cents due on 2026-01-29,
2026-02-12, 2026-02-26 and 2026-03-12. Sending the identical request again with
the same `Idempotency-Key` returns the same 201 and the same body, and does not
create a second loan.

## Limitations

* No authentication or authorization. Every caller can read and pay every loan.
* No real payment processor. A payment is simply asserted by the caller, and
  the `cash` account records that assertion. Nothing settles, nothing clears
  and nothing can be reversed.
* No refunds, reversals, voids or adjustments. The ledger is append only and
  there is no way to correct a mistake through the API.
* No late fees, no penalties, no compounding and no payoff quote that depends
  on the current date. Interest is fixed when the loan is created.
* Single region and single database. There is no replication, no sharding and
  no multi region story.
* `GET /loans/{id}` and `GET /loans/{id}/ledger` read without a lock, so a
  response can be a moment behind a payment that is committing concurrently.
* No pagination on the ledger endpoint, so a loan with a very large number of
  payments returns a very large body.
* No rate limiting, no metrics and no tracing. There is structured logging:
  one JSON line per request on stdout with a request id, taken from an inbound
  `X-Request-ID` when present and generated otherwise, and echoed back on the
  response.
* Idempotency keys are kept forever. There is no expiry and no cleanup job.

## Payments assistant

An optional conversational layer over the API above. It lets someone ask what
they owe and make a payment in plain language. It is a separate package, a
separate dependency set and a separate entry point, and it changes nothing about
how the ledger behaves.

### What it is, and what it is not

It is a small agent that can do four things: read a loan summary, read recent
ledger lines, prepare a payment for confirmation, and apply a payment that was
already confirmed. That is the whole surface.

It is not a bank, not a product and not connected to anything real. Every loan
is fictional, created by you or by a test against your own database. There is no
payment processor behind it, no settlement, no money. It has no authentication,
so it is not something to put in front of real customers. It gives no financial
advice and refuses to try.

### Shape of it

```
                    POST /assistant/chat
                    WS   /assistant/ws
                             |
                             v
         +-----------------------------------------+
         |            LangGraph agent              |
         |                                         |
         |  sanitize -> classify -> tool -> respond|
         |                 |                       |
         |                 v                       |
         |          model, classify only           |
         +-----------------------------------------+
                             |
                   MCP tools/list, tools/call
                             |
                             v
         +-----------------------------------------+
         |   assistant.mcp_server, stdio subprocess|
         |                                         |
         |  get_loan_summary      read             |
         |  get_ledger_entries    read             |
         |  prepare_payment       read, no write   |
         |  confirm_payment       the only write   |
         +-----------------------------------------+
                             |
                        plain HTTP,
                      Idempotency-Key
                             |
                             v
         +-----------------------------------------+
         |      the ledger API, unmodified         |
         |   POST /loans/{id}/payments, GET /loans |
         +-----------------------------------------+
                             |
                             v
                     Postgres, trigger
                  enforced double entry
```

Two things to notice in that picture. The model sits off to one side of the
agent and is only asked to label a message; it is never handed a tool and never
writes a word the user reads. And the assistant reaches the ledger the same way
any other client would, over HTTP with an `Idempotency-Key`, with no database
connection and no import of the service layer.

### How a payment flows end to end

1. The user says something like "pay 300 dollars". The `sanitize` node folds the
   unicode, drops control characters and truncates.
2. The `classify` node checks the message against its rules first. A
   cancellation, a confirmation, or anything out of scope is decided here with no
   model call at all. Only what is left reaches the model, which returns a label
   and an integer amount in cents.
3. Still in `classify`, the amount is checked against the per payment cap,
   against zero and against being written as a negative number, and the loan is
   checked against the set this session is bound to. A failure here means no tool
   runs.
4. The `tool_call` node opens an MCP session and calls `prepare_payment`. That
   tool reads the loan, checks the amount against the outstanding balance, works
   out which installments the payment would land on, and writes a row to its
   pending action table. It posts nothing to the ledger.
5. The `respond` node fills a template: the amount, the balance it would leave,
   the installments it would cover, and "nothing has been paid yet". The pending
   action id stays in the session's state and is never shown to the caller.
6. The user replies. If and only if the whole message is an exact phrase from the
   confirmation allowlist, with no digit in it, the next turn is a confirmation.
   Anything else, including a new amount or a change of subject, discards the
   pending action.
7. On a confirmation, `confirm_payment` posts to `POST /loans/{id}/payments`
   with `Idempotency-Key: assistant-pending-<pending action id>`. Because the key
   comes from the pending action id alone, confirming twice or retrying after a
   timeout sends the same key and the ledger applies the payment once.
8. `respond` reports the amount and the new balance using the numbers the ledger
   returned. If the write timed out it says the outcome is unknown, declines to
   send it again, and offers to read the balance instead.

### Safety rules, and where each one actually lives

The distinction matters, because a rule in a prompt is a request and a rule in
code is a rule.

| Rule | Enforced in |
| --- | --- |
| A write needs a pending action from an earlier turn | Code, `tool_call` node |
| A confirmation is an exact phrase, never the model's judgement | Code, `guards.is_explicit_confirmation` |
| A message with a digit in it is not a confirmation | Code, same function |
| Any turn other than a confirmation cancels the pending action | Code, `classify` node |
| A payment above the cap is refused before any tool runs | Code, `guards.check_amount` |
| Zero and negative amounts are refused before any tool runs | Code, `check_amount` plus a check on the message text |
| A loan outside the session is refused before any tool runs | Code, `guards.resolve_target_loan` |
| Out of scope requests get fixed text and cost no model call | Code, `guards.out_of_scope_reason` |
| A request to skip confirmation is refused outright | Code, same function |
| The user never sees a number the ledger did not return | Code, `agent/replies.py` templates |
| A timed out write is reported as unknown, not as success or failure | Code, `respond` node |
| Loan ids reach the logs as four characters | Code, `assistant/redaction.py` |
| A payment happens at most once per pending action | The ledger's unique index on the idempotency key |
| Payments cannot overpay under concurrency | The ledger's row lock, unchanged |
| Treat the user's text as data rather than instructions | Prompt only |
| Return strictly the requested JSON shape | Prompt, with a strict parser in code behind it that refuses anything else |

Only the last two are prompt level, and both have something in code behind
them. The model cannot widen its own remit by answering differently: a label
outside the three allowed values, an amount that is not an integer, or any
unparseable answer is treated as a failure rather than a guess.

Worth being clear about the one thing the deterministic confirmation check costs:
a user who types "yeah go for it" has not confirmed, and will be told there is
nothing waiting. That is the intended trade.

### Running it

The assistant needs its own dependencies. The ledger does not.

```bash
pip install -r requirements.txt -r requirements-assistant.txt
```

`pyproject.toml` declares the same direct dependencies under an `assistant`
extra, so `pip install -e ".[assistant]"` works too.
`requirements-assistant.txt` is the full transitive lock and is what CI installs.
It records the one pin this pulls forward: `langchain-core` requires
`certifi>=2026.10.19`, so installing the assistant moves `certifi` past the
version in `requirements.txt`. `requirements.txt` itself is unchanged.

Copy `.env.example` to `.env` and fill it in. `OPENAI_API_KEY` is read from the
environment and from nowhere else. It is never logged and never returned in a
response, and the MCP subprocess is started with it removed from its environment
because the tool server never talks to a model.

Then:

```bash
docker compose up -d postgres
alembic upgrade head
ASSISTANT_LLM=fake uvicorn assistant.asgi:app --port 8000
```

`assistant.asgi:app` serves the ledger and the assistant together.
`app.main:app` is untouched and has no assistant routes, so the ledger can still
be run exactly as before. Setting `ASSISTANT_ENABLED=0` makes `assistant.asgi`
hand back the ledger app alone.

A chat turn:

```bash
curl -X POST http://127.0.0.1:8000/assistant/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","message":"what is my balance","loan_ids":["<loan id>"]}'
```

`loan_ids` is sent once and stored against the session; later turns need only
the session id and the message. The agent refuses to read or pay any loan that is
not in that set.

The socket is at `/assistant/ws`. Its first frame must be
`{"token":"...","session_id":"...","loan_ids":[...]}`, with the token matching
`ASSISTANT_WS_TOKEN`. With that variable unset the socket refuses every
connection, which is the intended default. A frame over
`ASSISTANT_WS_MAX_FRAME_BYTES` closes the socket, and a process serves at most
`ASSISTANT_WS_MAX_CONNECTIONS` sockets before refusing new ones outright rather
than queueing them.

What the socket streams is the composed reply split into chunks, not model
tokens. The model does not write the reply, so there are no model tokens to
forward. Calling it streaming is accurate about the wire and would be misleading
about the source, which is why it is spelled out here.

### Running the tests

```bash
docker compose up -d postgres
pytest tests/assistant/unit         # no LLM, no network, database required
pytest tests/assistant/integration  # real Postgres, real ledger, real MCP subprocess
pytest                              # everything, including the existing ledger suite
```

Both assistant directories run without a model. The unit directory calls nothing
over a network. The integration directory starts a real ledger on loopback, runs
a real `python -m assistant.mcp_server` subprocess per turn, and uses the
deterministic fake model client, so it is reproducible and costs nothing.

"Database required" in the unit directory is the repository's own fixture, which
truncates Postgres before every test. Those tests do not themselves touch it.

Type checking covers the assistant package only:

```bash
mypy
```

The ledger is deliberately out of scope there. It predates this configuration,
and checking it would mean editing files this package has no business touching.

### Numbers from real runs

Everything below came from a run on this machine: Apple M5 (`Mac17,2`), 10
cores, macOS 27.0.1, Python 3.12.15, Postgres 18.6 in Docker, with the pinned
dependencies in `requirements.txt` and `requirements-assistant.txt`.

Tests, from `pytest` with `DATABASE_URL` pointing at the compose Postgres:

| Suite | Command | Tests | Wall time |
| --- | --- | --- | --- |
| Existing ledger suite | `pytest --ignore=tests/assistant` | 256 | 3.77s |
| Assistant unit | `pytest tests/assistant/unit` | 171 | 1.45s |
| Assistant integration | `pytest tests/assistant/integration` | 56 | 37.79s |
| Everything | `pytest` | 483 | 43.29s |

The existing 256 tests are unchanged and were not touched. The integration time
is dominated by starting one MCP subprocess per conversational turn, which the
eval run below measured at a median of 348.37 ms per turn end to end.

Lint and types, from the same environment:

```
ruff 0.16.10      ruff check .          All checks passed!
ruff 0.16.10      ruff format --check . all files already formatted
mypy 2.4.1        mypy                  Success: no issues found in 32 source files
```

#### Harness verification, not a model measurement

The evaluation harness was run end to end against the deterministic fake model
client, to check that the harness, the 56 scenarios and the five rule checks all
work. Command and output:

```
LEDGER_BASE_URL=http://127.0.0.1:8099 python -m assistant.eval.run_eval --provider fake

56/56 scenarios passed, 90 turns, 55 model calls, 33.85s
median latency per turn 348.37 ms, p95 507.46 ms
tokens: 0, because the fake client is not a model
```

Every rule passed on every scenario: `correct_tool_chosen` 56,
`no_write_without_confirmation` 56, `exactly_one_ledger_effect` 56,
`correct_amount` 56, and `refusal_when_required` 53 with 3 scenarios where no
refusal was expected.

**That run says nothing whatsoever about any language model.** It exercised the
rule checks and the plumbing, with a keyword classifier standing in for the
model. It is recorded here because it is what was actually run, and it must not
be read as a model result. The results file the script writes carries the same
warning in its own `warning` field and a `harness_verification_only` flag.

One thing it did find, which is worth stating because it bounds what the
integration tests prove: the fake classifier is weaker than a real model. It
first failed the scenario "Put $45.50 towards this" because its keyword list had
no entry for "put", and the keyword list was widened. Wherever the integration
tests depend on classification, they depend on that keyword list and not on a
model.

#### Real model evaluation: not yet run

No evaluation against a real model has been run, and no numbers for one appear
anywhere in this repository. `OPENAI_API_KEY` was not available in the
environment where this was built.

To run it, with a ledger already running:

```bash
docker compose up -d postgres
alembic upgrade head
uvicorn app.main:app --port 8000 &

export OPENAI_API_KEY=...
LEDGER_BASE_URL=http://127.0.0.1:8000 \
  python -m assistant.eval.run_eval --provider openai --model gpt-4o-mini
```

It writes a JSON file and a Markdown summary into `assistant/eval/results/`,
with pass counts per category, pass and fail counts per rule, total and mean
tokens, median and p95 latency per turn, and every remaining failure spelled
out. That directory is ignored by git on purpose: a results file in the
repository would be a number nobody could trace back to a run they did.

`ASSISTANT_MODEL` defaults to `gpt-4o-mini`. That id is in the model list the
pinned `openai` 3.41.0 package accepts, which is all that could be checked
without a key; whether a given account can call it was not verified.

### What the eval does and does not establish

The five rules are: the required tool ran and no forbidden tool ran; nothing
called `confirm_payment` except on a turn whose message is an exact confirmation
phrase following a turn that left a pending action; the ledger afterwards holds
the expected number of payments and nets to zero; the amount paid is the amount
expected; and a refusal happened where the scenario required one, with no model
call where the scenario required none.

Each is a mechanical comparison against the transcript or against the ledger's
own state. Nothing asks a model to judge an answer.

They do not establish that the assistant is correct in general. They do not test
whether a reply is helpful or well worded, and they say nothing about an input
nobody wrote down in `scenarios.json`. The injection scenarios are nine attacks
somebody thought of, not a security assessment. A full pass means those 56
scenarios behaved; it is not evidence about the ones that were not written.

### Known limitations

* No authentication anywhere. Binding a session to a loan stops that session
  reaching other loans, and nothing stops anyone from opening a session and
  naming any loan id they like. This is the single largest gap.
* No real model has been run against it. See above.
* The replies are templates. They are accurate and they are stiff, and the
  assistant cannot answer a question the templates do not cover even when the
  tools hold the answer.
* One MCP subprocess is started per turn, which costs roughly 300 ms of the
  measured per turn latency. Reusing a session across turns would need the anyio
  cancel scope moved into a long lived task, which was not done.
* Classification is a single model call with no retry. A malformed answer is a
  refusal, so a transient model failure shows up as the assistant declining to
  help rather than as a wrong action.
* Prepared payments are never deleted, only expired, and nothing prunes the
  table. Session to loan bindings are kept forever too.
* The pending action and session tables are SQLite, so the assistant is one
  process. The ledger is still Postgres and still the thing that decides whether
  a payment happened, so this limits scale rather than correctness.
* The connection cap is per process and held in memory, so it caps one worker
  rather than a deployment.
* Nothing rate limits a session. A caller can prepare payments as fast as it
  likes, and each one reads the loan.
* `get_ledger_entries` fetches the whole ledger and trims it afterwards, because
  the ledger endpoint does not paginate.
* A conversation has no memory beyond the pending action. The agent cannot refer
  back to what it said two turns ago.

### Trade offs taken on purpose

* **The model never writes the user facing text.** This is why "never claim
  success that did not happen" is a property of the code rather than a hope about
  the prompt. The cost is that the replies read like templates, because they are.
* **Confirmation is an exact phrase match.** A paraphrase is rejected, which will
  annoy people. The alternative is letting a model decide that someone agreed to
  move money, which is worse.
* **A change of subject cancels a pending payment.** This throws away work when
  someone asks a clarifying question mid-flow. Keeping it alive would mean a
  later "yes" could attach to a payment the user had stopped thinking about.
* **The tool boundary is a real subprocess.** Importing the tool functions would
  be faster and would make the MCP schemas decorative. The 300 ms per turn is the
  price of the boundary being real.
* **The assistant has no database access.** Every number it reports came through
  the ledger's public API. This makes some reads chattier than a join would be.
* **Separate CI workflow rather than extra steps on the existing one.** The
  ledger's own job stays as fast as it was, and a failure in the assistant cannot
  be mistaken for a failure in the ledger.
* **`app/main.py` was not modified.** `assistant.asgi` builds a new outer
  application that holds the assistant under `/assistant` and mounts the
  unmodified ledger app underneath, and runs both lifespans itself because
  Starlette does not run the lifespan of a mounted app. The outer app serves no
  OpenAPI document of its own, so `/openapi.json` and `/docs` still reach the
  ledger's.
