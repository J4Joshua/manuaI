---
doc_id: SOP-1192
title: "Fault & Alarm Code Reference — Line 3 Labeler"
machine_id: labeler-line3
machine: "Label-Aire 3115NV Pressure-Sensitive Label Applicator (Line 3)"
doc_type: reference
revision: "3.0"
effective_date: "2026-04-10"
owner: "Packaging Maintenance"
safety_flag: false
related_docs: ["SOP-1187", "SOP-1190"]
source_refs:
  - {manual_id: "LA-3115NV", section: "Microprocessor Messages", page: 21}
  - {manual_id: "LA-3115NV", section: "Low Label Alarm Adjustment (Fig 20)", page: 62}
  - {manual_id: "LA-3115NV", section: "Encoder Pulse Resolution", page: 37}
---

# SOP-1192 — Fault & Alarm Code Reference, Line 3 Labeler

Codes shown on the 3115NV control panel and the approved first response. Full
descriptions in the *3115NV Manual, Microprocessor Messages, p.21*.

| Code | Meaning | Likely cause | First response | Procedure |
|---|---|---|---|---|
| **E-40** | Low label stock | Roll near empty | Reload label roll; re-thread | Inline / re-thread per Fig 8 |
| **E-41** | Label not detected | Sensor gain/position, clear liner | Adjust label sensor edge (p.31); clean lens | — |
| **E-42** | **Label-web jam at peel tip** | Web bunched/torn/wrapped at peel tip | **⚠ Lockout/Tagout, then clear** | **SOP-1187** |
| **E-43** | Air pressure low | Supply < 60 psi, leak | Check regulator/supply; do not bypass | SOP-1190 if servicing |
| **E-50** | Encoder / speed-following fault | Encoder signal loss, coupling slip | Inspect encoder & coupling | Maintenance (p.37) |

## Notes
- **E-42** and **E-43** involve hazardous energy — treat any peel-tip or pneumatic
  intervention as Lockout/Tagout work (**SOP-1190**).
- There is **no approved procedure** to defeat a guard interlock or run with a fault
  forced/overridden. Repeated faults escalate to maintenance.
