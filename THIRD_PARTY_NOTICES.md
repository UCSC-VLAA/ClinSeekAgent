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

Before publishing the repo and paper, walk through this list and remove items as they are completed.

**Authorship and citation**

- [ ] Fill concrete authors, repository URL, model URL, and dataset URL in `CITATION.cff` and any model/dataset cards.
- [ ] Re-run secret and private-path scans before publishing.

**Documentation that is currently gitignored**

The following docs are in `.gitignore` (internal-only drafts), but `README.md` and `RESOURCES.md` cross-link them. A public clone will 404 on those links until public-safe versions are committed (or the links are removed):

- [ ] `docs/data_access.md`
- [ ] `docs/data_release.md`
- [ ] `docs/model_card.md`
- [ ] `docs/public_release_plan.md`

**Hugging Face artifacts**

- [ ] Verify `https://huggingface.co/datasets/UCSC-VLAA/ClinSeek-Bench` is live and accessible to reviewers (or document the access process if it stays controlled).
- [ ] Verify `https://huggingface.co/datasets/UCSC-VLAA/ClinSeek-Evaluation-Results` is live.
- [ ] Publish open-source **training trajectory** dataset (e.g. `UCSC-VLAA/ClinSeek-Trajectories`) so reviewers can reproduce SFT; link it from `RESOURCES.md` and from `docs/sft_training.md`.
- [ ] Publish `ClinSeek-35B-A3B` model weights on Hugging Face; link from README + `RESOURCES.md`.
- [ ] Update README badge state from "pending" to "live" for all of the above.

**Reproducibility**

- [ ] Make the Quick Start end-to-end runnable: either ship a tiny synthetic patient SQLite under `examples/` so `bash scripts/run_text_eval.sh DATA_PATH=examples/synthetic_text_sample.jsonl ...` actually executes, or document the data prerequisite up-front in the README.

**Paper coordination**

- [ ] After the paper is on arXiv, update README and `CITATION.cff` with the arXiv URL + BibTeX entry.
