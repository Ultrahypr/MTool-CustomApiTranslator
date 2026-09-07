# MTool Custom API Translator

A companion tool for MTool that translates MTool's exported game-text JSON through a user-configured third-party API.

MTool supports many game engines, but its built-in translation service does not expose arbitrary API base URLs, endpoints, or response formats. This project uses MTool's external-translation workflow instead of modifying MTool's compiled runtime.

## Requirements

- Windows or another OS with Python 3.8+
- Python standard library only; no `pip install` is required
- MTool
- An API server compatible with one of the supported request formats

The GUI uses `tkinter`, which is included in most Windows Python installations.

## Quick start

1. Run `Open Custom API Translator.bat`, or run:

   ```text
   cd CustomApiTranslator
   python mtool_custom_api.py
   ```

2. Enter your API settings in the GUI:
   - **Base URL**: scheme and host, for example `https://example.com`
   - **Endpoint**: path, for example `/v1/responses`
   - **API key**: your key; it is stored locally in `custom_api_config.json`
   - **Model name**: the model identifier accepted by your server
   - **API / response type**: select the request/response format
   - **Auth header**: choose `bearer`, `x-api-key`, `none`, or `auto`
3. In MTool, export the game text using **Export Text for External Translate**. This creates `ManualTransFile.json` in the game directory.
4. Pick `ManualTransFile.json` in this tool.
5. Click **Test connection**.
6. Click **Translate**.
7. In MTool, load the generated translated JSON with **Apply loaded text** / the translation-file loading function.

The output is normally written beside the input file as:

```text
ManualTransFile.<target-language>.json
```

## Supported API types

| Type | Default endpoint | Request format | Response extracted from |
|---|---|---|---|
| `openai-chat` | `/v1/chat/completions` | OpenAI Chat Completions | `choices[0].message.content` |
| `openai-responses` | `/v1/responses` | OpenAI Responses | `output_text`, or message `output[].content[].text` |
| `openai-text` | `/v1/completions` | OpenAI legacy text completion | `choices[0].text` |
| `anthropic` | `/v1/messages` | Anthropic Messages | concatenated `content[].text` |
| `gemini` | `/v1beta/models/{model}:generateContent` | Gemini GenerateContent | `candidates[0].content.parts[].text` |
| `ollama-chat` | `/api/chat` | Ollama chat | `message.content` |
| `ollama-generate` | `/api/generate` | Ollama generate | `response` |
| `raw-text` | `/translate` | Custom `{text, source, target, model}` object | Entire response body |
| `custom` | none | OpenAI Chat-style request | User-supplied `responsePath` |

For a custom response format, set **API / response type** to `custom` and set a dot-separated response path, for example:

```text
data.translation
```

Array indexes are supported:

```text
result.items.0.text
```

## Request settings

### Batch

`Batch` is the number of game-text entries placed into one API request. For example:

- `Batch = 1`: one entry per request
- `Batch = 20`: up to 20 entries per request
- `Batch = 300`: up to 300 entries per request

A larger batch reduces request overhead but produces a longer prompt and a longer required reply. If the model truncates the response or omits keys, reduce the batch size.

`raw-text` always uses a batch size of 1 because that format returns one raw response per request.

### Workers

`Workers` is the maximum number of API requests sent concurrently. It is independent of `Batch`:

```text
Total entries / Batch = approximate number of requests
Workers = maximum requests in flight at the same time
```

Start conservatively, usually 2–8. High values can trigger provider rate limits, especially with free or shared API keys.

### MaxTok

`MaxTok` controls the maximum reply-token field sent to the selected API:

- OpenAI Chat / Text and custom: `max_tokens`
- OpenAI Responses: `max_output_tokens`
- Anthropic: `max_tokens`
- Gemini: `generationConfig.maxOutputTokens`
- Ollama: `options.num_predict`

The model and provider may impose a lower maximum. A high value does not force the model to generate that many tokens; it only raises the permitted ceiling.

### Other fields

- **Timeout(s)**: per-request network timeout.
- **Temp**: generation temperature where supported.
- **jsonMode**: requests JSON-object output for OpenAI Chat/custom servers that support `response_format`.
- **Source language / Target language**: inserted into the translation instruction.
- **System prompt**: configurable in `custom_api_config.json`; an empty value uses the built-in game-localization prompt.

## Configuration file

The GUI saves settings to:

```text
CustomApiTranslator/custom_api_config.json
```

A sanitized example:

