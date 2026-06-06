# ManuAI — Starter Corpus

The documents ManuAI retrieves over. Two machines, so we can demo **per-machine
retrieval scoping** (`machine_id`) — the part judges will poke at on Moss.

## Layout
```
data/
  manifest.json                     # per-doc metadata -> drives ingestion + machine_id filtering
  reference/
    osha-loto-sample.pdf            # real OSHA 1910.147 template (provenance for our LOTO SOPs)
  machines/
    labeler-line3/                  # machine_id: labeler-line3  (Label-Aire 3115NV)
      manuals/label-aire-3115NV.pdf #   REAL OEM manual (128pp, table/figure-heavy -> Unsiloed showcase)
      sops/SOP-1187_label-jam-E42.md      #   jam clear, LOTO-first, defines the demo
      sops/SOP-1190_loto-labeler.md       #   Lockout/Tagout (safety banner source)
      sops/SOP-1192_fault-code-reference.md #  fault table: E-42 -> SOP-1187
    cobot-cellA/                    # machine_id: cobot-cellA   (Universal Robots UR20)
      manuals/ur-error-codes-directory.pdf  # REAL: every C-code is its own numbered section
      manuals/ur20-user-manual.pdf          # REAL 365pp w/ Safety ch.2 + Software Safety §21
      sops/SOP-2201_protective-stop-recovery.md  # fault recovery, cites real C-codes
      sops/SOP-2204_loto-cobot-cell.md           # energy isolation (incl. arm/backdrive)
```

## Provenance (what's real vs. authored)
- **Real OEM/OSHA PDFs** (`provenance: real-oem` / `real-osha` in manifest): the
  Label-Aire manual, both UR manuals, the OSHA LOTO sample. These are messy, real,
  table-rich → they're what proves the Unsiloed parsing step earns its place.
- **Authored Markdown SOPs** (`provenance: authored`): we wrote these so the demo
  beats land *exactly* — crisp `SOP-#### §x` citations, a guaranteed safety banner,
  and a controllable refusal. They cross-reference real manual pages for grounding.

This blend is deliberate: real docs for the parsing story, authored docs for demo control.

## The two demo scenarios (see `manifest.json` → `demo_scenarios`)
1. **Labeler:** *"The labeler on line 3 jammed and threw error E-42."*
   → retrieves SOP-1192 (E-42) + SOP-1187 → answer is LOTO-first, cites **SOP-1187**,
   ⚠ banner from SOP-1190.
2. **Cobot:** *"The pick-and-place robot in cell A stopped and shows fault C4."*
   → retrieves UR Error Codes §1.5 (C4) + SOP-2201 → reseat-comms/restart, cites both.

`machine_id` keeps these from cross-contaminating: a labeler query must not surface
cobot docs and vice-versa. That filter is the Moss demo.

## ⚠ Intentional gaps — DO NOT FILL
There is **no document** covering *"bypass the safety interlock"* (labeler) or
*"disable/widen the safety-rated limits"* (cobot). The **absence is the feature** — it
triggers the cite-or-refuse → escalate-to-supervisor beat. Please don't "helpfully"
add an SOP for these. (See `manifest.json` → `intentional_gaps`.)

## Licensing
The OEM/OSHA PDFs are third-party copyrighted/public docs used here for a hackathon
demo. This actually *strengthens* the pitch: the customer brings their own proprietary
manuals and **nothing leaves the box**. Don't redistribute the PDFs outside the demo.

## Extending
Add a machine: create `machines/<machine_id>/{manuals,sops}/`, drop docs, add entries
to `manifest.json`. Keep `machine_id` consistent — it's the retrieval filter key.
