---
name: image
description: "Images for content, in two modes. prompt: write a photorealistic text-to-image prompt for Midjourney, DALL-E or Stable Diffusion from content just produced (a post, article, email or slide copy), text only (\"image prompt\", \"visualize this\", an image or illustration to go with a piece). generate: produce an actual image file on disk through Replicate, Nano Banana 2 by default or FLUX.2 max (\"generate an image\", \"create an image\", \"make a picture\", \"flux\", turn a prompt into a real picture). Do NOT use for full design work such as logos, infographics, mockups or brochures (use /design), or to draft the post or article itself."
argument-hint: "[prompt|generate] [content to visualize, or the image prompt]"
allowed-tools: "Read, Bash(.venv/bin/python:*), Bash(cd:*), Bash(git:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Turns content into a platform-ready text-to-image prompt, or turns a prompt
    into a real image file saved in the data overlay's generated-images
    directory through the Replicate API.
  how: >
    Run /image prompt after producing content to get the prompt as text, or
    /image generate <prompt> to render it with Nano Banana 2 (default) or
    FLUX.2 max. The two chain: prompt first, then generate.
  when: >
    Use for a visual companion to a post, article or slide. For logos,
    infographics, mockups and other design work use /design.
x-heading-routing:
  category: Content
  label: /image [prompt|generate]
  triggers:
    - image prompt
    - visualize this
    - generate image prompt
    - generate image
    - create image
    - make a picture
    - flux
  exclusions:
    - N/A
  router: auto
---

# Image

One skill, two modes. Each mode's whole procedure is in its reference file.

## Mode selector

1. If the first argument is `prompt` or `generate`, use that mode. Pass the remaining arguments to it.
2. Otherwise infer the mode from the request. A text prompt, or a visual idea for written content, is `prompt`. An actual picture or image file is `generate`.
3. If both modes still fit, ask one question that names them. Then wait.

## Modes

- `prompt`: read `references/prompt.md` and follow it.
- `generate`: read `references/generate.md` and follow it.

The aliases `/image-prompt` and `/flux-image` run the same two modes.
