"""Build-time only: download the pinned, public Argos model archives."""
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MANIFEST = Path(__file__).with_name("models.json")
FILES = (
    "metadata.json", "README.md", "sentencepiece.model",
    "model/config.json", "model/model.bin", "model/shared_vocabulary.json",
)
MAX_ARCHIVE = 256 * 1024 * 1024


def install_archive(archive_path: Path, output: Path, spec: dict):
    digest = hashlib.sha256()
    with archive_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != spec["sha256"]:
        raise ValueError("Translation model checksum mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        prefix = spec["archive_root"] + "/"
        metadata = json.loads(archive.read(prefix + "metadata.json"))
        if (metadata.get("from_code"), metadata.get("to_code"), metadata.get("package_version")) != (spec["source"], spec["target"], spec["version"]):
            raise ValueError("Translation model metadata mismatch")
        # Copy only fixed member names. No archive-controlled destination paths,
        # extraction helpers, executable code, or sentence-model downloads.
        for name in FILES:
            item = archive.getinfo(prefix + name)
            if item.file_size > MAX_ARCHIVE:
                raise ValueError("Translation model member exceeds size limit")
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)


def main():
    output = Path(os.getenv("TRANSLATION_MODELS_DIR", "/models"))
    specs = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="claude-cloud-models-") as temporary:
        for direction, spec in specs.items():
            archive_path = Path(temporary) / (direction + ".argosmodel")
            request = urllib.request.Request(spec["url"], headers={"User-Agent": "ArgosTranslate"})
            with urllib.request.urlopen(request, timeout=90) as response, archive_path.open("wb") as file:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_ARCHIVE:
                        raise ValueError("Translation model archive exceeds size limit")
                    file.write(chunk)
            install_archive(archive_path, output / direction, spec)
            print(f"Installed verified {direction} model {spec['version']}")


if __name__ == "__main__":
    main()
