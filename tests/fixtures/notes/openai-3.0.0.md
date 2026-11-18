## [3.0.0](https://github.com/openai/openai-python/compare/v2.54.0...v3.0.0) (2026-08-12)


### ⚠ BREAKING CHANGES

* **api:** HTTPX2 is now the default HTTP client, and `httpx` is no longer installed automatically. Applications using custom HTTPX clients, transports, or configuration objects must migrate to their HTTPX2 equivalents or use the temporary, runtime-only legacy HTTPX escape hatch. See the [HTTPX2 migration guide](https://github.com/openai/openai-python/blob/main/httpx2.md).

### Features

* **api:** migrate to HTTPX2 ([#3594](https://github.com/openai/openai-python/pull/3594))