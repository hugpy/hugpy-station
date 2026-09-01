# Query Arbitration SOP — mid-turn messages from C

**The contract for what B does when C sends a message while A's turn is in
flight.** It exists because A's turns are atomic: a one-shot `claude -p` process
consumes its prompt at launch and cannot listen mid-turn. Anything C says while
A works must therefore be *disposed of* by B — and the governing principle is:

> **B infers wording; C dictates disposition.**
> B may compress, collate, and rephrase. B may never, on its own inference,
> destroy paid work (interrupt) or silently defer a request (capture).

## The four lanes

Every interim message goes down exactly one lane:

| lane | who selects | what happens |
|---|---|---|
| **1 · INTERRUPT** | C, explicitly | A's in-flight process is killed. The next turn opens with a **reconcile brief**: what was aborted and why, that the workspace may hold partial changes toward the abandoned goal, and the corrected goal. |
| **2 · COALESCE** | default | The message folds into the **next turn slot** as part of an explicit digest. The current turn runs to completion. |
| **3 · APPEND** | C, explicitly | An unrelated request gets its **own turn after** current work and any pending digest. Never blended into a collation. |
| **4 · CAPTURE** | C, explicitly | **No turn at all.** The request lands on the to-do board (per TODO-BOARD-SOP.md) with enough context to be actionable cold. The "don't interrupt, don't lose it" lane. |

C's selection is **a floor and a ceiling**: B never promotes a lane
(coalesce → interrupt is forbidden) and never demotes one (an interrupt-marked
message may not be quietly coalesced; a coalesce-lane message may not be
quietly captured). If a selected lane is impossible (e.g. `!` arrives after the
turn already finished), B takes the nearest *weaker* lane and says so.

## Selection vocabulary (prefix on the interim message)

| prefix | lane |
|---|---|
| `!` | interrupt |
| *(none)* | coalesce |
| `+` | append |
| `todo:` or `?` | capture |

Unprefixed defaults to coalesce because it is the safe lane: nothing is
destroyed, nothing is silently deferred.

## Collation rules (inside the coalesce lane)

B's job is to compile the query C *would have written* with time to write it —
**compile intent, keep provenance**:

1. **Superseded instructions**: drop the body, keep the delta when it
   disambiguates ("X, *not* Y" — the "not Y" earns its tokens).
2. **Affect**: keep it only when it changes A's behavior. Frustration =
   "current approach is failing, change strategy" — keep. "My bad" = "the
   correction was C's own misstatement, not a reaction to your output; the
   correction is final" — compress to a provenance note. Pure politeness —
   drop.
3. **When ambiguous, forward, don't infer.** A verbatim passthrough of three
   short messages costs less than one wrong collation. If a message could be
   countermanding vs. amending, coalesce it and flag the ambiguity in the
   digest.
4. **Always mark the digest.** A must be able to tell it received a collation,
   not C's verbatim words. Everything explicit — B's edits of C's words
   included.

## The reconcile brief (after an interrupt)

A is amnesiac between turns: an interrupted one-shot turn leaves partial side
effects on disk and **no memory that it happened**. The brief is the amnesiac
architecture's replacement for the continuity an interactive session gets for
free. It must state:

- which turn was aborted, and that it was C's explicit choice;
- that the workspace may contain partial changes toward the abandoned
  instruction (pointer to the aborted prompt);
- the corrected goal;
- an instruction to verify workspace state before building on it.

## Interrupt economics (for C, and for B's advice to C)

The tokens already spent on a doomed turn are sunk either way; interrupt only
reclaims the *remainder*. Rules of thumb:

- turn nearly done, or output salvageable → let it finish, coalesce;
- turn just started, or every further token compounds cleanup → interrupt;
- the new message *amends* rather than countermands → never interrupt.

Cheap proxies: elapsed time (how much spend is still ahead) and whether the
turn has written files yet (how mutating it has been — visible in the exchange
dir / workspace mtimes).

## Where this is implemented

- **mct2** (pointer-exchange test grounds, `mct2_repl.py`) is the proving
  ground: a deterministic B — lanes, digests, and reconcile briefs are
  mechanical (verbatim joins with explicit framing), so the *contract* is
  exercised even though no inference happens.
- **mct** (the real broker) is where collation rules 1–3 become actual
  inference. Until it implements this SOP, mct has no mid-turn channel and
  every message waits for the next turn boundary.
