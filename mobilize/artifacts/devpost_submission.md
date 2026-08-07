# Devpost submission copy — mobilize

Paste-ready text for each Devpost narrative field. Every number here is
reproducible from the repo; nothing is estimated or rounded up.

---

## Tagline (one line, shown under the project name)

A human coordinator can hold one phone conversation. `mobilize` holds forty
— and knows which "yes" is real.

---

## Inspiration

When a hospital needs three O-negative donors in the next hour, someone
picks up a phone and starts working down a registry — one number at a time.
Most don't answer. Many are ineligible. And the ones who say "yeah, I'll try
to swing by" often never arrive.

The bottleneck isn't the registry. It's that a human coordinator can only
hold one conversation at a time. Industry guidance puts a full manual call
tree at up to three and a half hours to propagate, with roughly a third of
people missed on the first pass.

CALL-E removes that constraint entirely — software can hold forty
conversations at once. But once you're calling forty people simultaneously,
a second problem shows up that nobody has built for: **a stated "yes" is not
a confirmation.** People agree to be polite and then don't show up.
Acquiescence bias is real and measured in volunteer and donor recruitment.
If you just count stated yeses, you fill your quota with people who never
arrive.

That's the actual problem I wanted to solve: not "make calls faster," but
"get *N* people who will genuinely turn up, before a deadline, without
over-calling a registry into fatigue."

## What it does

`mobilize` takes a need — *3 confirmed donors, within 60 minutes, from this
consented registry* — and:

1. **Ranks** the pool by eligibility window, distance, and historical
   follow-through.
2. **Dispatches a wave in parallel** (real concurrent `asyncio` dispatch, not
   a loop), sized so expected confirmations clear the need with a margin.
3. **Scores how firm each "yes" is** from the call's own evidence text —
   "I'm leaving now, ten minutes" outranks "yeah, I'll try" — blended with
   that person's historical show-up rate.
4. **Stops the moment the need is met.** No further wave is dispatched;
   everyone else in the registry is simply never called.
5. **Survives crashes** — a write-ahead ledger plus CALL-E's own
   idempotency keys mean a process killed mid-dispatch never double-dials
   anyone on restart.

It ships as a Python library, a CLI, a live WebSocket dashboard, an MCP
server (so any agent can trigger a mobilization as a tool), and a reusable
CALL-E Agent Skill.

## How I built it

**Day one was reading, not writing.** Before designing anything I pulled
CALL-E's OpenAPI spec and found something that changed the architecture:
there is no endpoint to cancel an in-flight call. `POST /v1/calls`,
`GET /v1/calls/{id}`, `GET /v1/calls/{id}/events` is the entire surface.

That killed my original design ("fire 25 calls, cancel the rest the instant
we have 3"). So the system became **wave-based** instead: dispatch a sized
wave, wait, and only dispatch another if still short. It's honest about what
the platform can actually do, and the README and Skill both explicitly warn
against describing it as cancellation.

The engine talks to a `Transport` interface with two implementations: the
real CALL-E transport (async REST — the SDK's `create_and_wait` blocks,
which makes true parallel dispatch impossible), and a simulator with a
synthetic population whose *true* show-up probability is known but hidden
from the system. Identical code path either way.

That simulator is what made rigorous validation possible on a 20-free-call
budget: **300 simulated mobilizations, zero cost, reproducible with one
command.**

## Challenges I ran into

**The 20-call budget.** You cannot validate a policy on 20 calls. Rather
than hand-wave it, I built the evaluation harness with known ground truth
and spent real calls only where they were irreplaceable — proving the
integration works, and demo footage.

**A stated yes is a noisy signal, and I had to prove that mattered.** It
would have been easy to assert commitment scoring helps. Instead the harness
measures it against baselines:

| Policy | Confirmation accuracy | Calls used |
|---|---|---|
| **Calibrated (mobilize)** | **94.6%** | 11.0 |
| Trust every stated yes | 87.7% | 6.8 |
| Call the entire pool | 80.2% | 40.0 |

*Confirmation accuracy* = of the people the system believed were confirmed,
what fraction would actually show up. The naive policy fills just as
reliably — it just fills with people who don't come.

**Three rounds of external code review.** A contributor on CALL-E's
repository reviewed my PR and found real bugs — three separate times. Among
them: my wave dispatch was awaiting each call sequentially, silently
serializing the exact parallelism the project is named for; my idempotency
fix was undermined by a non-deterministic mobilization ID; and my
timezone-aware calling-hours check was never actually wired into the live
entry points, so every real recipient silently defaulted to UTC. All fixed,
each with a dedicated test. I also audited my own fixes between rounds and
caught two more myself.

## Accomplishments that I'm proud of

**The crash-safety proof is real, not claimed.** A test `SIGKILL`s an actual
subprocess mid-dispatch, restarts it against the same ledger, and asserts
zero duplicate dials and zero lost confirmations.

**The second real call landed at 0.54 — just under the 0.55 threshold.** The
system correctly refused to count it as confirmed. That's the calibration
model discriminating at a genuine decision boundary on a real conversation,
not a synthetic one. The transcript is committed in the repo.

**Consent is enforced in code, not promised in a doc.** Do-not-call,
cooldowns, contact-fatigue limits, and per-recipient-timezone calling
windows all run before dispatch, on by default, persisted across
invocations. A mid-call "don't contact me again" is detected and written to
a permanent do-not-call list immediately.

**65 tests, all passing from a clean install.**

## What I learned

That the interesting problems in agent-driven calling aren't in the calling
— they're in everything around it. Reading the OpenAPI spec before designing
saved me from building on an assumption that was simply false. And having a
sharp external reviewer find bugs three times running was the most valuable
part of the whole build; several of those were failures where the mechanism
*existed* but nothing actually invoked it, which is the exact class of bug
that passes a casual read.

## What's next

The engine is domain-agnostic — the same wave dispatch, commitment
calibration, and governance apply to emergency shift coverage, volunteer
disaster mobilization, and on-call escalation. Next steps: validating the
commitment-calibration curve against a larger set of real calls (it's
currently tuned against the simulator's generative assumptions), a
Prometheus exporter, and a learned rather than hand-tuned ranking model.

## Built With

`python` · `asyncio` · `call-e` · `mcp` · `fastapi` · `websockets` ·
`pytest` · `hypothesis`

## Try it out

- GitHub: https://github.com/AshrafAhmed9/mobilize
- Submission PR: https://github.com/CALLE-AI/awesome-phone-call-agents/pull/98
