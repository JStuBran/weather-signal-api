"""
Weather Signal API — x402 FastAPI Service
Bias-corrected temperature forecasts + Polymarket BUY/SELL signals
"""

import logging
import os
import time
from datetime import date, datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PAYMENT_REQUIRED = os.getenv("PAYMENT_REQUIRED", "true").lower() == "true"
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x0000000000000000000000000000000000000000")
VERSION = "1.0.0"
RESOURCE_URL = "https://weather-signal.up.railway.app/api/signal"

# Price: $0.05 → 50000 USDC micro-units (6 decimals)
SIGNAL_PRICE_UNITS = "50000"

# ---------------------------------------------------------------------------
# City registry
# ---------------------------------------------------------------------------

CITIES: dict[str, dict] = {
    "Seoul":          {"lat": 37.4691,  "lon": 126.4513, "unit": "celsius",    "bias": 1.0},
    "London":         {"lat": 51.4775,  "lon": -0.4614,  "unit": "celsius",    "bias": 0.0},
    "New York City":  {"lat": 40.6413,  "lon": -73.7781, "unit": "fahrenheit", "bias": 0.0},
    "Wellington":     {"lat": -41.3272, "lon": 174.8052, "unit": "celsius",    "bias": 2.5},
    "Buenos Aires":   {"lat": -34.8222, "lon": -58.5358, "unit": "celsius",    "bias": 0.0},
    "Dallas":         {"lat": 32.8998,  "lon": -97.0403, "unit": "fahrenheit", "bias": 0.0},
    "Ankara":         {"lat": 40.1281,  "lon": 32.9951,  "unit": "celsius",    "bias": 0.0},
    "Paris":          {"lat": 49.0097,  "lon": 2.5479,   "unit": "celsius",    "bias": 0.0},
}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("weather-signal")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ForecastResponse(BaseModel):
    city: str
    date: str
    temperature: float
    unit: str
    bias_applied: float
    raw_temperature: float


class MarketOutcome(BaseModel):
    title: str
    probability: float = Field(..., ge=0.0, le=1.0)


class PolymarketMarket(BaseModel):
    """Simplified Polymarket market object."""

    id: str
    question: str
    city: str
    threshold_celsius: Optional[float] = None
    threshold_fahrenheit: Optional[float] = None
    condition: str = Field(
        ...,
        description="above | below | exact",
        pattern="^(above|below|exact)$",
    )
    end_date: str = Field(..., description="YYYY-MM-DD")
    outcomes: list[MarketOutcome] = Field(..., min_length=2, max_length=2)


class SignalRequest(BaseModel):
    market: PolymarketMarket


class EdgeBreakdown(BaseModel):
    market_yes_prob: float
    model_yes_prob: float
    edge: float
    threshold_temperature: float
    forecast_temperature: float


class SignalResponse(BaseModel):
    market_id: str
    signal: str          # BUY_YES | BUY_NO | NO_EDGE
    confidence: float
    edge: float
    forecast: ForecastResponse
    edge_breakdown: EdgeBreakdown
    processing_time_ms: float


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


