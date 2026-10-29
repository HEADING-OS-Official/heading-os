# Image Prompt Generator

Consumed by: `.claude/skills/image/SKILL.md`, mode `prompt` (the alias `/image-prompt` runs the same mode).

Last Updated: 2026-10-30


Analyze the preceding text, article, or post and generate a detailed, platform-ready image prompt for AI text-to-image tools.

## Process

1. **Identify the Central Idea** - Distill the main concept or narrative into one sentence
2. **Extract Visual Elements** - Find symbols, metaphors, settings, or scenarios that represent the content's essence
3. **Define the Mood** - Match the emotional atmosphere: inspirational, urgent, contemplative, dynamic, warm, authoritative
4. **Construct the Prompt** - Compose a 50-150 word description with subject, composition, lighting, color palette, atmosphere

## Visual Element Selection for Company Content

Match content type to visual language:

| Content Type | Visual Direction |
|-------------|-----------------|
| Maritime/leadership metaphors | Sailing, navigation, open water, helm, compass, horizon |
| Product/technical | Networks, data flows, light particles, fiber optics, server rooms |
| Platform/building narrative | Architecture, bridges, foundations, construction, engineering |
| Expansion/market | Landscapes, coastlines, cities at dawn, maps, new horizons |
| Team/culture | Warm light, human silhouettes, collaborative spaces, depth |
| Sovereignty/independence | National landmarks, flags in wind, fortified structures, vaults |
| Speed/growth | Motion blur, launch trajectories, acceleration, time-lapse |

Default brand palette, the same one `/design` applies: dark backgrounds (#000000), blue-purple (#5B5FFF) and orange (#FF8C00) accents.

## Output Format

Deliver exactly this structure:

```
**Core Theme Identified:** [One sentence - the central idea of the source content]

**Image Prompt:**
[50-150 words. Specific, descriptive, platform-ready. Include: subject matter, composition,
lighting, color palette, atmosphere. Only tangible, photographable elements.]

**Parameters:**
- Aspect Ratio: 16:9
- Style: Photorealistic, high-resolution, natural lighting, authentic textures
- No text, typography, words, letters, numbers, or written elements

**Platform Suffix (Midjourney):** --ar 16:9 --v 6.1 --style raw --s 250
```

For other platforms, see [platforms.md](platforms.md).

## Rules

- The image must feel like a natural visual companion to the source text
- Specific over vague - "golden hour light casting long shadows across cracked salt flats" beats "dramatic lighting"
- Focus on tangible subjects that photograph well in reality
- Cinematic composition suited to 16:9 widescreen format
- One clear focal point per image - avoid cluttered scenes

## Never Include

- Text, logos, typography, numbers, or written elements of any kind
- Stock photo clichés (handshakes, pointing at screens, generic team meetings)
- Military imagery (maritime only for company content)
- Dystopian or fear-based imagery (the company narrative is constructive)
- Abstract geometric patterns with no connection to the content
- Multiple competing focal points
