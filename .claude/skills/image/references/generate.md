# AI Image Generator

Consumed by: `.claude/skills/image/SKILL.md`, mode `generate` (the alias `/flux-image` runs the same mode).

Last Updated: 2026-10-30


Generate images using Nano Banana 2 (Google DeepMind) or FLUX.2 max (Black Forest Labs) via the Replicate API. Runs a Python script that calls the API and saves images locally.

## Prerequisites

The `REPLICATE_API_TOKEN` must be set in the workspace `.env` file (root directory). The script reads it from the environment, else from that `.env` (`read_env_value` in `scripts/utils/paths.py`), so no manual export is needed. Its dependencies come from `uv sync --all-extras`.

If the token is missing or invalid, instruct the user to add it to `.env`:

```
# In the workspace root .env file:
REPLICATE_API_TOKEN=r8_your_token_here
```

Get a token at: https://replicate.com/account/api-tokens

**Security:** Never hardcode API tokens in skill files, scripts, or documentation. All secrets live in `.env` only.

## Workflow

### 1. Determine the prompt

Either:
- Use the image prompt from a preceding `/image prompt` run
- Use a prompt the user provides directly
- Craft a prompt from context (article, post, etc.) using the image prompt guidelines

### 2. Run the generation script

Run from the workspace root (the script path is root-relative). The image is a DATA artifact. It
must land in the DATA overlay, never the engine tree. `generate_image.py` writes `--output` literally
relative to the current directory, which is the engine root after the `cd`. A bare data-relative path
would create a stray image inside the engine. An omitted `--output` resolves the generated-images
directory by NAME and refuses when it cannot. Still resolve the dir here and pass an absolute path, so
the filename is descriptive. Resolve the name, never a path.

```bash
cd "$(git rev-parse --show-toplevel)"
IMAGES_DIR="$(.venv/bin/python scripts/data-path.py path generated-images)"
.venv/bin/python ".claude/skills/flux-image/scripts/generate_image.py" \
  --prompt "your detailed image prompt here" \
  --output "$IMAGES_DIR/my-image.png" \
  --aspect-ratio 16:9 \
  --model banana \
  --format png
```

### 3. Report the result

Tell the user the file path of the generated image. Use the Read tool to display the image to the user (Claude Code can render images).

## Script Parameters

| Parameter | Default | Options | Description |
|-----------|---------|---------|-------------|
| `--prompt` | Required | Any text | The image generation prompt |
| `--output` | `generated_image.png` in the DATA generated-images directory | Any path | Where to save the image. The default resolves the logical name, so an omitted flag never writes into the public engine clone and never into the vacated `outputs` tree. Pass an absolute path anyway, per step 2. |
| `--aspect-ratio` | `16:9` | `1:1`, `16:9`, `21:9`, `2:3`, `3:2`, `4:5`, `5:4`, `9:16`, `9:21` | Image dimensions |
| `--model` | `banana` | `banana`, `flux-max` | Model: Nano Banana 2 (default) or FLUX.2 max |
| `--num-outputs` | `1` | `1`-`4` | Number of variations |
| `--format` | `png` | `png`, `jpg`, `webp` | Output format |
| `--seed` | Random | Any integer | For reproducible results |

## Model Selection

Choose based on need. See [models.md](models.md) for full details.

| Model | Speed | Quality | Cost | Best For |
|-------|-------|---------|------|----------|
| **banana** (default) | ~10-15s | Highest | ~$0.05 | Best text rendering, infographics, branded visuals, general-purpose |
| **flux-max** | ~15-20s | Highest | ~$0.05 | Top-tier photorealism, complex scenes, multi-reference editing |

Default to **banana** (Google Nano Banana 2). Use **flux-max** (FLUX.2 max) for maximum photorealism or complex scene editing.

## Sensitive sessions (SENSITIVE_MODE)

Before sending any prompt to the Replicate API, check session sensitivity:

```bash
.venv/bin/python -c "from scripts.utils.sensitive import is_sensitive, sanitize_prompt_guidance; print(sanitize_prompt_guidance()) if is_sensitive() else None"
```

It prints guidance when sensitivity is not explicitly cleared, which is the
fail-closed default. In that case strip ALL project-identifying detail from the
prompt before the call: no codenames, company names, people names, deal terms,
or strategic specifics.
Use abstract, generic descriptions ("corporate acquisition integration diagram",
not "Phoenix acquisition of CompanyX"). The image file stays local; only the
prompt goes to Replicate, so the prompt must carry nothing identifying. This
replaces the removed `secure-flux` vault wrapper.

## Output Location

Save generated images under the DATA overlay's generated-images directory, resolved by name via
`$IMAGES_DIR` (see step 2): `"$IMAGES_DIR/<name>.png"`. ALWAYS use this path. Never the overlay's
`outputs` tree, which the HEADING OS 2 move emptied. Never a bare cwd-relative data path, which
resolves into the engine tree. Use descriptive filenames:
- `$IMAGES_DIR/linkedin-hiring-post-image.png`
- `$IMAGES_DIR/mwc-teaser-visual.png`
- `$IMAGES_DIR/tribe-monday-banner.png`

## Combining with /image prompt

The typical two-step workflow:
1. `/image prompt` - analyzes content and produces a text prompt
2. `/image generate` - takes that prompt and generates the actual image

When used after `/image prompt`, extract the Image Prompt text (not the parameters or platform suffix) and pass it to the script.
