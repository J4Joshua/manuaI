#!/usr/bin/env python3
"""render(screen_state) — terminal renderer (ARCHITECTURE.md §3b consumer).

The renderer never changes between phases: it programs ONLY to the screen_state dict
(§3b), never to the retriever or the model. Mirrors the style of the M1 ask.py /
moss_demo.py output: a transcript line with top_score+threshold+status, then either a
[STOP] escalation block or the answered card (safety banner ▸ answer ▸ steps ▸ cite ▸ source).
"""

W = 70


def _thr(state):
    t = state.get("threshold")
    return "n/a" if t is None else f"{t:.2f}"


def _corroboration(state):
    """Supplemental panel: prior operator incidents (the `chats` index). Additive —
    never a citation; shown beneath the SOP-grounded answer / escalation."""
    corr = state.get("corroboration") or []
    if not corr:
        return
    print("-" * W)
    print("  [prior incidents]  (operator chats — corroboration only, not a citation)")
    note = state.get("corroboration_note")
    if note:
        print(f"      ✓ {note}")
    for c in corr:
        print(f"      · {c['chat_id']} (match {c.get('score', 0):.2f}) — {c.get('summary','')}")


def render(state):
    print()
    print(f'[mic] Operator: "{state["question"]}"   [machine: {state["machine_id"]}]')
    print(
        f"      top_score={state['top_score']:.3f}   threshold={_thr(state)}   "
        f"status={state['status'].upper()}"
    )

    if state["status"] == "escalated":
        print("=" * W)
        print("  [STOP]  NO APPROVED PROCEDURE — ESCALATING TO SUPERVISOR")
        print("=" * W)
        print(f"  {state['answer']}")
        print("  Supervisor flagged. Do not improvise on safety-critical steps.")
        _corroboration(state)
        print("=" * W)
        print()
        return

    # ---- answered ----
    print("=" * W)
    if state.get("safety_flag") or state.get("safety_warnings"):
        print("  [!] SAFETY FIRST — lockout/tagout applies")
        for w in state.get("safety_warnings", []):
            print(f"      - {w}")
        print("-" * W)

    print("  [answer]")
    print(f"      {state['answer']}")

    if state.get("steps"):
        src = state.get("steps_source") or {}
        hdr = " ".join(x for x in (src.get("sop_id"), src.get("section")) if x)
        title = src.get("procedure_title")
        print("-" * W)
        print(f"  [steps]  {hdr}{f' — {title}' if title else ''}")
        for i, s in enumerate(state["steps"], 1):
            print(f"      {i}. {s}")

    cites = state.get("citations", [])
    print("-" * W)
    print("  [cite]" if len(cites) == 1 else "  [cites]")
    for c in cites:
        page = f" (p.{c['page']})" if c.get("page") is not None else ""
        print(f"      {c['sop_id']} {c['section']}{page} — {c['procedure_title']}")

    excerpt = state.get("source_excerpt") or ""
    if excerpt:
        snippet = " ".join(excerpt.split())
        if len(snippet) > 280:
            snippet = snippet[:280].rstrip() + "…"
        print("-" * W)
        print("  [source]")
        print(f"      {snippet}")

    _corroboration(state)
    print("=" * W)
    print()
