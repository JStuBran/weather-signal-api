# Weather Signal API

> x402 FastAPI service — bias-corrected temperature forecasts + Polymarket BUY/SELL signals

Fetch weather forecasts from [Open-Meteo](https://open-meteo.com/) with city-specific bias corrections, then generate statistically-grounded BUY/SELL signals for Polymarket weather markets. Signals are priced at **$0.05 USDC** via the [x402 protocol](https://x402.org) on Base mainnet.

---

## Features

- 🌤 **Forecasts** — bias-corrected max temperature for 8 supported cities (free endpoint)
- 📊 **Signals** — BUY_YES / BUY_NO / NO_EDGE with edge breakdown for Polymarket weather markets
- 💳 **x402 payments** — $0.05 USDC per signal on Base (ERC-20, no subscription required)
- ⚡ **Fast** — async httpx, no heavy ML dependencies
- 🐳 **Docker-ready** — deploy to Railway in one click

---

## Supported Cities

| City | Unit | Bias Correction |
|---|---|---|
| Seoul | °C | +1.0° |
| London | °C | 0.0° |
| New York City | °F | 0.0° |
| Wellington | °C | +2.5° |
| Buenos Aires | °C | 0.0° |
| Dallas | °F | 0.0° |
| Ankara | °C | 0.0° |
| Paris | °C | 0.0° |

Bias corrections account for systematic differences between the weather station used and local market expectations (e.g. microclimate offsets for Seoul, Wellington).

---

## Pricing

| Endpoint | Price |
|---|---|
| `GET /api/forecast` | **Free** |
| `POST /api/signal` | **$0.05 USDC** |

---

## Endpoints

### `GET /health`

Returns service liveness.

```json
{
  "status": "ok",
  "version": "1.0.0",
  "cities": ["Seoul", "London", "New York City", ...]
}
```

---

### `GET /api/forecast`

Returns the bias-corrected maximum temperature for a city on a given date. **No payment required.**

**Query parameters**

| Param | Type | Description |
|---|---|---|
| `city` | string | City name (see supported cities above) |
| `date` | string | Forecast date in `YYYY-MM-DD` format |

**Example request**

```bash
curl "https://weather-signal.up.railway.app/api/forecast?city=London&date=2026-03-10"
```

**Example response**

```json
{
  "city": "London",
  "date": "2026-03-10",
  "temperature": 12.4,
  "unit": "celsius",
  "bias_applied": 0.0,
  "raw_temperature": 12.4
}
```

**Seoul example (with +1.0° bias)**

```bash
curl "https://weather-signal.up.railway.app/api/forecast?city=Seoul&date=2026-03-10"
```

```json
{
  "city": "Seoul",
  "date": "2026-03-10",
  "temperature": 8.3,
  "unit": "celsius",
  "bias_applied": 1.0,
  "raw_temperature": 7.3
}
```

---

### `POST /api/signal`

Generate a BUY/SELL signal for a Polymarket weather market. **Requires $0.05 USDC x402 payment.**

**Request body**

```json
{
  "market": {
    "id": "market-london-mar10-above-12",
    "question": "Will London's max temperature exceed 12°C on March 10, 2026?",
    "city": "London",
    "threshold_celsius": 12.0,
    "condition": "above",
    "end_date": "2026-03-10",
    "outcomes": [
      { "title": "Yes", "probability": 0.45 },
      { "title": "No",  "probability": 0.55 }
    ]
  }
}
```

**Market object fields**

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | ✅ | Unique market identifier |
| `question` | string | ✅ | Human-readable question |
| `city` | string | ✅ | One of the supported cities |
| `threshold_celsius` | float | Either/or | Temperature threshold in °C |
| `threshold_fahrenheit` | float | Either/or | Temperature threshold in °F |
| `condition` | string | ✅ | `above`, `below`, or `exact` |
| `end_date` | string | ✅ | Resolution date `YYYY-MM-DD` |
| `outcomes` | array | ✅ | Exactly 2 outcomes: `[YES, NO]` with market probabilities |

**Example response**

```json
{
  "market_id": "market-london-mar10-above-12",
  "signal": "BUY_YES",
  "confidence": 0.142,
  "edge": 0.142,
  "forecast": {
    "city": "London",
    "date": "2026-03-10",
    "temperature": 12.4,
    "unit": "celsius",
    "bias_applied": 0.0,
    "raw_temperature": 12.4
  },
  "edge_breakdown": {
    "market_yes_prob": 0.45,
    "model_yes_prob": 0.592,
    "edge": 0.142,
    "threshold_temperature": 12.0,
    "forecast_temperature": 12.4
  },
  "processing_time_ms": 143.7
}
```

**Signal values**

| Signal | Meaning |
|---|---|
| `BUY_YES` | Model probability > market probability by ≥ 5% |
| `BUY_NO` | Market probability > model probability by ≥ 5% |
| `NO_EDGE` | Difference < 5% — not worth trading |

---

## x402 Payment Flow

All requests to `/api/signal` require an `X-Payment` header containing a valid x402 payment proof.

If the header is missing, the server returns **HTTP 402** with a machine-readable descriptor:

```json
{
  "x402Version": 1,
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "maxAmountRequired": "50000",
      "resource": "https://weather-signal.up.railway.app/api/signal",
      "description": "Weather-based Polymarket signal ($0.05 USDC)",
      "mimeType": "application/json",
      "payTo": "<WALLET_ADDRESS>",
      "maxTimeoutSeconds": 300,
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "extra": { "name": "USDC", "decimals": 6 }
    }
  ]
}
```

- `maxAmountRequired: "50000"` → 0.05 USDC (6 decimals)
- Asset: USDC on Base mainnet (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- Network: Base mainnet (`eip155:8453`)

Use an x402-compatible client to handle payment automatically and retry:

```bash
# Example with x402 Python SDK (https://github.com/coinbase/x402)
x402-pay \
  --private-key $PRIVATE_KEY \
  --url https://weather-signal.up.railway.app/api/signal \
  --method POST \
  --body '{"market": {...}}'
```

---

## Edge Calculation

The signal engine uses a **sigmoidal probability model**:

```
model_yes_prob = sigmoid(delta * 0.5)  # for "above" condition
model_yes_prob = sigmoid(-delta * 0.5) # for "below" condition

delta = forecast_temperature - threshold
```

This maps a point forecast to a probability:
- delta = +5°: ~97% confident YES
- delta = 0°: 50% (no signal)
- delta = -5°: ~3% confident YES

**Edge** = `model_yes_prob - market_yes_prob`

A minimum edge of **5%** is required before a BUY signal is issued.

---

## Deployment

### Railway (recommended)

1. Fork this repo
2. Connect to [Railway](https://railway.app)
3. Set environment variables:
   - `WALLET_ADDRESS` — your USDC wallet address on Base
   - `PAYMENT_REQUIRED` — `true` (default) or `false` for testing
4. Deploy — Railway auto-detects the Dockerfile

### Docker

```bash
docker build -t weather-signal-api .
docker run -p 8080:8080 \
  -e WALLET_ADDRESS=0xYourWalletAddress \
  -e PAYMENT_REQUIRED=false \
  weather-signal-api
```

### Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set PAYMENT_REQUIRED=false for local testing

uvicorn main:app --reload
```

---

## Example cURL

**Free forecast:**

```bash
curl "http://localhost:8080/api/forecast?city=Wellington&date=2026-03-15"
```

**Signal (payment bypassed locally):**

```bash
curl -X POST http://localhost:8080/api/signal \
  -H "Content-Type: application/json" \
  -H "X-Payment: test" \
  -d '{
    "market": {
      "id": "wlg-mar15-above-18",
      "question": "Will Wellington max temp exceed 18°C on March 15?",
      "city": "Wellington",
      "threshold_celsius": 18.0,
      "condition": "above",
      "end_date": "2026-03-15",
      "outcomes": [
        { "title": "Yes", "probability": 0.4 },
        { "title": "No",  "probability": 0.6 }
      ]
    }
  }'
```

---

## Architecture

```
main.py
├── CITIES config          — lat/lon/unit/bias per city
├── get_forecast()         — async Open-Meteo fetch + bias correction
├── calculate_edge()       — sigmoidal model → BUY/SELL/NO_EDGE
├── payment_middleware()   — x402 gate on /api/signal
└── FastAPI routes
    ├── GET  /health
    ├── GET  /api/forecast  (free)
    └── POST /api/signal    (x402 $0.05)
```

---

## License

MIT
