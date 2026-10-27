# Skills: Content, presentations and design

These ten skills turn your thinking into things other people see: LinkedIn posts and series, a speaking deck, image prompts and real images, branded graphics, and slide decks in three formats (Markdown, PowerPoint, and Pencil exports). Nothing here publishes for you. You post, present and send the results yourself. For the full index of skills and how a skill is called, see `SKILLS.md`.

## /linkedin-post

Drafts one LinkedIn post in your own voice. The post opens with a single sentence that works as a hook, moves from an observation to an insight, and ends on a statement rather than a call to action or a request for comments. After the draft you get two alternative opening lines, three to five hashtags, a suggested posting time, and one line on what to cut if you need it shorter. The draft comes back in the conversation. Nothing is saved or published.

**How to call it.** `/linkedin-post <topic>`. Or say it in your own words, for example: "draft a post about our first warehouse pilot", "write a LinkedIn post on why we stopped doing quarterly targets". You can also give an angle, extra context and a length: short (about 150 words), medium (about 300, the default) or long (about 500).

**Example.**

```text
You:        /linkedin-post what three weeks inside a Northwind Logistics depot taught us about night shifts
Assistant:  Draft (medium, 287 words):
            "The busiest hour in a depot is the one nobody plans for."
            ...
            Alternative openers: (1) ... (2) ...
            Hashtags: #logistics #operations #nightshift
            Post Tuesday to Thursday, 7-9am your time. To shorten: cut paragraph 4.
```

**What it needs.** Your voice guide, three to five of your published posts from the LinkedIn archive (used as style examples), your company overview and strategy records, and the datastore index when the post cites facts or numbers. The posting time uses your time zone from `HEADING_OS_TZ` in `.env`. With no hashtag list in your records, it picks topic hashtags and says so.

**When not to use it.** For several connected posts with one arc, use `/linkedin-series`. To file a post you have already published, use `/linkedin-archive`. For a picture to go with the post, use `/image-prompt` or `/flux-image`.

## /linkedin-series

Plans a LinkedIn series of several connected posts around one theme. You get a series overview (the theme, why it matters now, the goal, the audience), then for each post a publish date, a working title, a draft opening line, the core angle, the proof point that anchors it, hashtags, and how it leads into the next post. The arc runs from a hook in post 1, through evidence and story, to a closing post about the future. At the end you get a ready-to-publish draft of post 1.

**How to call it.** `/linkedin-series <theme>`. Or say it in your own words, for example: "plan posts for the week about our move to four-day sprints", "content series on what the Acme Corp trade show taught us". You can also give the number of posts (default 4), a timeframe, and the goal of the series.

**Example.**

```text
You:        /linkedin-series speed versus compliance, 4 posts over the month before the Acme Corp summit
Assistant:  Series overview: ...
            Post 1 (Tue 3rd): "Compliance is not the brake. Paperwork is."  Angle: ...
            Post 2 (Tue 10th): ...
            Post 4 (Tue 24th): the call to the future ...
            Draft of post 1, ready to publish: ...
```

**What it needs.** Your voice guide, three to five of your published posts from the LinkedIn archive, your strategy, and your current data for proof points. Because a series is planned over several turns, the skill keeps a small working log, `<theme-slug>/.memlog.md` under the registry name `linkedin-drafts`, so a later session can pick the plan up where you left it. That log is ignored by git. The plan itself, with the Post 1 draft, is saved beside it as `<theme-slug>/series-plan.md`, and the skill gives you its full path. Running the skill again on the same theme updates that plan and keeps the posts already marked as published. Nothing is posted.

**When not to use it.** For a single post, use `/linkedin-post`. To file a post after publishing it, use `/linkedin-archive`.

## /linkedin-archive

Files a piece you have just published on LinkedIn. It takes the newest draft from your LinkedIn staging area (registry name `linkedin-drafts`), together with any images, and moves it into the archive (registry name `linkedin-archive`), into a folder for its type: articles, posts or comments. It handles exactly one piece per run, always shows you a dry-run plan first, and moves nothing until you approve.

**How to call it.** `/linkedin-archive [slug]`. Or say it in your own words, in English or Russian, for example: "I published this on LinkedIn", "the LinkedIn post is live". Every trigger phrase contains the word LinkedIn. Without a slug, the script takes the newest draft by modification time.

**Example.**

```text
You:        I published this on LinkedIn
Assistant:  Found 2026-11-04_depot-night-shifts/ with 1 image in media/. Type: post.
            Plan: move to the archive under posts/depot-night-shifts/ (2 files).
            Approve and execute (with commit), approve without commit, or cancel?
You:        Approve without commit
Assistant:  Done. 2 files moved to posts/depot-night-shifts/.
```