async def get_forecast(city: str, target_date: str) -> ForecastResponse:
    """Fetch temperature from Open-Meteo and apply city-specific bias."""
    if city not in CITIES:
        raise ValueError(f"Unknown city: '{city}'. Supported: {', '.join(CITIES)}")

    cfg = CITIES[city]
    unit_param = "fahrenheit" if cfg["unit"] == "fahrenheit" else "celsius"

    params = {
        "latitude": cfg["lat"],
        "longitude": cfg["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": unit_param,
        "start_date": target_date,
        "end_date": target_date,
        "timezone": "UTC",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    temps = data.get("daily", {}).get("temperature_2m_max", [])
    if not temps:
        raise ValueError(f"No forecast data returned for {city} on {target_date}")

    raw_temp = temps[0]
    biased_temp = raw_temp + cfg["bias"]

    logger.info(
        "forecast — city=%s date=%s raw=%.1f bias=%.1f result=%.1f %s",
        city, target_date, raw_temp, cfg["bias"], biased_temp, cfg["unit"],
    )

    return ForecastResponse(
        city=city,
        date=target_date,
        temperature=round(biased_temp, 2),
        unit=cfg["unit"],
        bias_applied=cfg["bias"],
        raw_temperature=round(raw_temp, 2),
    )


def _model_yes_probability(
    forecast_temp: float,
    threshold: float,
    condition: str,
) -> float:
    """
    Convert a point forecast to a model probability for the YES outcome.

    Uses a simple sigmoidal edge based on distance from the threshold.
    The further from the threshold, the more confident the model is.
    """
    delta = forecast_temp - threshold  # positive → above threshold

    if condition == "above":
        # YES = temperature finishes above threshold
        # Map delta to probability: delta +5 → ~0.97, delta -5 → ~0.03
        prob = 1 / (1 + 2.718281828 ** (-delta * 0.5))
    elif condition == "below":
        # YES = temperature finishes below threshold
        prob = 1 / (1 + 2.718281828 ** (delta * 0.5))
    else:
        # exact — within 0.5 degrees either way
        prob = 1.0 if abs(delta) <= 0.5 else 0.0

    return round(max(0.0, min(1.0, prob)), 4)


def calculate_edge(market: PolymarketMarket, forecast: ForecastResponse) -> SignalResponse:
    """
    Determine BUY_YES, BUY_NO, or NO_EDGE signal with edge calculation.

    Edge = model_probability - market_implied_probability
    - Positive edge on YES → BUY_YES
    - Negative edge on YES (positive edge on NO) → BUY_NO
    - |edge| < MIN_EDGE → NO_EDGE
    """
    MIN_EDGE = 0.05  # minimum edge threshold to act

    # Resolve threshold in the city's native unit
    city_unit = CITIES[market.city]["unit"]
    if city_unit == "fahrenheit":
        threshold = market.threshold_fahrenheit or (
            (market.threshold_celsius * 9 / 5 + 32)
            if market.threshold_celsius is not None
            else None
        )
    else:
        threshold = market.threshold_celsius or (
            ((market.threshold_fahrenheit - 32) * 5 / 9)
            if market.threshold_fahrenheit is not None
            else None
        )

    if threshold is None:
        raise ValueError("Market must provide threshold_celsius or threshold_fahrenheit")

    # Market-implied YES probability (first outcome is YES)
    market_yes_prob = market.outcomes[0].probability

    # Model probability
    model_yes_prob = _model_yes_probability(
        forecast_temp=forecast.temperature,
        threshold=threshold,
        condition=market.condition,
    )

    edge = round(model_yes_prob - market_yes_prob, 4)

    if abs(edge) < MIN_EDGE:
        signal = "NO_EDGE"
        confidence = 0.0
    elif edge > 0:
        signal = "BUY_YES"
        confidence = round(abs(edge), 4)
    else:
        signal = "BUY_NO"
        confidence = round(abs(edge), 4)

    logger.info(
        "signal — market=%s city=%s threshold=%.1f forecast=%.1f market_p=%.3f model_p=%.3f edge=%.3f signal=%s",
        market.id, market.city, threshold, forecast.temperature,
        market_yes_prob, model_yes_prob, edge, signal,
    )

    return SignalResponse(
        market_id=market.id,
        signal=signal,
        confidence=confidence,
        edge=edge,
        forecast=forecast,
        edge_breakdown=EdgeBreakdown(
            market_yes_prob=market_yes_prob,
            model_yes_prob=model_yes_prob,
            edge=edge,
            threshold_temperature=round(threshold, 2),
            forecast_temperature=forecast.temperature,
        ),
        processing_time_ms=0.0,  # filled in by route handler
    )


# ---------------------------------------------------------------------------
# x402 payment middleware
# ---------------------------------------------------------------------------

X402_RESPONSE = {
    "x402Version": 1,
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "maxAmountRequired": SIGNAL_PRICE_UNITS,
            "resource": RESOURCE_URL,
            "description": "Weather-based Polymarket signal ($0.05 USDC)",
            "mimeType": "application/json",
            "payTo": WALLET_ADDRESS,
            "maxTimeoutSeconds": 300,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "extra": {"name": "USDC", "decimals": 6},
        }
    ],
}


async def payment_middleware(request: Request, call_next):
    """
    Gate /api/signal behind x402 payment check.
    /api/forecast is free (read-only weather data, no signal logic).
    /health is always free.
    """
    if PAYMENT_REQUIRED and request.url.path == "/api/signal":
        payment_header = request.headers.get("X-Payment")
        if not payment_header:
            logger.info("402 — missing X-Payment header")
            return JSONResponse(status_code=402, content=X402_RESPONSE)
    return await call_next(request)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Weather Signal API",
    description=(
        "Bias-corrected temperature forecasts and x402-gated Polymarket BUY/SELL signals. "
        "Powered by Open-Meteo."
    ),
    version=VERSION,
)

app.middleware("http")(payment_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION, "cities": list(CITIES.keys())}


@app.get("/api/forecast", response_model=ForecastResponse)
async def forecast(
    city: str = Query(..., description="City name (e.g. London, Seoul)"),
    date: str = Query(..., description="Forecast date in YYYY-MM-DD format"),
):
    """
    Return the bias-corrected maximum temperature for a city on a given date.
    This endpoint is **free** — no payment required.
    """
    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            status_code=422,
            content={"detail": "date must be in YYYY-MM-DD format"},
        )

    if city not in CITIES:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"Unknown city '{city}'",
                "supported_cities": list(CITIES.keys()),
            },
        )

    try:
        result = await get_forecast(city, date)
        return result
    except httpx.HTTPStatusError as exc:
        logger.error("Open-Meteo error: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"detail": "Upstream forecast service error", "upstream_status": exc.response.status_code},
        )
    except Exception as exc:
        logger.error("Forecast error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.post("/api/signal", response_model=SignalResponse)
async def signal(body: SignalRequest):
    """
    Generate a BUY/SELL signal for a Polymarket weather market.

    **Requires x402 payment** — $0.05 USDC on Base mainnet.
    Returns BUY_YES, BUY_NO, or NO_EDGE with edge breakdown.
    """
    start = time.time()

    market = body.market

    # Validate city
    if market.city not in CITIES:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"Unknown city '{market.city}'",
                "supported_cities": list(CITIES.keys()),
            },
        )

    # Validate date
    try:
        datetime.strptime(market.end_date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            status_code=422,
            content={"detail": "end_date must be in YYYY-MM-DD format"},
        )

    try:
        fc = await get_forecast(market.city, market.end_date)
        result = calculate_edge(market, fc)
        result.processing_time_ms = round((time.time() - start) * 1000, 2)
        return result
    except httpx.HTTPStatusError as exc:
        logger.error("Open-Meteo error: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"detail": "Upstream forecast service error"},
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except Exception as exc:
        logger.error("Signal error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})
