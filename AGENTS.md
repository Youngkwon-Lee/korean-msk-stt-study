# Research repository rules

- Keep public protocol drafts, blank forms, synthetic tests and reviewed aggregates only.
- Never commit original recordings, transcript/reference pairs, individual participant results, signed consent forms, patient/staff identifiers, private Drive/operations links, model weights or credentials.
- Do not turn this repository into a production service or start participant collection from a documentation task.
- Distinguish historical reconstructed aggregates from newly executed experiments. Never claim GPU/model validation from mock tests.
- Use existing model-specific environments. Do not install, download models, change drivers, start CI/schedulers or run GPU jobs unless explicitly included in the accepted task.
- Before publication, stage explicit paths, run scoped tests and scripts/check_publication.py, then inspect the actual staged content. The scanner is not proof that PHI is absent.
- Consent documents are unapproved drafts until institution-specific details and review are completed.
