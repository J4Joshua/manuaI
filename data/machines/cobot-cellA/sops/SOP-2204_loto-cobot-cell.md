---
doc_id: SOP-2204
title: "Energy Isolation / Lockout/Tagout — Cell A Cobot Cell (UR20)"
machine_id: cobot-cellA
machine: "Universal Robots UR20 Pick-and-Place Cobot (Cell A)"
doc_type: sop
revision: "1.1"
effective_date: "2026-02-15"
owner: "EHS / Safety"
safety_flag: true
related_docs: ["SOP-2201"]
basis: "OSHA 29 CFR 1910.147; see data/reference/osha-loto-sample.pdf"
source_refs:
  - {manual_id: "UR20-UM", section: "Movement Without Drive Power (Backdrive)", page: 26}
  - {manual_id: "UR20-UM", section: "Maintenance and Repair", page: 86}
energy_sources:
  - {type: "electrical", magnitude: "mains to control box", isolation: "cell main disconnect"}
  - {type: "pneumatic", magnitude: "gripper air supply", isolation: "gripper air shutoff + bleed"}
  - {type: "gravitational", magnitude: "arm pose", isolation: "support/backdrive arm to safe rest"}
---

# SOP-2204 — Energy Isolation / Lockout/Tagout, Cell A Cobot Cell

## §1 Purpose
Isolate all hazardous energy on the **Cell A UR20** cell before mechanical, electrical,
or gripper service. Conforms to OSHA 1910.147.

## §2 Energy Sources
| Source | Isolating device | Stored energy |
|---|---|---|
| Electrical (mains) | Cell main disconnect → control box | Control-box capacitors |
| Pneumatic (gripper) | Gripper air shutoff valve | Pressurized air at gripper |
| Gravitational (arm) | Physically support / backdrive arm | Arm weight at pose |

## §3 Sequence of Lockout
1. **Notify** affected operators; halt the program from the teach pendant.
2. **Move** the arm to a stable rest pose (low, supported) where it cannot fall.
3. **Isolate** — open the cell main disconnect; close the gripper air valve.
4. **Lock & tag** each isolating device with your personal lock and tag.
5. **Release stored energy** — bleed gripper air to 0 psi; allow control-box capacitors
   to discharge. If the arm must be repositioned with power off, use the documented
   **backdrive / movement-without-drive-power** method (ref. *UR20 UM, p.26*) — never
   force a joint.
6. **Verify** — confirm the controller is dead and the gripper will not actuate.
7. The cell is now locked out.

## §4 Restoring to Service
1. Reinstall guarding; confirm tools removed.
2. Confirm all personnel clear of the operating space.
3. Each worker removes **their own** lock/tag.
4. Restore electrical, then air; re-initialize the robot per **SOP-2201 §4.3**.
5. Notify affected operators; run one slow verification cycle.
