from mongoose.services.weather import build_consensus, make_voice_summary, ProviderResult


def test_weather_summary_is_shortish_and_contains_data():
    payload = {
        "current": {
            "temperature_2m": 22,
            "apparent_temperature": 22,
            "precipitation": 0,
            "rain": 0,
            "showers": 0,
            "weather_code": 0,
            "wind_speed_10m": 5,
        },
        "hourly": {
            "time": ["2026-06-13T12:00", "2026-06-13T13:00"],
            "temperature_2m": [22, 23],
            "apparent_temperature": [22, 23],
            "precipitation": [0, 0],
            "precipitation_probability": [0, 0],
            "uv_index": [5, 6],
            "wind_speed_10m": [5, 6],
            "wind_gusts_10m": [10, 11],
            "weather_code": [0, 0],
        },
    }
    consensus = build_consensus([ProviderResult("open_meteo", payload)])
    assert "voice" in consensus
    assert "22 degrees" in consensus["voice"]
    assert len(consensus["voice"]) < 180


def test_make_voice_summary_does_not_fake_minutely_precision():
    summary = make_voice_summary({
        "current": {"temperature_c": 12, "feels_like_c": 12, "condition": "rain"},
        "rain": {"next_expected": "2026-06-13T15:00:00+01:00", "minute_precision": False},
        "uv": {"peak": 2, "peak_time": "2026-06-13T13:00:00+01:00"},
    })
    assert "rain risk" in summary


def test_no_sources_no_data_means_no_forecast():
    """The external audit probed voice-summary with zero providers and empty data, and got
    'probably dry': fake confidence from nothing. With no sources AND no current reading the
    summary must say it has no live data, and never guess."""
    summary = make_voice_summary({"current": {}, "rain": {}, "uv": {}, "sources": []})
    assert "don't have live weather data" in summary
    assert "probably dry" not in summary
