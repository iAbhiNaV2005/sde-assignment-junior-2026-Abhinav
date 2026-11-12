# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- Google Antigravity / Gemini 3.8 Flash

## What I used them for
- **Scaffolding and boilerplate**: Generating initial frozen dataclasses for `AppConfig`, `WindowConfig`, `TenantConfig`, `CampaignConfig`, and `LeadRecord`.
- **FastAPI Webhook Server**: Scaffolding the FastAPI app (`POST /webhooks/telephony`), raw body extraction, and `GET /status` console metrics.
- **Visual Status Console UI**: Designing a responsive, dark-mode real-time status console dashboard (`GET /`) showing tenant concurrency meters, campaign progress bars, recent calls table, and carrier report statistics.
- **Test Suite Drafting**: Generating unit test fixtures and parameterized test cases for pure calling window logic (`test_windows.py`), priority ranking rules (`test_priority.py`), and R9 precedence resolution (`test_policy.py`).
- **Chaos Test Harness**: Drafting the subprocess orchestration harness (`solution/chaos/run_chaos.py`) that manages mock carrier and dispatcher lifecycles, injects ungraceful kills (`proc.kill()`), and verifies exact matching against `/admin/leads`.

## What I wrote or decided myself
- **Write-ahead reservation & at-most-once dialling state machine**: Designed the atomic reservation pattern using SQLite WAL mode with `BEGIN IMMEDIATE` and write-ahead commits (`DIALING` state) *before* issuing carrier HTTP requests.
- **Derived invariants architecture**: Decided against mutable tenant call counters or phone lock flags. Concurrency and phone busy mutexes are calculated directly from active rows (`WHERE state IN ('DIALING', 'UNKNOWN', 'ACTIVE')`), rendering the system mathematically immune to counter drift or desynchronization across crashes.
- **Pre-flight window safety margin**: Realized that the carrier sleeps 2.0 real seconds during simulated 504 timeouts (which equates to 3,600 simulated seconds at 1,800× speed). Designed both the reservation query and the pre-flight dispatch guard to enforce a dynamic safety margin (`max(120.0, 3.0 * clock.speed)`), completely eliminating `outside_calling_window` violations.
- **504 Gateway Timeout ambiguity resolution**: Architected the immediate carrier query probe and background reconciler. If carrier returns 404, the reservation is released back to `PENDING` without burning an attempt. If carrier returns 200, the call SID is adopted and its terminal state applied.
- **R9 Precedence Matrix**: Explicitly designed the deterministic priority rules: terminal disposition (`completed`) > attempt cap (≥ 3, `exhausted`) > sibling DND opt-out (`dnd_blocked`).
- **Carrier lost webhook recovery**: Designed periodic reconciliation for active calls (>450 simulated seconds) to recover the 3% lost webhooks without burning extra attempts or deadlocking tenant concurrency slots.

## Where the AI was wrong or unhelpful
- **Calling window boundary check latency**: The initial AI-generated dispatch loop checked calling windows only once during SQLite reservation. When a 504 or network queue delay occurred on an in-flight batch, subsequent calls in that batch were fired slightly past the campaign window boundary, triggering a 422 `outside_calling_window` carrier violation on `zen_0057`. I fixed this by adding a secondary pre-flight check immediately before the HTTP POST with a dynamic margin proportional to clock speed.
- **Windows SQLite file lock contention**: The AI wrote SQLite connection handling with module-level persistent connections, which caused `database is locked` / `busy_timeout` errors on Windows during multi-task async execution. I refactored the database access into an explicit context manager (`try ... finally: conn.close()`) with `PRAGMA busy_timeout = 5000` and `PRAGMA synchronous = FULL`.
- **UNKNOWN call state release**: The initial reconciler implementation released reservations only `WHERE state = 'DIALING'`. When a 504 occurred and marked the call `UNKNOWN`, if the carrier subsequently returned 404, the call remained stuck in `UNKNOWN` and was repeatedly polled every 1.5 seconds. I caught this during chaos debugging and updated both `release_dialing()` and `record_accept()` to operate on `state IN ('DIALING', 'UNKNOWN')`.

## How I verified AI-generated code
- **Line-by-line inspection**: Audited every SQL query, atomic transaction boundary, HMAC signature computation, and state transition.
- **Comprehensive Unit Tests**: Executed 21 unit tests via `pytest solution/tests/ -v` covering window calculations, priority ordering, retry policies, store invariants, HMAC verification, and crash recovery (100% pass rate in <0.7s).
- **Chaos Stress Testing**: Ran automated chaos runs with simulated 1,800× time acceleration, injected 503 service unavailabilities, 504 gateway timeouts, 3% webhook loss, and 3 ungraceful hard-kills (`proc.kill()`).
- **Zero-Tolerance Diffing**: Verified that the final output `final_state.csv` has zero violations (`violation_count == 0` on `GET /admin/report`) and matches `GET /admin/leads` with 100% accuracy across all 200 leads on both seed 7 and unseen seeds.
