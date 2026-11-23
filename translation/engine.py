"""Bounded CPU inference, with executable and path literals kept out of models."""
import json
import os
import re
import threading
from pathlib import Path
from typing import Callable

MAX_TEXT = 64000
MAX_PROSE_CHUNK = 350
MAX_TOKENS = 240
MAX_OUTPUT = 256000
PAIRS = {("zh", "en"), ("en", "zh")}

# These spans are copied byte-for-byte (as Unicode strings), never translated.
# Broad path matching is intentional: a false positive leaves a token unchanged
# rather than corrupting an executable command, filename or URL.
PROTECTED = re.compile(
    r"(?P<fence>^[ \t]{0,3}(?P<mark>`{3,}|~{3,})[^\r\n]*\r?\n.*?(?:^[ \t]{0,3}(?P=mark)[ \t]*(?:\r?\n|$)|\Z))"
    r"|(?P<inline>(?P<ticks>`+)(?:(?!(?P=ticks)|[\r\n]).)+(?P=ticks))"
    r"|(?P<url>https?://[^\s<>\"'`\[\]{}，。；！？、（）]+)"
    r"|(?P<winpath>[A-Za-z]:[\\/][^\s<>\"'`|，。；！？、]+)"
    r"|(?P<path>(?<![A-Za-z0-9])(?:~?/|\.{1,2}/)[^\s<>\"'`|，。；！？、（）\[\]{}]+)"
    r"|(?P<relative>(?<![A-Za-z0-9_])(?:[A-Za-z0-9_.-]+|[\u3400-\u9fff]+)(?:/[^\s<>\"'`|，。；！？、（）\[\]{}]+)+)"
    r"|(?P<filename>(?<![A-Za-z0-9_])[A-Za-z0-9_-]+\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|md|txt|html|css|sh|sql|go|rs|java|c|cpp|h|env|lock|png|jpg|svg|pdf|csv|xml)(?![A-Za-z0-9_]))"
    r"|(?P<flag>(?<!\w)--?[A-Za-z][A-Za-z0-9_-]*(?:=[^\s，。；！？]+)?)"
    r"|(?P<identifier>\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b)"
    r"|(?P<markdown>^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]*))",
    re.MULTILINE | re.DOTALL,
)
HAN = re.compile(r"[\u3400-\u9fff\U00020000-\U0002ffff]")
LATIN = re.compile(r"[A-Za-z]")
SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])|(?<=[.])[ \t]+")


def segments(text: str):
    """Yield (text, protected); exact concatenation always reproduces the input."""
    offset = 0
    for match in PROTECTED.finditer(text):
        if match.start() > offset:
            yield text[offset:match.start()], False
        value = match.group()
        # Closing prose punctuation is not part of a URL.
        if match.lastgroup == "url":
            trimmed = value.rstrip(".,;:!?，。；！？、）)]}")
            if trimmed:
                yield trimmed, True
            if len(trimmed) < len(value):
                yield value[len(trimmed):], False
        else:
            yield value, True
        offset = match.end()
    if offset < len(text):
        yield text[offset:], False


def prose_chunks(text: str):
    """Keep newlines/outer whitespace and bound each model sentence input."""
    for line in re.split(r"(\r\n|\r|\n)", text):
        if not line or line.isspace():
            yield line, False
            continue
        # Preserve leading and trailing whitespace exactly.
        left = len(line) - len(line.lstrip())
        right = len(line.rstrip())
        if left:
            yield line[:left], False
        content = line[left:right]
        start = 0
        for split in SENTENCE_BREAK.finditer(content):
            end = split.start() if split.end() > split.start() else split.end()
            if end > start:
                yield from bounded(content[start:end])
            if split.end() > split.start():
                yield content[split.start():split.end()], False
            start = split.end()
        if start < len(content):
            yield from bounded(content[start:])
        if right < len(line):
            yield line[right:], False


