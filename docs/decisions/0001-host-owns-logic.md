# 0001: the host owns the logic; the panel is a thin client

Date: 2026-10-21. Status: accepted.

## Context

Premiere Pro can be extended through CEP (HTML panel plus ExtendScript) today and through UXP later; Adobe plans
to retire CEP a year after UXP reaches parity, with no date. UXP in 2026 lacks captions and parts of the timeline
API. The product must grow module by module for years and must update itself without the user handling Adobe's
extension installers.

## Decision

- All logic, state and scripts live in the Windows host application (`PremiereDeck.exe`). The CEP extension only
  renders a UI tree the host sends, executes the ExtendScript the host sends and forwards Premiere events
  (`docs/PROTOCOL.md`).
- A new module changes the host only; the panel is updated by the host as part of the exe's self-update.
- The UI inside Premiere is flat and plain (no Liquid UI kit, no animation): it is a utility surface inside
  someone else's application, and the platform it runs on is the one being retired.
- Scripts are shipped as text per operation so they can be replaced one by one when the scripting layer changes.

## Consequences

- A UXP port replaces one thin layer and the jsx files, not the product.
- Every Premiere operation costs a round trip over localhost (milliseconds); operations are batched in scripts
  where it matters (snapshot, bulk relabel).
- The panel cannot work without the host running; the agent starts the host when it is missing.
