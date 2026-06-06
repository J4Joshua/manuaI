---
doc_id: SOP-1187
title: "Clearing a Label-Web Jam (Fault E-42) — Line 3 Labeler"
machine_id: labeler-line3
machine: "Label-Aire 3115NV Pressure-Sensitive Label Applicator (Line 3)"
doc_type: sop
revision: "2.1"
effective_date: "2026-03-15"
owner: "Packaging Maintenance"
safety_flag: true            # surfaces the ⚠ Lockout/Tagout banner
fault_codes: ["E-42"]
related_docs: ["SOP-1190", "SOP-1192"]
source_refs:
  - {manual_id: "LA-3115NV", section: "Microprocessor Messages", page: 21}
  - {manual_id: "LA-3115NV", section: "Label Sensor & Peel Tip Assy (Fig 19)", page: 59}
  - {manual_id: "LA-3115NV", section: "Threading Diagram (Fig 8 / Fig 9)", page: 29}
---

# SOP-1187 — Clearing a Label-Web Jam (Fault E-42), Line 3 Labeler

## §1 Purpose & Scope
This procedure covers clearing a jammed label web at the peel tip / applicator of the
**Line 3 Label-Aire 3115NV** when the controller displays **Fault E-42 (label-web jam
at peel tip)**. It applies to line operators and maintenance technicians.

## §2 Fault Identification
**E-42** indicates the label web has bunched, torn, or wrapped at the peel tip so the
web is no longer advancing. The control panel halts the applicator and displays the
fault (ref. *3115NV Manual, Microprocessor Messages, p.21*). Confirm by inspecting
the peel tip and label sensor (ref. *Fig 19, p.59*). For the full code list, see
**SOP-1192**.

## §3 Safety — ⚠ LOCKOUT/TAGOUT REQUIRED BEFORE CLEARING
The 3115NV carries **two hazardous energy sources**: 115/230 VAC electrical and
**~60 psi pneumatic** (air-assist / tamp). The web must never be cleared with the
machine live — the applicator can actuate without warning.
**You must complete the Lockout/Tagout in SOP-1190 — including bleeding the
pneumatic supply to 0 psi — before reaching into the peel-tip area.**

## §4 Procedure
**§4.1 — Stop the line.** Press the labeler **STOP**, then the line **E-STOP**. Notify
affected operators that the labeler is going down for service.

**§4.2 — Lock out and bleed (REQUIRED).** Perform **SOP-1190 Lockout/Tagout** on the
Line 3 labeler: open and lock the electrical disconnect, close and lock the air supply
valve, and **bleed residual air to 0 psi** at the regulator gauge. Verify zero energy.

**§4.3 — Clear the jam.** With energy isolated, open the guard. Gently remove the
jammed/torn web from the peel tip and vacuum grid. Do not use metal tools against the
peel edge. Inspect the label sensor lens for adhesive (ref. *Fig 19, p.59*).

**§4.4 — Re-thread.** Re-thread the web per the threading diagram (ref. *Fig 8/Fig 9,
p.29–30*). Confirm the web seats under the label sensor and over the peel tip.

**§4.5 — Restore to service.** Remove locks/tags and restore energy per **SOP-1190**
(restore section). Reset the fault, run **3 test products** at reduced line speed, and
confirm labels place within tolerance before returning to full speed.

## §5 If the Fault Will Not Clear
If E-42 returns after two clearing attempts, or the web tears repeatedly, **stop and
escalate to maintenance** — do not modify peel-tip geometry, defeat the guard
interlock, or run the applicator with the guard open. There is no approved procedure
for operating this machine with safety guards bypassed.
