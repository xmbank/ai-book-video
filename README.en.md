# AI Book Video Workbench

[简体中文](./README.md) | English

This project is a local web workbench for turning Douyin book-selling content into traceable, replayable short-form video production tasks. It is not a generic video editor. Each task keeps source metadata, transcript repair, rewrite candidates, segmented audio, subtitles, scene images, book metadata, and final rendered outputs in one place.

The pipeline separates "ASR proofreading" from "attraction-oriented rewriting", confirms the real book and selling points before script generation, and supports local configuration for LLM, image generation, and Volcengine TTS credentials.

## Workflow

```text
Collection table / paste Douyin share link
  -> 1. ASR proofreading: fix wording, punctuation, slips, creator traces, risky expressions
  -> 2. Book and product: confirm real title, author/version, cover, verifiable selling points
  -> 3. Three-strategy rewrite: content card + multiple rewrite strategies
  -> 4. Segmented audio: split long narration, synthesize TTS, merge
  -> 5. Portrait storyboard: direct and generate native 9:16 scene images with QA
  -> 6. Style and quantity: choose multiple visual styles and output counts
  -> 7. Batch rendering: compose captions, book info, and declarations with HyperFrames
  -> 8. Review and logs: inspect scripts, product assets, scene QA, and rendered outputs
```

Shot count is automatically estimated from final TTS duration by default. The real pipeline stores prompt versions and prompt payloads with task artifacts for later review. Image generation uses native portrait outputs instead of cropping landscape grids into 9:16 frames.

## Start The Workbench

```bash
cd /path/to/ai-book-video
./start.sh
```

The script builds outdated frontend assets automatically and finds an available port. You can also set one explicitly:

```bash
WORKBENCH_PORT=8770 ./start.sh
```

## UI Capabilities

- ASR proofreading: edit original transcript and repaired transcript side by side.
- Book and product: inspect detected book identity, selling points, and AI-generated concept cover.
- Rewrite: compare the content card, three rewrite strategies, and quality scoring.
- Audio: inspect segment plans and merged audio output.
- Portrait storyboard: inspect native 9:16 scenes, contact sheets, visual intent, and image QA.
- Style: choose built-in styles and output counts.
- Render: preview and download generated MP4 files.
- Review: inspect product completeness, script scoring, image QA, compliance, and media checks.

Built-in styles include `book-sales`, `clean-narration`, `typewriter-dark`, `dark-knowledge`, and `book-broadcast`.

## Offline Demo

You can generate a demo task without external providers:

```bash
uv run book-video demo
```

Offline mode uses built-in transcript data, macOS system voice, and a local reference video for validation. It does not prove that your real LLM, image provider, or Volcengine TTS accounts are production-ready.

## Real Pipeline Configuration

Copy the template and fill in real values either in the web settings page or in `.env`:

```bash
cp .env.example .env
uv run book-video doctor
```

`.env`, `logs/`, `data/`, and other local runtime artifacts are ignored by Git by default. For open-source publishing, keep only `.env.example` in the repository. Do not commit real API keys, base URLs, cookies, or machine-specific absolute paths.

Required configuration:

- `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`: OpenAI-compatible Chat Completions for transcript repair, rewrite, book recognition, and visual direction.
- `IMAGE_API_KEY`, `IMAGE_MODEL`, `IMAGE_BASE_URL`, `IMAGE_SIZE`: OpenAI-compatible Images Generations for native 9:16 scene generation.
- `VOLC_TTS_API_KEY`, `VOLC_TTS_RESOURCE_ID`, `VOLC_TTS_VOICE_TYPE`: Volcengine Doubao Speech Synthesis 2.0.

Some Douyin share pages only return a placeholder web page. In that case the workbench falls back to the signed detail API, using `DOUYIN_COOKIE` from `.env` when available, otherwise the local Chrome session cookie. Cookies stay in-process and are not written into task artifacts or logs.

CLI equivalent:

```bash
uv run book-video run \
  --share "full Douyin share text or URL" \
  --keyword "health books" \
  --scene-count 0
```

## Publish To GitHub

Recommended approach: keep the current enterprise remote and add a separate GitHub remote.

```bash
git remote add github git@github.com:<your-name>/ai-book-video.git
git push -u github master
```

Before making the repository public, run `git status` again and confirm that `.env`, logs, data folders, and any other local private files are absent from the index.

## Task Persistence

```text
data/tasks/{task-id}/
├── task.json
├── pipeline-state.json
├── artifacts.json
├── source/
├── transcript/
├── scripts/
├── tts/
├── subtitles/
├── scene-images/
├── book/
├── styles/
├── output-plans/
├── render-project-*/
├── renders/
└── review/
```

Old versions are preserved instead of overwritten. Editing upstream artifacts only marks downstream stages as stale and ready to rerun.

## Development Validation

```bash
uv run python -m compileall -q src tests
uv run pytest -q
npm run build
```

Backend tests cover product gating, selling points, sales-focused storyboard generation, image QA and regeneration, cover upload, style invalidation, hero card rendering, and shared error handling.

## Out Of Scope

This repository does not currently include:

- automatic Douyin trending-page crawling with account login,
- automatic publishing to Douyin or WeChat Channels,
- commission attribution,
- multi-user permission management.

The first version still expects humans to judge which source links are worth turning into tasks. The system focuses on making downstream production reproducible and auditable.
