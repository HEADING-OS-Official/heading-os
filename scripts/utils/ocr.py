"""The Tesseract language every LiteParse OCR call in this workspace uses.

One constant, because two scripts OCR through LiteParse
(`scripts/datastore-extract.py` and `scripts/docparse.py`), and a language that
differs between them gives one document two different texts.

`rus+eng`, not LiteParse's default `eng`. MEASURED 2026-09-26 (HOS-148) on a
Russian book scan with no text layer, pages 5 and 20, offline:

- `eng` wrote Russian as Latin look-alikes ("Ha pycckom BbIITyCKaeTCA") and
  raised nothing, so a companion would have carried it as the document's text.
- `rus` read the Russian well and broke the English inside it ("Арре" for
  Apple, "15ВМ" for ISBN): 6 errors in the first 14 lines of page 5.
- `rus+eng` had 3 errors in the same lines, and about 2 in the roughly 170
  words of page 20, one of them a lost drop cap.

On English scans it changed almost nothing: 178 of 179 words shared on a
signed one-page letter. On an image-only design sheet it added 31 Cyrillic
characters, all inside the noise OCR already produces from graphics, and
damaged one real word.

LiteParse downloads a language model from GitHub the first time it needs one
it has not cached; only the model travels, never the document. Offline and
without the model, OCR fails, and each caller writes that failure where the
reader sees it.
"""

OCR_LANGUAGE = "rus+eng"
