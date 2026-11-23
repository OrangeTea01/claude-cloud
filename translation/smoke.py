"""Build/runtime check: models must translate with networking disabled."""
from .engine import HAN, LocalTranslator


def main():
    engine = LocalTranslator()
    engine.warm()
    incoming = "请创建一个登录页面，并保留 `src/main.py`。"
    outgoing = "The login page is ready. Keep `src/main.py` unchanged."
    english = engine.translate(incoming, "zh", "en")
    chinese = engine.translate(outgoing, "en", "zh")
    assert english != incoming and not HAN.search(english), "Chinese-to-English smoke failed"
    assert chinese != outgoing and HAN.search(chinese), "English-to-Chinese smoke failed"
    assert "`src/main.py`" in english and "`src/main.py`" in chinese
    print("Offline zh→en and en→zh models ready; literal preservation passed")


if __name__ == "__main__":
    main()
