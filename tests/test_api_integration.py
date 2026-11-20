
import time
from fastapi.testclient import TestClient
import main
from storage import initialize_database, register_data_source
from ingestion import ingest_candles


def test_api_ingestion_storage_and_analysis(tmp_path, monkeypatch):
    db_path = tmp_path / "integration.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DATA_SOURCE_NAME", "market_data_provider")
    monkeypatch.setattr(main, "API_TOKEN", "integration-token")

    initialize_database(db_path)
    source_id = register_data_source("market_data_provider", db_path=db_path)

    now = int(time.time())
    aligned = (now // 900) * 900
    candles = []

    for index in range(110):
        timestamp = aligned - (110 - index) * 900
        close = 100.0 + index * 0.2
        candles.append({
            "symbol": "BTC/USDT",
            "timeframe": "M15",
            "timestamp": timestamp,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": None,
        })

    ingest_candles(source_id, candles, now=now, db_path=db_path)

    client = TestClient(main.app)
    headers = {"Authorization": "Bearer integration-token"}

    response = client.post(
        "/api/v1/market-data/analyze",
        json={"symbol": "BTC/USDT", "timeframe": "M15", "limit": 100},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["analysis"]["candles_used"] == 100
    assert response.json()["trading_execution"] == "disabled"

    response = client.post(
        "/api/v1/market-data/candle",
        json=candles[-1],
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["candle_stored"] is True
    assert response.json()["storage"]["duplicates"] == 1
    assert response.json()["source_name"] == "market_data_provider"

    response = client.post(
        "/api/v1/market-data/analyze",
        json={"symbol": "BTC/USDT"},
    )
    assert response.status_code == 401
