# VIGIL HTTP API

Base URL `http://localhost:8000`. All responses are JSON unless stated. Every response carries an
`X-Response-Time-ms` header. Failures return a structured, explainable body:

```json
{"state":"DEGRADED","what_failed":"…","affected":"…","fallback":"…"}
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | VIGIL system health, component states, data mode |
| GET | `/api/pulse` | market state, pressure, participation, attention, signal field, open cases |
| GET | `/api/cases` | case index |
| GET | `/api/cases/{case_id}` | one case with its full Evidence Ledger and interpretation |
| GET | `/api/universe` | instruments VIGIL covers |
| GET | `/api/forecast/{symbol}?horizon=&live=` | forecast contract, evidence, model debate |
| GET | `/api/forecast/{symbol}/history?horizon=` | out-of-sample probability trajectory with outcomes |
| GET | `/api/decision/{symbol}` | gate checks, uncertainty, drift, risk, modality, verdict |
| POST | `/api/forecast/{symbol}/stress` | re-run the gate under perturbed inputs |
| GET | `/api/audit/{forecast_id}` | automated integrity audit + reproducibility capsule |
| GET | `/api/replay?symbol=&date=&horizon=` | point-in-time reconstruction with leak guard |
| GET | `/api/replay/timeline?symbol=&start=&end=&step=&horizon=` | many reconstructions in sequence |
| GET | `/api/research/overview` | tournament, experiments, calibration, uncertainty, drift, backtest, decision quality, failure lab, patterns |
| GET | `/api/research/cemetery` | the most confident wrong predictions, with lessons |
| GET | `/api/research/compare?mode=model\|experiment\|regime\|symbol` | side-by-side comparison |
| GET | `/api/observatory` | architecture narrative + lake, quality, streaming, MapReduce, docstore, latency, lineage, runs |
| GET | `/api/graph` | correlation graph, communities, centrality |
| GET | `/api/recommend?risk=` | research shortlist with blocking reasons |
| GET | `/api/search?q=` | global search across instruments, cases, models, experiments, architecture |
| POST | `/api/human-vote` | record a human call for later comparison |
| POST | `/api/assistant` | grounded Q&A over measured artefacts only |
| GET | `/api/export/{kind}.{json\|csv\|pdf}` | `case`, `forecast`, `experiments`, `data-quality` |

Static: `/` landing page, `/app` application, `/static/*` assets.

## Notes
- `?live=true` on `/api/forecast/{symbol}` forces a point-in-time refit instead of serving the
  stored contract. It takes ~25 s on a laptop CPU; the service caches the fitted bundle.
- `/api/replay/timeline` defaults to at most 12 points because each point is a real retrain.
- The assistant answers only from stored artefacts. If a question is outside them it says so
  rather than generating a number.
