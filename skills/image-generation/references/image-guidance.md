# Prompting and visual checks

## Describe the requested result

Capture the subject, scene, action, framing, visual style, lighting, palette and background only when relevant. Use clear English for descriptive API prompts. Preserve words that must appear in the image verbatim, with an explicit lettering instruction; do not translate exact lettering without a request.

For edits, state each input's role: edit target, pose reference, identity reference or compositing input. Describe what should change and what must remain. For example: change the main character to adult Sakura while preserving the referenced pose and camera angle. A reference must be uploaded as an input file, not merely summarized in prose.

## Choose outputs

Use PNG for lossless details or supported transparency. JPEG or WebP can reduce file size. The client preserves the actual returned type; inspect the file rather than assuming the requested extension was honored. Output count and reference count are different quantities.

Use the shipped API contract for accepted CLI options. Provider behavior, model availability and supported parameters can change; consult the provider's current documentation when troubleshooting. This guide does not make current pricing, licensing or model-availability claims.

## Check the actual image

Verify subject, pose, count, composition, style, exact lettering, transparency and requested removals. For edits, compare against the selected source and check all preserved features. Keep a partial result if only some requested images returned, and report that difference. A successful request does not prove visual correctness or delivery to QQ/WeChat.

Use only inputs the user selected or authorized. Keep credentials and private reference material out of shared logs and repositories. Return the actual image through the host's supported delivery mechanism; do not claim a local file is visible in the user's platform chat without a successful send.
