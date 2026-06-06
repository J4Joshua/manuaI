---
doc_id: SOP-2201
title: "Protective Stop & Fault Recovery — Cell A Cobot (UR20)"
machine_id: cobot-cellA
machine: "Universal Robots UR20 Pick-and-Place Cobot (Cell A)"
doc_type: sop
revision: "1.2"
effective_date: "2026-03-20"
owner: "Automation Maintenance"
safety_flag: true
fault_codes: ["C4", "C50", "C153"]
related_docs: ["SOP-2204"]
source_refs:
  - {manual_id: "UR-ERRCODES", section: "Error Codes Directory (C-codes)", page: null}
  - {manual_id: "UR20-UM", section: "Emergency Stop", page: 25}
  - {manual_id: "UR20-UM", section: "Safety-related Functions and Interfaces", page: 27}
  - {manual_id: "UR20-UM", section: "Log Tab", page: 337}
---

# SOP-2201 — Protective Stop & Fault Recovery, Cell A Cobot

## §1 Purpose & Scope
Recovery from a **protective stop** or controller fault on the **Cell A UR20** cobot.
For line operators and automation maintenance.

## §2 Identify the Fault
Read the fault code on the PolyScope **Log Tab** (ref. *UR20 User Manual, Log Tab,
p.337*). Look it up in the **Error Codes Directory** (each code is its own section,
e.g. *§1.5 C4 Communication issue*, *§1.35 C50 Robot powerup issue*). Note the code
before clearing — it determines the cause and fix.

| Code | Meaning (per Error Codes Directory) | First response |
|---|---|---|
| **C4** | Communication issue | Check robot/base-flange cable seating; restart controller |
| **C50** | Robot power-up issue | Power-cycle control box; verify mains; re-initialize |
| **C153** | Safety-related fault / protective stop | Identify trip cause before re-enabling |

## §3 Safety — ⚠ THE ARM CAN MOVE
A cobot under fault may resume motion when re-enabled. Before approaching:
- Keep clear of the operating space; know the nearest **Emergency Stop** (ref.
  *UR20 UM, Emergency Stop, p.25*).
- If servicing electrical/pneumatic/gripper hardware, isolate energy per **SOP-2204**.
- Do **not** alter safety settings to clear a stop (see §5).

## §4 Procedure
**§4.1** From a safe position, read and record the fault code (§2).
**§4.2** Clear the physical cause (remove obstruction, reseat cable, clear the part
jam at the gripper). If servicing hardware, complete **SOP-2204** first.
**§4.3** On the teach pendant, acknowledge the fault and **re-initialize** the robot.
**§4.4** Verify the cobot returns to its **Safe Home Position**, then run one slow
cycle before resuming production.

## §5 Escalate — Do Not Bypass Safety
If the protective stop repeats, or the cause is a **safety-rated limit, safety plane,
or safeguard-stop input**, **stop and escalate to automation maintenance.** There is
**no approved procedure** for an operator to disable, widen, or bypass the cobot's
safety-rated soft limits or safeguard stop. Safety configuration is password-protected
and may only be changed under a documented risk assessment (ref. *UR20 UM, Software
Safety Configuration §21.1, and General Warnings & Cautions §2.4*).
