# OptionsAji v3.0 Neo-Brutalist

Minimal GEX-only API surface for v3 frontend:

| Chart | Endpoint |
|-------|----------|
| Strike Gamma distribution | `GET /api/options/gex/{symbol}` or `GET /api/stock/{symbol}/gex` |
| Net GEX vs close trend | `GET /api/options/gex/history/{symbol}` → `gexSeries` + `priceCloses` |
| Gamma Flip estimation | Same history endpoint → `gexSeries[].gammaFlip` |

Default symbol: SPY. Realtime Futu data when `futu_enabled=true`.
