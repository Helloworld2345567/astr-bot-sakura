# Bundled image client contract

This describes `scripts/image_api.py` shipped with this skill, rather than a promise about every compatible provider. Both the Sakura plugin and standalone skill use this client.

## Connection and credentials

- Default base URL: `https://api.openai.com/v1`; override through `--base-url` or `OPENAI_BASE_URL`.
- Default model: `gpt-image-1`; override through `--model` for the chosen provider.
- Default credential variable: `SAKURA_IMAGE_API_KEY`; select another with `--api-key-env VARIABLE_NAME`.
- Credentials are read from the environment and sent as a Bearer header. Only HTTPS API URLs without embedded credentials, query or fragment are accepted. Redirects are disabled.
- Standalone CLI use can prompt for a missing key; the plugin preflights the variable before starting the client. Configure unattended jobs with a process environment variable.

## Generation

`POST {base_url}/images/generations` with a JSON body containing `model`, `prompt`, `n`, and supported output options.

## Reference editing

`POST {base_url}/images/edits` with multipart form data. The text fields include `model`, `prompt`, `n`, and output options. Every selected input is uploaded as a repeated `image` field. An optional PNG mask uses `mask`.

```python
files = [
    ("image", (target.name, target_handle, target_mime)),
    ("image", (reference.name, reference_handle, reference_mime)),
]
```

Do not collapse these fields into a dictionary. Input count is independent of `n`. Preserve the actual selected source bytes, rather than replacing the reference with a text description.

## Parameters and limits

- `n`: 1–10; Sakura automatic illustration requests exactly 1 output.
- `output_format`: `png`, `jpeg`, or `webp`. JPEG is the default.
- `output_compression`: 0–100 for JPEG/WebP; default 96. Do not send it for PNG.
- `background`: optional `auto`, `opaque`, or `transparent`; transparency cannot use JPEG.
- The client omits `quality` and `size`; describe composition and framing in the prompt.
- Each input must be a non-empty PNG/JPEG/WebP file no larger than 10 MiB. Masks must be PNG. Combined input bytes must not exceed 22 MiB.
- Combined outputs are capped at 24 MiB. The script derives saved extensions from actual output signatures.

Providers must support the parameters being sent. Use an actual provider-supported model; do not infer availability from its name or from another service's documentation.

## Results and failures

The client accepts an image body or JSON `data` items containing `b64_json` or an HTTPS image URL. It validates file signatures and caps downloads. It reports `requested_count` and `returned_count`, and preserves completed outputs.

There is at most one retry for HTTP 502/503. Do not silently retry authentication or rate-limit errors. Network/credential blockers and API failures must be reported separately from output inspection and platform delivery. Request records omit the API key but can include private prompts, response error text and file paths.
