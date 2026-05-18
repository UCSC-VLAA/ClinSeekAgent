# Third-Party Notices

This release includes code that interfaces with or depends on external assets. Confirm final license compatibility before public publication.

## Datasets

- MIMIC-IV and MIMIC-CXR are credentialed PhysioNet datasets. Do not redistribute raw records, images, generated patient databases, or patient-level derived artifacts through this GitHub repository.
- EHRXQA and MedMod are used as source benchmarks for multimodal tasks. Follow their original licenses and access terms.
- ClinSeek-Bench manifests and trajectory data should be released separately with a dataset card that states provenance, access restrictions, and non-clinical intended use.

## Code And Tooling

- VERL is vendored in this repository so the SFT training code can be released with the paper. Preserve upstream VERL license and notice files when publishing.
- The browser tool path uses OpenAI Harmony / gpt-oss-style browser message utilities.
- Image tools depend on third-party medical-imaging models and libraries, including torchxrayvision and Hugging Face-hosted models. Some model weights may be gated.
- AWS Bedrock and vLLM integrations require the user's own credentials or local serving infrastructure.

## Required Final Checks

- Replace `LICENSE` with the approved project license.
- Fill concrete authors, repository URL, model URL, and dataset URL in `CITATION.cff`, `docs/model_card.md`, and `docs/data_release.md`.
- Re-run secret and private-path scans before publishing.