**What it needs.** Nothing beyond the core install. The skill runs `scripts/linkedin-archive.py`: dry run by default, `--execute` to apply, and optionally `--commit` to commit the move in your data repository. Other flags are `--slug`, `--type article|post|comment` and `--image <path>` (repeatable). If the type cannot be read from the file name (`_linkedin-post_`, `_linkedin-article_`, `_linkedin-comment_`) or a `type:` field in the frontmatter, it asks you. A draft inside a dated task folder brings the files in its `media/` folder with it. The draft must be tracked by git; if it is not, the skill tells you to `git add` it first. There is no undo flag.

**When not to use it.** To write a post, use `/linkedin-post` or `/linkedin-series`. For a question about LinkedIn analytics or your profile, just ask; no skill is needed.

## /keynote-deck

Writes the full script of a talk for an event, a VIP meeting or a government meeting: the slide structure plus what you say. The deck follows six parts: an opening hook, the problem in the audience's own terms, the solution, proof, the future, and a close with one clear ask. For every slide you get a title, a one-sentence key message, the content with a visual suggestion, and speaker notes in your voice. You review it. It does not build the slide file.

**How to call it.** `/keynote-deck <event> <topic>`. Or say it in your own words, for example: "keynote deck for the Acme Corp logistics summit", "conference slides for a 20-minute talk on cold-chain visibility". It also uses the audience, the length in minutes (which sets the slide count) and any goals you give it.

**Example.**

```text
You:        /keynote-deck Northwind Partner Day, 20 minutes, why route data beats route plans
Assistant:  Missing records: current-data is empty (proof slides left as placeholders).
            Slide 1  Title: "Every route plan is wrong by 9am"
                     Key message: ...  Content: ...  Speaker notes: ...
            ...
            Slide 14 Close: ask for a pilot conversation ...
```

**What it needs.** Your company records: company overview, strategy, current data, pipeline and voice guide, plus market intelligence, geopolitical landscape and the datastore index when present, and an optional per-skill frame under `skill-data`. When a record is missing or empty, the deck names it at the top, tells you to run `/setup-wizard` or write the record, and carries on with a neutral frame. It never invents a deployment, a partner, a customer or a figure; a proof slide with no data stays a marked placeholder.

**When not to use it.** For a fundraising deck, use `/investor-pitch`. To turn the script into an editable PowerPoint file, use `/pptx-generator`. For a quick internal deck from Markdown, use `/marp`.

## /image-prompt

Reads the text you just produced (a post, an article, an email, slide copy) and writes a prompt for an AI image tool to go with it. You get the core theme in one sentence, a 50 to 150 word photorealistic prompt (subject, composition, lighting, colour, atmosphere), the parameters (16:9, photorealistic, no text in the image), and a Midjourney suffix. It returns text only and creates no image file.

**How to call it.** `/image-prompt [content or description]`, usually straight after another piece of content. Or say it in your own words, for example: "visualize this", "generate an image prompt for that post".

**Example.**

```text
You:        /image-prompt
Assistant:  Core Theme Identified: night shifts carry the whole depot.
            Image Prompt: a loading bay at 3am, a single forklift under sodium light,
            wet concrete reflecting ...
            Parameters: Aspect Ratio 16:9, photorealistic, no text.
            Platform Suffix (Midjourney): --ar 16:9 --v 6.1 --style raw --s 250
```

**What it needs.** Nothing beyond the core install. Its reference file also gives the adjustments for DALL-E, Stable Diffusion, Ideogram and Flux when you are not using Midjourney. The prompt never asks for text, logos, stock-photo cliches, military or fear-based imagery, or a crowded scene with several competing subjects.

**When not to use it.** To produce the actual image, use `/flux-image` (you can pass it this prompt). For layouts, infographics, logos or mockups, use `/design`.

## /flux-image

Generates real image files from a text prompt through the Replicate API. Two models are available: Nano Banana 2 from Google DeepMind (`banana`, the default) and FLUX.2 max from Black Forest Labs (`flux-max`), which the skill recommends for maximum photorealism or complex scenes. The images are saved under the registry name `generated-images` in your data repository. You see each one inline.

**How to call it.** `/flux-image <prompt>`. Or say it in your own words, for example: "generate an image of a container port at dawn", "make a picture for this post". It also works as the second step after `/image-prompt`: it takes the Image Prompt text, without the parameters or the Midjourney suffix.

**Example.**

