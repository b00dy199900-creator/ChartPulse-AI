        raise HTTPException(status_code=400, detail="timestamp must be a positive Unix timestamp")
    if candle_time > now:
        raise HTTPException(status_code=400, detail="timestamp cannot be in the future")
    if now - candle_time > 86400:
        raise HTTPException(status_code=400, detail="timestamp must be within the last 24 hours")
