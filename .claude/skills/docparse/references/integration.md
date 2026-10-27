# DocParse Integration Guide

Consumed by: `.claude/skills/docparse/SKILL.md`
Last Updated: 2026-04-09

## CLI Interface

### Parse Documents

Scratch JSON goes in the Workspace scratch directory, never in `/tmp`.

```bash
SCRATCH="$(.venv/bin/python scripts/data-path.py path --create runtime:scratch/tmp)"
.venv/bin/python scripts/docparse.py parse \
  --files "path/to/doc.pdf" "other.pptx" \
  [--pages "1-5,10"] \
  [--dpi 150] \
  [--password "secret"] \
  [--no-cache] \
  --output-json "$SCRATCH/parsed.json"
```

Directories are auto-discovered for supported file types.

### Generate Visual Citation Report

`--output-dir` names a path under the DATA overlay. Bash is not covered by the
data-path-redirect hook, so resolve the report directory BY NAME
(`docparse-reports`) first and never hand `docparse.py` a bare data-relative path.

```bash
DOCPARSE_DIR="$(.venv/bin/python scripts/data-path.py path docparse-reports)"
.venv/bin/python scripts/docparse.py report \
  --parse-json "$SCRATCH/parsed.json" \
  --citations "$SCRATCH/citations.json" \
  [--output-dir "$DOCPARSE_DIR/YYYY-MM-DD/"] \
  [--title "Analysis Report"] \
  [--no-pdf]
```

## Python Import Interface

```python
from scripts.docparse import parse_document, find_boxes_for_quote

# Parse a document (cache-aware, returns dict)
doc = parse_document("path/to/doc.pdf", pages="1-5", dpi=150)

# Find bounding boxes for a verbatim quote (returns pixel coordinates)
boxes = find_boxes_for_quote(doc["pages"][0]["text_items"], "exact quote", dpi=150)
```

## Parse Output JSON Schema

```json
{
  "files": [{
    "file": "/absolute/path/report.pdf",
    "file_name": "report.pdf",
    "parsed_at": "2026-04-09T10:30:00+00:00",
    "dpi": 150,
    "pages": [{
      "page_num": 1,
      "width_pt": 595.0,
      "height_pt": 842.0,
      "text": "full page text (use this for reading)",
      "text_items": [
        { "text": "Revenue", "x": 72.0, "y": 144.5, "width": 80.3, "height": 14.0 }
      ]
    }]
  }],
  "summary": { "total_files": 1, "total_pages": 3, "cache_hits": 0, "elapsed_seconds": 2.1 }
}
```

- `text` - concatenated page text for reading/analysis
- `text_items` - individual text elements with bounding boxes (PDF points). For bounding box matching only - do not present to users.
- Coordinates are in PDF points (72 points/inch). Convert to pixels: `px = pt * (dpi / 72)`

## Citations Input JSON Schema

```json
{
  "question": "[the question asked]",
  "answer_md": "[answer with **key figure**] [1], [supporting claim] [2].",
  "citations": [
    { "id": 1, "file": "report.pdf", "page": 3, "quote": "[verbatim source text]", "relevance": "[why this passage answers]" }
  ]
}
```

- `quote` must be character-for-character exact from the parsed text
- `id` matches `[N]` markers in `answer_md`

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Prerequisites missing (run `setup --install`) |
| 2 | Parse or report generation error (including all files not found) |

## Cache Behavior

- Location: the runtime address `runtime:docparse/cache` (`data_write_dir("runtime:docparse/cache")`; refuses on a clone with no data overlay)
- Key: SHA256 of resolved file path + size + mtime
- TTL: 7 days
- Invalidation: automatic on file change (mtime or size differs)
- Skip: `--no-cache` flag

## Integration Examples

To parse a document from the datastore (a prospect's RFP, a source document for
fact-checking), find it across the homes the `datastore-homes` group names and
pass the path to `parse`:

```bash
DOC="$(.venv/bin/python scripts/data-path.py group datastore-homes | while read -r h; do find "$h" -name '<file-name>.pdf' 2>/dev/null; done | head -1)"
SCRATCH="$(.venv/bin/python scripts/data-path.py path --create runtime:scratch/tmp)"
.venv/bin/python scripts/docparse.py parse --files "$DOC" \
  --output-json "$(mktemp -d -p "$SCRATCH")/parsed.json"
# Inbound documents alone sit under the registry name `datastore-documents`.
```