```text
You:        /flux-image a quiet warehouse aisle at dawn, long shadows, no people, 16:9
Assistant:  Generated with banana: <generated-images>/warehouse-aisle-dawn.png
            [shows the image]
```

**What it needs.** `REPLICATE_API_TOKEN` in `.env` (get one from your Replicate account). The skill file gives a rough price of about $0.05 per image for either model. The script, `generate_image.py`, accepts `--prompt` (required), `--output`, `--aspect-ratio` (`1:1`, `16:9` default, `21:9`, `2:3`, `3:2`, `4:5`, `5:4`, `9:16`, `9:21`), `--model banana|flux-max`, `--num-outputs 1-4`, `--format png|jpg|webp` and `--seed`. Only the prompt leaves your machine. Before sending it, the skill checks whether the session is marked sensitive; unless sensitivity has been explicitly cleared, it strips names, codenames, deal terms and other identifying detail from the prompt first.

**When not to use it.** For the text prompt alone, use `/image-prompt`. For branded layouts, infographics, logos, image editing, upscaling or background removal, use `/design`.

## /design

A design studio for branded visuals. It sends each request down one of three routes. Layout, text and data work (social graphics, infographics, mockups, cards) is written as HTML and rendered to PNG locally, at no cost. Creative imagery (illustrations, photos, vector logos, graphics with text, edits, upscaling, background removal) goes to Replicate AI models. Presentations are handed to `/pptx-generator`. HTML renders and their source land under the registry name `design-output`; AI images land under `generated-images`.

**How to call it.** `/design <type> <brief>`. The types are `social`, `infographic`, `mockup`, `card`, `illustration`, `photo`, `logo`, `text-graphic`, `brochure`, `edit <instruction>`, `upscale <path>`, `remove-bg <path>`, and `draft` (a fast concept with FLUX Schnell, about $0.003 by the skill file). Or say it in your own words, for example: "design a LinkedIn graphic for our Q3 numbers", "design a logo for Acme Corp's new fleet app".

**Example.**

```text
You:        /design infographic five steps of the Northwind returns process, LinkedIn size
Assistant:  Brief: 5 steps, 1200x628, dark background, one message. Confirm?
You:        Yes
Assistant:  Rendered: <design-output>/returns-process.png  [shows it]
            Want changes? I edit the HTML and re-render.
```

**What it needs.** For the HTML route, the `browser` extra (`uv sync --extra browser`) and Playwright's Chromium; the renderer asks for `playwright install chromium` when it is missing. For the AI routes, `REPLICATE_API_TOKEN` in `.env`. Your brand kit lives in the data repository (registry name `skill-brand-assets`, for `design` and `pptx-generator`); with none, renders come out unbranded and the skill says so. It confirms the brief before generating, starts with one variant, and runs the same sensitivity check as `/flux-image` before any prompt leaves the machine.

**When not to use it.** For one image from a prompt, use `/flux-image`. For slides or a LinkedIn carousel, use `/pptx-generator`. For a Markdown deck, use `/marp`.

## /marp

Builds presentations from Markdown with your brand theme and renders them to PDF and HTML (and PNG on request). It is meant for internal decks, runbooks, state checks and turning notes into slides. It never produces PowerPoint. New decks are written to `marp-source/` under the registry name `presentations`; renders land beside the source unless you give an output folder.

**How to call it.** `/marp new <topic>`, `/marp render <path>`, `/marp from <path>` or `/marp watch <path>`. Or say it in your own words, for example: "turn this into slides", "quick slides on the new on-call rota". In plain words, the skill picks the subcommand: a topic with no file means `new`, a file outside `marp-source/` means `from`, a MARP source means `render`, and "preview" or "live" with a path means `watch`.

- `new` drafts a deck of cover, context, three to five main points, takeaway and close. Options: `--slides N`, `--mode dark|light|mixed` (default mixed), `--no-render`, `--title`, `--images png`. It refuses to overwrite an existing file unless you rename or add `--force`.
- `render` renders an existing source: `--pdf-only` or `--html-only`, `--images png`, `--output <dir>`, `--auto-sanitize`.
- `from` turns any Markdown in your records into slides without changing the file: `--break-at h2|h3|manual`, `--mode`, `--title`, `--subtitle`, `--no-auto-cover`, `--no-auto-closing`, `--paginate-heavy`, `--output`.
- `watch` starts a live preview; `watch stop` and `watch status` manage it. One preview at a time.

**Example.**

```text
You:        /marp new handover process for the Acme Corp support desk --slides 6
Assistant:  Source: <presentations>/marp-source/2026-11-04-acme-support-handover.md
            Rendered PDF and HTML beside it. 8 slides. Hidden characters: none.
```

