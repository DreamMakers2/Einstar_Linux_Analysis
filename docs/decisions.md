# Technical decisions (ADRs)

## ADR-001 — Treat the `.img` as a transport container, not an FX3 image

**Context:** the raw file's section chain fails at file offset 0x2534 and a plausible header
appears at 0x2536.
**Decision:** model the file as 4097-byte records (4096 payload + 1 checksum) and de-interleave
before parsing.
**Evidence:** 320/320 record checksums valid; the de-interleaved image parses as a documented
AN76405 FX3 image and its 32-bit checksum validates. The vendor's updater performs exactly this
per-record check (`FUN_18000bc80`).
**Consequence:** `fx3img.py` de-interleaves by default; raw images (recovery/boot app) are
auto-detected by size and parse directly.

## ADR-002 — Use generated ELF32-ARM images for firmware analysis

**Context:** the FX3 image is a set of sections for disjoint memories (ITCM at 0x100, SRAM at
0x40003000+). A flat raw import gives wrong addresses and no ARM mode.
**Decision:** emit a minimal ELF32-ARM with one PT_LOAD per section, congruent file offsets, and
the real entry point.
**Consequence:** Ghidra recovers 518 functions at correct addresses; the same ELF works for
IDA/objdump; regeneration is one command.

## ADR-003 — The Windows device plugin is the protocol authority; libeinstar is the cross-check

**Context:** two independent protocol descriptions exist.
**Decision:** take opcode/wire/reply layout from `einscan10.2.0.0.dll` (vendor, decompiled) and
require agreement with `libeinstar` before raising confidence; document disagreements.
**Result:** layouts agree (group byte `0x10`, opcode byte 3, status byte 4, value at 9). One
refinement: the reply length is `u32 BE` at offsets 5..8, not a single byte at 8 (equal for all
observed lengths < 256).

## ADR-004 — Do not assume "newer firmware exists"

**Context:** the task brief hypothesised FX3 ≥ V2.14.
**Decision:** search official distribution only, verify hashes, and report a negative if no such
artifact exists.
**Result:** no V2.14+ exists; V2.10/V3.7 is the final release for this product. Documented in
`docs/firmware/official_versions.md`, and the `V2.14` string in libeinstar is identified as an
example, not an observation.

## ADR-005 — Calibration: never substitute guessed values to make geometry pass

**Decision:** the decoder must reject data that violates orthonormality/epipolar invariants
rather than silently "fixing" it. Non-orthonormal IR rotations are treated as a *format or
interpretation* bug to be resolved, not as noise to be normalised away.
**Status:** decoder work in progress (`docs/calibration/`); the invariant suite is part of the
acceptance criteria.

## ADR-006 — Separate acquisition from processing behind a stable frame ABI

**Decision:** the new backend owns USB + reassembly + timestamps and exports immutable frame
handles through a bounded ring; recording, decoding, C++/CUDA reconstruction, TensorRT and Qt
consume it but never touch the transport. A Rust control plane talks to the backend over a
small C ABI.
**Rationale:** mirrors the vendor's (sane) split between `CommunicateLayer` and the device
plugin, but removes the synchronous, copy-heavy behaviour.

## ADR-007 — All performance claims must be labelled simulated or measured

**Decision:** benchmark reports carry a `mode: simulated|hardware` field; the repository never
presents simulator numbers as device measurements.
**Rationale:** the scanner is unavailable; conflating the two would be a reproducibility bug.