```json
{
  "baseUrl": "https://example.com",
  "endpoint": "/v1/responses",
  "apiKey": "",
  "model": "your-model-name",
  "apiType": "openai-responses",
  "authStyle": "bearer",
  "responsePath": "choices.0.message.content",
  "sourceLang": "Japanese",
  "targetLang": "Simplified Chinese",
  "batchSize": 20,
  "workers": 4,
  "timeout": 120,
  "maxTokens": 16384,
  "jsonMode": false,
  "verifyTls": true,
  "extraHeaders": {},
  "systemPrompt": "",
  "temperature": 0.2
}
```

Do not commit or share a config containing a real API key. Keep a private copy of your configured `custom_api_config.json` and use a blank-key template for distribution.

## CLI usage

Open the GUI with no arguments:

```text
python mtool_custom_api.py
```

Translate a file using the saved configuration:

```text
python mtool_custom_api.py --file "D:\Game\ManualTransFile.json"
```

Choose an output path:

```text
python mtool_custom_api.py --file "D:\Game\ManualTransFile.json" --out "D:\Game\translated.json"
```

Override settings for one run:

```text
python mtool_custom_api.py \
  --file "D:\Game\ManualTransFile.json" \
  --api-type openai-responses \
  --base-url "https://example.com" \
  --endpoint /v1/responses \
  --api-key "YOUR_API_KEY" \
  --auth-style bearer \
  --model "your-model" \
  --batch-size 20 \
  --workers 4 \
  --max-tokens 16384
```

On Windows `cmd.exe`, use one line or replace the backslash continuations with `^`.

List supported API types:

```text
python mtool_custom_api.py --list-types
```

Test the configured server without translating a file:

```text
python mtool_custom_api.py --test
```

Inspect the number of entries and planned request sizes without network calls:

```text
python mtool_custom_api.py --dry-run --file "D:\Game\ManualTransFile.json"
```

Useful one-run options:

| Option | Meaning |
|---|---|
| `--config PATH` | Use a different JSON configuration file |
| `--file PATH` | MTool `ManualTransFile.json` input |
| `--out PATH` | Output translation file |
| `--test` | Send a small probe request and exit |
| `--dry-run` | Print the batch plan without API calls |
| `--retranslate` | Translate entries even if they already have values |
| `--api-type TYPE` | Override API type |
| `--auth-style STYLE` | Override authentication header style |
| `--batch-size N` | Override entries per request |
| `--workers N` | Override concurrent-request limit; clamped to 1–32 |
| `--max-tokens N` | Override reply-token limit |

## Checkpointing and failures

During translation, progress is saved to:

```text
<output-file>.progress.json
```

If the process stops, run the same command again. Completed entries are reused and only unfinished entries are sent again. The checkpoint is deleted after a fully successful run.

The translator:

- Retries transient failures, incomplete JSON replies, and missing batch keys.
- Fails fast for most 4xx client errors because repeating them will not fix credentials or request shape.
- Stops issuing new requests after a consecutive-failure circuit breaker trips, preventing a rate-limit retry storm.
- Keeps failed entries available for a later resume.

If you see repeated 429 responses:

1. Lower **Workers**.
2. Lower **Batch** if the provider has prompt-size or output-size limits.
3. Wait for the provider's rate limit window to reset.
4. Run again using the existing output/checkpoint files.

If a server returns Cloudflare error 1010, the tool already sends browser-like request headers. Check the endpoint and provider access policy if it still blocks the request.

## Translation-file format

MTool's exported file is a JSON object whose keys are original strings and whose values are current translations:

```json
{
  "Original Japanese line": "Original Japanese line",
  "Another line": ""
}
```

The translator sends original keys to the model and maps the model's numbered JSON reply back to the original keys. Model replies must preserve the same numeric keys and return only a JSON object for batched API types.

Placeholders and game control codes should be preserved, including examples such as:

```text
\\n
%s
{0}
<tags>
[control codes]
```

Always test a small section of a game before translating a large file.

## Security notes

- API keys are sent to the configured server and saved locally in the config file.
- Do not share `custom_api_config.json` after entering a key.
- Use `verifyTls: true` for normal HTTPS certificate verification. Disabling TLS verification is unsafe and should only be used for a trusted local test server.
- The tool has no external Python dependencies and does not upload files other than the request text sent to your configured API.

## Project files

```text
CustomApiTranslator/
  mtool_custom_api.py       translator GUI, CLI, request builders, parsers
  custom_api_config.json    local configuration; keep private after adding a key

Open Custom API Translator.bat
打开自定义API翻译.bat
```
