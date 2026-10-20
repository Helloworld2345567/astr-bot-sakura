---
name: image-generation
description: Generate images or edit user-selected reference images through a configured OpenAI-compatible image API. Use for explicit image creation or editing requests; do not use for ordinary chat, slash commands, bracketed control instructions, or requests explicitly excluding images.
---

# Image generation and reference editing

Use the bundled [Python client](scripts/image_api.py) for image generation and editing. Read the [API contract](references/direct-api-contract.md) and [image guidance](references/image-guidance.md) before a request. The client is self-contained and can be copied with this entire skill directory.

## Decide whether to run

- Run for an explicit request to create or edit an image. A request without reference images can still create a new image; a reference-edit request must have the specified inputs.
- Do not generate for `/stop`, other slash commands, messages containing `[]` control instructions, or an explicit request not to generate an image.
- In Sakura's automatic reply flow, the plugin owns generation and delivery after the text reply. Do not call this skill in parallel or produce a second image.
- Follow the current tool permissions. This skill does not grant administrator access or bypass confirmation for host operations.
- If the user requests a reference edit, use their actual selected image(s), including their selected ordinal. If a specified image is missing, ask them to resend it; never substitute a text-only generation.

## Configure

Credentials come from `SAKURA_IMAGE_API_KEY` by default. To use another variable, pass its name with `--api-key-env`. Never put the secret itself in a prompt, command argument, example, log or committed file. The CLI defaults to `https://api.openai.com/v1` and `gpt-image-1`; pass `--base-url` and `--model` for your provider. `OPENAI_BASE_URL` can also set the CLI base URL. HTTPS is required.

The plugin separately passes its WebUI provider settings to this same client. Standalone skill invocations must pass equivalent provider settings explicitly; they do not read the plugin configuration automatically.

Locate this skill using the `SKILL.md` path supplied by the host, then resolve `scripts/image_api.py` relative to that directory. Do not assume the host's installation path, operating system or working directory. Commands below run from this skill directory, in the Python environment that has `requests` installed.

## Generate one image

```sh
python scripts/image_api.py generate --prompt "An adult silver-haired catgirl maid greeting the viewer, expressive emerald eyes, soft light, simple background, anime illustration, 3:2 framing" --model gpt-image-1 --n 1 --output-format jpeg --output-compression 96 --output-dir assets
```

## Edit the selected reference image

```sh
python scripts/image_api.py edit --prompt "Use the attached pose reference. Replace the main subject with adult Sakura, a silver-haired catgirl maid with emerald eyes. Preserve the action and camera framing; change only the character identity." --image reference.png --model gpt-image-1 --n 1 --output-format jpeg --output-dir assets
```

Repeat `--image` for additional user-selected references. State the role of each input in the prompt. `n` is the output count, not the number of inputs. Use `--mask mask.png` for an optional PNG mask. Paths in examples are placeholders for user-provided files.

Use `--dry-run` to validate options without sending a request or requiring a key. Input files must still exist for edit validation. `--record assets/request.json` optionally saves a key-redacted request/result record; it can contain private prompts and local paths, so keep it out of shared repositories.

## Validate and deliver

Translate descriptive prompts into clear English, preserving exact requested lettering verbatim. Keep the requested subject, pose, style and edit invariants; do not add unrelated changes. The client uses only the options described in its contract, and makes no guarantee that a provider supports a particular model or parameter.

Inspect the saved output, verify the requested edit and actual image format, and report the returned count. An HTTP success is not visual validation. Deliver the actual file using the host's image-send capability; do not expose a host-local path as if a remote chat user could open it. On failure, report the stage and available evidence without exposing secrets or claiming an image was generated or sent. Do not silently retry the whole batch or fall back from editing to generation.
