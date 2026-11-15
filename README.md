# AdsCrawl selective-rendering Apify Actor

This standalone Python Actor accepts 1–25 public URLs. Each item must explicitly set `render` to `false` (ordinary HTTPX GET) or `true` (AdsCrawl `POST https://api.adscrawl.net/html` with `contentMode: "html"`). A selector is applied locally to the full returned HTML and is never sent to AdsCrawl.

This is an AdsCrawl-maintained community project, not an Apify endorsement or a claim of Store approval. Verification so far is limited to offline mock tests; no live AdsCrawl production request has been made. Apify cloud builds/runs may consume platform usage, and rendered requests consume AdsCrawl usage under the user's plan. This repository does not include an API key or purchase a service plan. Use it only on sites you are authorized to access.

## Input

```json
{
  "urls": [
    {"url": "https://example.com/", "render": false},
    {"url": "https://example.com/app", "render": true, "selector": ".product"}
  ],
  "timeoutSeconds": 20,
  "adscrawlApiKey": "provided as an Apify secret input"
}
```

The key field is marked `isSecret: true`; no key is stored in this repository. The `urls` array uses Apify's `schemaBased` editor because each item is a custom object, not a Crawlee request-list source. See the [Actor input schema specification](https://docs.apify.com/actors/development/actor-definition/input-schema/specification/v1).

## Output semantics

Every successful item includes `url`, `rendered`, `title`, `text`, `items`, `finalUrl`, `status`, and `apiStatus`.

* Ordinary fetching reports the actual redirect-resolved `finalUrl`, actual target HTTP `status`, and `apiStatus: null`.
* Rendered fetching reports `finalUrl: null` and `status: null`, because the AdsCrawl HTML endpoint does not expose the target browser final URL or target-page HTTP status. It reports the endpoint response as `apiStatus` (normally `200`). This avoids presenting API metadata as target-page metadata.

Rendered success requires a raw `text/html` response. Individual failures are sanitized; response bodies and secrets are not written to output or logs. Limits are 2 MB per response, 60 seconds per request, three ordinary redirect hops, and 25 URLs. URL validation rejects credentials, control characters, backslashes, non-public literal IPs, numeric/hex dotted IP shorthand, single-label hosts, malformed labels, local/internal suffixes, unsupported ports, and invalid schemes. DNS names are not resolved, so this validator is not a DNS-rebinding defense.

## Direct local usage

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m src
```

For a real Apify run, provide input through the platform/SDK; a rendered run also requires the secret `adscrawlApiKey`. Local tests never call AdsCrawl, public sites, or a live API key.

For the local selector/rendering pattern, see the [AdsCrawl selective browser rendering tutorial](https://github.com/AdsCrawl/scrapy-adscrawl/blob/main/docs/tutorials/selective-browser-rendering.md).

## Offline checks

```bash
pip install -r requirements.txt pytest pytest-asyncio pytest-socket ruff jsonschema
pytest --disable-socket --allow-unix-socket
ruff check .
ruff format --check .
```

The regression suite validates the schema with Python `jsonschema` and checks Apify-specific editor and secret fields. No live URLs or credentials are used.
