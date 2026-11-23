# Offline translation models

This service uses the Chinese→English and English→Chinese version 1.9 model
packages published in the [official Argos package index](https://github.com/argosopentech/argospm-index).
The archive URLs and SHA-256 digests are pinned in `models.json`. Models are
downloaded only when the image is built; the service does not install or fetch
models while translating.

Model attribution, as supplied in both model archives:

- Authors: Jörg Tiedemann and Santhosh Thottingal.
- Title: “OPUS-MT — Building open translation services for the World”.
- Proceedings of the 22nd Annual Conference of the European Association for
  Machine Translation (EAMT), 2020, Lisbon, Portugal.
- Original OPUS model license: [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).

Each model's original README and metadata are retained in the image. We use the
packaged CTranslate2 weights and SentencePiece vocabulary without modifying
them. Inference uses CPU int8 computation. Sentence chunking and code/path
preservation are implemented by this project, so output may differ from the
full Argos Translate application.

Runtime libraries:

- [CTranslate2](https://github.com/OpenNMT/CTranslate2), MIT.
- [SentencePiece](https://github.com/google/sentencepiece), Apache 2.0.

This is machine translation. Ambiguous prose, unfamiliar technical terms and
sentences split around protected literals may lose nuance. Fenced code, inline
code and recognized paths/URLs are copied as-is; users can compare the original
and translated messages in the dashboard. There is no LLM or online fallback.

Put commands, code, paths containing spaces, and exact text that must not change
inside inline backticks or a fenced code block. Plain prose is not a programming
language parser: no automatic recognizer can reliably identify every unmarked
command, quoted string, or path adjacent to natural-language text.
