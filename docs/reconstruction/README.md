# Reconstruction documentation

| document | contents |
|---|---|
| [`vendor_pipeline_analysis.md`](vendor_pipeline_analysis.md) | Evidence-backed survey of the shipped EXStar/Einstar reconstruction modules: stage table, numeric parameters, CUDA/PXT evidence, config files, confidence and unknowns. |
| [`modern_pipeline_design.md`](modern_pipeline_design.md) | Proposed C++23/CUDA reconstruction core: frame ABI, stereo backends, confidence, tracking/factor graph, TSDF/surfel fusion, mesh/texture, GPU budgets, Rust/Qt integration, milestones, risks. |
| [`validation_plan.md`](validation_plan.md) | Offline synthetic/dataset experiments with metrics and acceptance criteria, plus the hardware benchmark plan. |
| [`validation_plan_m3.md`](validation_plan_m3.md) | **Implemented M3 acceptance plan** for the new `recon/` engine: three validation levels, concrete gates, failure-mode tests, and the artefact protocol. |
| [`architecture.md`](architecture.md) | **Implemented** acquisition-to-reconstruction architecture (C++23/OpenCV 5), data model, frames, provenance, live/replay parity. |
| [`audit.md`](audit.md) | Integration audit: existing components, status vocabulary, wrong assumptions found, blockers. |
| [`stereo_matcher.md`](stereo_matcher.md) | IR matcher: padded SGBM, LR consistency, confidence, measured coverage and test evidence. |
| [`synthetic_validation.md`](synthetic_validation.md) | Synthetic ground-truth results (250/350/600 mm) and the automated test matrix. |
| [`real_data_report.md`](real_data_report.md) | Real genuine-frame reconstruction: metrics, ablations, consistency (filled by the real-data evaluation). |
| [`benchmarks.md`](benchmarks.md) | CPU stage timings, memory, determinism and repeatability. |
| [`acquisition.md`](acquisition.md) | Live acquisition runbook (pending hardware). |

> **Implemented vs proposed.** `modern_pipeline_design.md` is the broader target design
> (Rust control plane, CUDA backends, tracking/fusion/Qt). `architecture.md` and the
> `recon/` engine are the **implemented first stage**: genuine-frame stereo → depth →
> point cloud, with a stable interface that the target design can build on.
> `validation_plan.md` is the original experiment plan; `validation_plan_m3.md` is the
> acceptance plan actually used for this milestone.

Evidence dumps (`analysis/gnu/reconstruction_strings/`) and the survey JSON
(`analysis/gnu/reconstruction_survey.json`) live in the private research workspace and are not
published; see [analysis/README.md](../../analysis/README.md) for the exclusion policy.
Survey tool: [`tools/analysis/survey_modules.py`](../../tools/analysis/survey_modules.py)

All vendor-binary analysis was read-only; no vendor binary was executed.
