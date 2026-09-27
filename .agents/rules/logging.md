# Unified Logging & Diagnostic Directives

> **Logs must be concise, structured, and machine-parsable without sacrificing diagnostic correctness. Never truncate required numerical precision or suppress exception tracebacks to save tokens. Never log secrets or credentials, and never delete diagnostic evidence unilaterally.**

## 1. Information Integrity & Diagnostic Value
- **Correctness Over Token Economy:** Token thrift must never compromise the ability to diagnose bugs or reconstruct events. Preserve full exception tracebacks, root causes, and error payloads for `ERROR` and `CRITICAL` levels.
- **Precision Preservation:** Retain sufficient precision for financial and numerical quantities; do not round small non-zero quantities into meaningless zeros.
- **Correlation Identifiers:** Include stable correlation identifiers (such as session, execution, order, symbol, or stage identifiers) when concurrent or multi-stage pipelines make event attribution ambiguous.
- **Strict Credential Redaction:** NEVER log broker/exchange API credentials, private secret keys, app keys, passphrase tokens, certificates, webhook secrets, or sensitive account identifiers.

## 2. Standard Category Taxonomy & Structured Output
- **Fixed Domain Category Taxonomy:** Classify all operational log events using one of six fixed category tags:
  - `[SYS]`: Infrastructure lifecycle, configuration, external service connections, and host runtime resources.
  - `[DATA]`: Corporate disclosures (DART), financial statements, consensus, and market data ingestion/normalization.
  - `[RESEARCH]`: Event studies, corporate action analysis, financial metrics, and qualitative catalyst synthesis.
  - `[MODEL]`: Quantitative evaluation engines, scoring pipelines, and LLM inference workflows.
  - `[RISK]`: Data quality thresholds, parser failures, numerical anomalies, and hallucination guardrails.
  - `[AUDIT]`: Source provenance, primary citation tracking, report publishing, and immutable artifact persistence.
- **Consistent Log Format:** Operational pipeline transitions and lifecycle log messages must include the category prefix: `[<CATEGORY>] <event description>`. Structured logging or standardized key-value contexts are strongly preferred.
- **Structured Fields:** Prefer structured key-value pairs or machine-readable records over unstructured conversational sentences.
- **Large Collection Summaries:** Never dump massive arrays, tabular data, or raw market order book depth directly into logs. Summarize collections using concise descriptors: count, shape, min/max range, null count, or representative head/tail samples.

## 3. Separation of Concerns & High-Frequency Output
- **Operational vs. High-Frequency Logs:** Keep high-frequency streaming data, order book depth, and dense telemetry isolated in dedicated storage destinations rather than spamming primary operational logs.
- **INFO vs. DEBUG/TRACE:**
  - **`INFO`**: High-level, human-readable phase transitions and milestone summaries (typically 1 line per phase).
  - **`DEBUG` / `TRACE`**: Fine-grained, structured diagnostic events targeted for troubleshooting and programmatic inspection.

## 4. Lifecycle & Path Hygiene
- **Configured Storage Paths:** Persistent service logs must reside in the designated project log path. Temporary diagnostic logs should use distinct scratch locations.
- **Non-Destructive Cleanup:** Temporary diagnostic logs may be cleared via project-designated maintenance scripts or explicit lifecycle commands. The AI must never unilaterally delete diagnostic logs or historical evidence needed for unresolved investigations.
