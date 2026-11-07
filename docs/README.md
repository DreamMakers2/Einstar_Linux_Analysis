# Reports

| File | Content |
|---|---|
| [FINDINGS.md](FINDINGS.md) | numbered findings with confidence and evidence; corrections to earlier assumptions |
| [firmware/format.md](firmware/format.md) | `.img` container, FX3 sections, EN/CH arithmetic, flash layout |
| [firmware/fpga_bitstream.md](firmware/fpga_bitstream.md) | Pango PGL25G identification, packet format, size, compression analysis |
| [firmware/fx3_analysis.md](firmware/fx3_analysis.md) | FX3 memory map, ThreadX threads, USB/DMA, command dispatch, update writer |
| [firmware/official_versions.md](firmware/official_versions.md) | official version archaeology and the "no V2.14" result |
| [firmware/firmware_evolution.md](firmware/firmware_evolution.md) | what V2.10 added over V1.0 (string-set evidence) |
| [firmware/compare_V1.0_to_V2.10.md](firmware/compare_V1.0_to_V2.10.md) | generated comparison report |
| [protocol/einstar_usb_protocol.md](protocol/einstar_usb_protocol.md) | consolidated USB/command/stream/update specification |
| [protocol/libeinstar_protocol_spec.md](protocol/libeinstar_protocol_spec.md) | byte-level spec recovered from the Linux driver, with code references |
| [protocol/libeinstar_gap_analysis.md](protocol/libeinstar_gap_analysis.md) | 21 ranked gaps in the existing Linux driver |
| [protocol/libeinstar_code_map.md](protocol/libeinstar_code_map.md) | file/function map and build record |
| [calibration/ccf_format.md](calibration/ccf_format.md) | CCF layout, the IR key proof, geometry, device-blob map |
| [calibration/model.md](calibration/model.md) | three-camera model, proved vs bounded key, rectification/triangulation validation, hardware checklist |
| [benchmarks/offline_benchmarks.md](benchmarks/offline_benchmarks.md) | separated USB 2.0 / USB 3.0 / trigger / host scenarios and the 58-vs-40 MB/s cadence finding |
| [readiness.md](readiness.md) | implemented / offline-validated / hardware-validated matrix and replacement gates |
| [windows/architecture.md](windows/architecture.md) | runtime architecture and IPC chain |
| [windows/module_map.md](windows/module_map.md) | 741-module inventory, dependency graph, API surface |
| [reconstruction/vendor_pipeline_analysis.md](reconstruction/vendor_pipeline_analysis.md) | evidence-backed vendor pipeline survey |
| [reconstruction/modern_pipeline_design.md](reconstruction/modern_pipeline_design.md) | modern design, budgets, milestones |
| [reconstruction/validation_plan.md](reconstruction/validation_plan.md) | offline experiments and acceptance criteria |
| [roadmap.md](roadmap.md) | phased plan with exit gates |
| [blockers.md](blockers.md) | unresolved problems and discriminating tests |
| [decisions.md](decisions.md) | technical decision records |
| [evidence-index.md](evidence-index.md) | evidence classes and key function-level citations |
| [progress-log.md](progress-log.md) | chronological work log |

Paths under `sources/` and `analysis/logs/` in these reports refer to the private research
workspace; those inputs are not published (see [../analysis/README.md](../analysis/README.md)).