def bounded(text: str):
    while len(text) > MAX_PROSE_CHUNK:
        split = text.rfind(" ", MAX_PROSE_CHUNK // 2, MAX_PROSE_CHUNK + 1)
        if split == -1:
            split = MAX_PROSE_CHUNK
        yield text[:split], True
        text = text[split:]
        leading = len(text) - len(text.lstrip(" \t"))
        if leading:
            yield text[:leading], False
            text = text[leading:]
    if text:
        yield text, True


def translate_preserving(text: str, source: str, target: str, translate: Callable[[str], str]) -> str:
    if (source, target) not in PAIRS:
        raise ValueError("Only zh↔en is supported")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError("Translation text exceeds 64000 characters")
    result = []
    size = 0
    previous_protected = previous_translated = False
    for value, protected in segments(text):
        pieces = [(value, False)] if protected else prose_chunks(value)
        for piece, translatable in pieces:
            needed = HAN.search(piece) if source == "zh" else LATIN.search(piece)
            if translatable and needed:
                output = translate(piece)
                if not isinstance(output, str) or not output.strip():
                    raise RuntimeError("Local translation model returned an empty result")
            else:
                output = piece
            if (target == "en" and result and output and
                    ((protected and previous_translated) or (previous_protected and translatable and needed)) and
                    not result[-1][-1:].isspace() and not output[0].isspace() and
                    output[0] not in ".,;:!?，。；！？、)]}" and result[-1][-1:] not in "([{"):
                # Chinese prose need not contain spaces around a literal. Add
                # readable English boundaries without changing that literal.
                result.append(" ")
                size += 1
            size += len(output)
            if size > MAX_OUTPUT:
                raise ValueError("Translation output exceeds limit")
            result.append(output)
            previous_protected = protected
            previous_translated = bool(translatable and needed)
    return "".join(result)


class LocalTranslator:
    def __init__(self, model_dir: str | Path | None = None, threads: int | None = None):
        self.model_dir = Path(model_dir or os.getenv("TRANSLATION_MODELS_DIR", "/models"))
        self.threads = max(1, min(threads or int(os.getenv("TRANSLATION_THREADS", "2")), 8))
        self.models = {}
        self.lock = threading.Lock()
        self.ready = False

    def warm(self):
        import ctranslate2
        import sentencepiece
        with self.lock:
            for source, target in sorted(PAIRS):
                directory = self.model_dir / f"{source}_{target}"
                metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
                if (metadata.get("from_code"), metadata.get("to_code")) != (source, target):
                    raise RuntimeError("Local translation model direction mismatch")
                tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(directory / "sentencepiece.model"))
                model = ctranslate2.Translator(str(directory / "model"), device="cpu", compute_type="int8", inter_threads=1, intra_threads=self.threads)
                self.models[(source, target)] = (tokenizer, model)
                sample = "你好。" if source == "zh" else "Hello."
                if not self._translate(sample, source, target).strip():
                    raise RuntimeError("Local translation model warmup failed")
            self.ready = True

    def _translate(self, text: str, source: str, target: str) -> str:
        tokenizer, model = self.models[(source, target)]
        tokens = tokenizer.encode(text, out_type=str)
        if not tokens:
            return text
        # No implicit truncation: split large inputs before the runtime limit.
        batches = [tokens[start:start + MAX_TOKENS] for start in range(0, len(tokens), MAX_TOKENS)]
        outputs = model.translate_batch(
            batches, beam_size=4, num_hypotheses=1, length_penalty=0.2,
            replace_unknowns=True, max_batch_size=8, max_input_length=0,
            max_decoding_length=512,
        )
        chunks = []
        for item in outputs:
            hypothesis = item.hypotheses[0]
            if len(hypothesis) >= 512:
                raise RuntimeError("Local translation reached its output limit")
            # Match Argos SentencePiece decoding, including Asian byte fallback.
            chunks.append(tokenizer.decode_pieces(hypothesis).replace("▁", " ").replace("_", " ").strip())
        return (" " if target == "en" else "").join(chunks)

    def translate(self, text: str, source: str, target: str) -> str:
        if not self.ready:
            raise RuntimeError("Local translation models are not ready")
        with self.lock:
            return translate_preserving(text, source, target, lambda chunk: self._translate(chunk, source, target))