**What it needs.** The `marp` command on your PATH; the renderer pins `@marp-team/marp-cli@4.4.0` and, when it is missing, tells you to run `npm install -g @marp-team/marp-cli@4.4.0`. A theme in your data repository (registry name `skill-brand-assets` for `marp`); with no theme, `render` says so rather than producing an unbranded deck. `.venv/bin/python scripts/marp_render.py --self-test` checks the setup.

**When not to use it.** For an editable, brand-heavy client deck or a LinkedIn carousel, use `/pptx-generator`. For a Pencil deck, use `/pencil-export`.

## /pptx-generator

Creates and edits PowerPoint files in your brand, using a cookbook of 16 reusable slide layouts. It has three modes: a 16:9 presentation, a square LinkedIn carousel exported to PDF, and managing the layouts themselves. Before generating, it shows you a slide plan table, then builds the deck in batches of at most five slides, checks each batch, and combines them into one file. Part files are deleted. Decks are saved under the registry name `deck-outputs`, in a folder per brand. The files open in PowerPoint, Google Slides and Keynote.

**How to call it.** `/pptx-generator <brand> <topic>`. Or say it in your own words, for example: "create slides for acme on our 2027 roadmap", "make a LinkedIn carousel about five packing mistakes", "edit this pptx", "add a layout to the cookbook".

**Example.**

```text
You:        create slides for northwind on the Q4 depot results
Assistant:  Plan (9 slides):
            | # | Layout        | Title                 | Key content     |
            | 1 | title-slide   | Q4 depot results      | ...             |
            | 2 | stats-slide   | Three numbers that moved | ...          |
            ...
            Generate?
You:        Go
Assistant:  Batch 1 (5 slides) checked. Batch 2 (4 slides) checked.
            Saved: <deck-outputs>/northwind/q4-depot-results.pptx
```

**What it needs.** The `documents` extra (`uv sync --extra documents`, which brings python-pptx). At least one brand in your data repository: the skill resolves the brands folder with `scripts/data-path.py path skill-brand-assets skill=pptx-generator`, and if only the empty `template` brand exists, it stops and walks you through creating one (colours, fonts, optionally a logo and tone of voice). Carousel PDFs need LibreOffice installed. The skill calls no outside API itself; before any of its text goes to an image tool or a web lookup, it runs the same sensitivity check as `/flux-image`.

**When not to use it.** For non-slide visuals, use `/design`. For a quick internal deck from Markdown, use `/marp`. To export a deck designed in Pencil, use `/pencil-export`. For the talk content itself, start with `/keynote-deck` or `/investor-pitch`.

## /pencil-export

Exports a deck designed in the Pencil desktop app (a `.pen` file) to per-slide PNG, a PDF, a PowerPoint file and a self-contained HTML file. Pencil renders PNG and PDF natively. The PowerPoint file is editable by default: each slide is the exact Pencil render as a background, with native text boxes laid on top and your brand fonts embedded. A locked-look, image-per-slide PowerPoint is available on request. Everything lands in an `export/` folder next to the deck. The skill is written for Pencil on Windows with the engine in WSL.

**How to call it.** `/pencil-export <path-to.pen> [--formats png,pdf,html,pptx,pptx-flat]`. Or say it in your own words, for example: "export the pencil deck to pdf", "make an editable version of the Acme pitch pencil deck".

**Example.**

```text
You:        /pencil-export decks/acme-pitch/acme-pitch.pen --formats pdf,pptx
Assistant:  12 Slide-* frames found (component frames skipped).
            Written: export/acme-pitch.pdf (12 pages), export/acme-pitch.pptx (12 slides, fonts embedded).
            No overlap warnings.
```

**What it needs.** The Pencil desktop app with the `.pen` open as the active document, and its MCP server connected to Claude Code. The `browser` extra with Playwright's Chromium and the `documents` extra (python-pptx). Your brand fonts in `fonts/` under the registry name `brand-kit`; only TTF and OTF fonts can be embedded. The script, `scripts/pencil-export.py`, also takes `--keep-in-bg <Name>` (keep that component's text in the background), `--width` and `--height` (default 1920x1080), `--scale` (default 2), and `--image-format jpeg` with `--quality` (default 85) to shrink large decks. Because Pencil writes a WSL path to the wrong disk, the skill stages every export on `C:` and collects it from `/mnt/c`, then checks each file on disk before reporting success.

**When not to use it.** For a Markdown deck, use `/marp`. For a branded PowerPoint built from scratch, use `/pptx-generator`.
