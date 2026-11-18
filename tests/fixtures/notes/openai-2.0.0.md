## 2.0.0 (2025-09-30)

Full Changelog: [v1.109.1...v2.0.0](https://github.com/openai/openai-python/compare/v1.109.1...v2.0.0)

### ⚠ BREAKING CHANGES

* **api:** `ResponseFunctionToolCallOutputItem.output` and `ResponseCustomToolCallOutput.output` now return `string | Array<ResponseInputText | ResponseInputImage | ResponseInputFile>` instead of `string` only. This may break existing callsites that assume `output` is always a string.

### Features

* **api:** Support images and files for function call outputs in responses, BatchUsage ([4105376](https://github.com/openai/openai-python/commit/4105376a60293581371fd5635b805b717d24aa19))