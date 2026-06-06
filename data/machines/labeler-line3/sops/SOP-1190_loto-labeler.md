---
doc_id: SOP-1190
title: "Lockout/Tagout — Line 3 Labeler (Label-Aire 3115NV)"
machine_id: labeler-line3
machine: "Label-Aire 3115NV Pressure-Sensitive Label Applicator (Line 3)"
doc_type: sop
revision: "1.4"
effective_date: "2026-02-01"
owner: "EHS / Safety"
safety_flag: true
related_docs: ["SOP-1187"]
basis: "OSHA 29 CFR 1910.147 (Control of Hazardous Energy); see data/reference/osha-loto-sample.pdf"
energy_sources:
  - {type: "electrical", magnitude: "115/230 VAC", isolation: "wall disconnect, rear of Line 3 panel"}
  - {type: "pneumatic", magnitude: "~60 psi", isolation: "air supply valve + bleed at regulator"}
---

# SOP-1190 — Lockout/Tagout, Line 3 Labeler

## §1 Purpose
Establish the minimum steps to isolate **all hazardous energy** on the Line 3
Label-Aire 3115NV before any servicing where unexpected start-up or release of stored
energy could cause injury. Conforms to OSHA 1910.147.

## §2 Energy Sources on This Machine
| Source | Magnitude | Isolating device | Stored energy |
|---|---|---|---|
| Electrical | 115/230 VAC | Locking disconnect, rear of Line 3 control panel | Capacitors (drive) |
| Pneumatic | ~60 psi | Air supply shutoff valve (inlet) | Pressurized air in lines/tamp |

## §3 Sequence of Lockout
1. **Notify** all affected operators that the labeler must be shut down for servicing.
2. **Identify** the energy types above and their hazards.
3. **Shut down** by the normal stop, then line E-STOP.
4. **Isolate** — open the electrical disconnect; close the air supply valve.
5. **Lock & tag** each isolating device with your assigned personal lock and tag.
6. **Release stored energy — ⚠ bleed the pneumatic line to 0 psi** at the regulator
   gauge; allow drive capacitors to discharge.
7. **Verify zero energy** — attempt a normal start (control returns to OFF after);
   confirm the gauge reads 0 psi and the applicator will not actuate.
8. The machine is now locked out.

## §4 Restoring to Service
1. Confirm tools removed and guards reinstalled.
2. Confirm all personnel clear of the applicator.
3. Confirm controls are OFF/neutral.
4. Each worker removes **their own** lock/tag; re-energize electrical, then re-open air.
5. Notify affected operators the machine is back in service.

## §5 Group Lockout
If more than one person services the machine, each applies their own lock to a group
hasp / lockbox; each removes only their own lock when finished.
