# Reconstruction documentation

| document | contents |
|---|---|
| [`vendor_pipeline_analysis.md`](vendor_pipeline_analysis.md) | Evidence-backed survey of the shipped EXStar/Einstar reconstruction modules: stage table, numeric parameters, CUDA/PXT evidence, config files, confidence and unknowns. |
| [`modern_pipeline_design.md`](modern_pipeline_design.md) | Proposed C++23/CUDA reconstruction core: frame ABI, stereo backends, confidence, tracking/factor graph, TSDF/surfel fusion, mesh/texture, GPU budgets, Rust/Qt integration, milestones, risks. |
| [`validation_plan.md`](validation_plan.md) | Offline synthetic/dataset experiments with metrics and acceptance criteria, plus the hardware benchmark plan. |

Evidence dumps: [`analysis/gnu/reconstruction_strings/`](../../analysis/gnu/reconstruction_strings/README.md)
Survey tool: [`tools/analysis/survey_modules.py`](../../tools/analysis/survey_modules.py)
Survey JSON: [`analysis/gnu/reconstruction_survey.json`](../../analysis/gnu/reconstruction_survey.json)

All vendor-binary analysis was read-only; no vendor binary was executed.
