# Brand Setup Guide

Consumed by: `.claude/skills/pptx-generator/SKILL.md`, section "PREREQUISITE: Brand Check", when no brand exists yet and one must be created first.

How to create a new brand configuration for the PPTX generator.

A brand is created in the private data repository, never in the engine: brand content is not code. Every path below is under `$BRANDS`, resolved once:

```bash
BRANDS="$(.venv/bin/python scripts/data-path.py path skill-brand-assets skill=pptx-generator)/brands"
```

If that command fails, this clone has no data repository to hold a brand; say so and stop.

## Step 1: Read the Template

```
Read: $BRANDS/template/README.md
Read: $BRANDS/template/brand.json
Read: $BRANDS/template/config.json
```

## Step 2: Gather Brand Information

Ask the user for (or extract from provided materials):

| Required | Description |
|----------|-------------|
| **Brand name** | Folder name (lowercase, no spaces) |
| **Colors** | Background, text, accent colors (hex codes) |
| **Fonts** | Heading font, body font, code font |

| Optional | Description |
|----------|-------------|
| Output directory | Where to save generated files (default: `{brand}` under the registry name `deck-outputs`, resolved under the DATA overlay via `$DECK_DIR`) |
| Logo | Path to logo file (PNG/SVG) |
| Brand guidelines | Existing style guide or website to reference |
| Tone of voice | Writing style, vocabulary preferences |

## Step 3: Create Brand Files

1. **Create the brand folder:**
   ```bash
   mkdir -p "$BRANDS/{brand-name}"
   ```

2. **Create brand.json** with the gathered values:
   ```json
   {
     "name": "Brand Name",
     "description": "One-line description",
     "colors": {
       "background": "hex-without-hash",
       "background_alt": "hex-without-hash",
       "text": "hex-without-hash",
       "text_secondary": "hex-without-hash",
       "accent": "hex-without-hash",
       "accent_secondary": "hex-without-hash",
       "accent_tertiary": "hex-without-hash",
       "code_bg": "hex-without-hash",
       "card_bg": "hex-without-hash",
       "card_bg_alt": "hex-without-hash"
     },
     "fonts": {
       "heading": "Font Name",
       "body": "Font Name",
       "code": "Monospace Font"
     },
     "assets": {
       "logo": "assets/logo.png",
       "logo_dark": null,
       "icon": null
     }
   }
   ```

3. **Create config.json** with output settings. The default `directory` is a marker resolved at
   generation time under the DATA overlay (`.venv/bin/python scripts/data-path.py path deck-outputs`/{brand} via `$DECK_DIR`);
   set an absolute path only to override. A bare relative `output/...` would write into the engine.
   ```json
   {
     "output": {
       "directory": "$DECK_DIR",
       "naming": "{name}-{date}",
       "keep_parts": false
     },
     "generation": {
       "slides_per_batch": 5,
       "auto_combine": true,
       "open_after_generate": false
     },
     "defaults": {
       "slide_width_inches": 13.333,
       "slide_height_inches": 7.5
     }
   }
   ```

4. **Create brand-system.md** - Copy from template and fill in brand guidelines

5. **Create tone-of-voice.md** - Copy from template and fill in voice guidelines

6. **Add assets** - Copy logo/images to `$BRANDS/{brand-name}/assets/`

## Step 4: Verify

After creating the brand, verify with:
```
Glob: $BRANDS/{brand-name}/*
```

Then proceed to slide generation.
