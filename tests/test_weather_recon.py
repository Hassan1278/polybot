"""Tests for the pure classifiers in scripts/weather_recon.py — the part that's
bug-prone (word-boundary matching so 'ukraine' isn't read as 'rain'). The gamma I/O is
integration-level and run on the VPS.
"""

from __future__ import annotations

from scripts.weather_recon import is_weather, location, wtype


def test_is_weather_detects_real_weather_questions():
    assert is_weather("Will NYC high temperature exceed 90°F on June 30?")
    assert is_weather("Highest temperature in London this week?")
    assert is_weather("Will it snow in Chicago before December 25?")
    assert is_weather("Total rainfall in Miami in July?")


def test_is_weather_rejects_lookalikes():
    # word boundaries: these contain 'rain'/'snow'/'heat' as substrings but aren't weather
    assert not is_weather("Will Ukraine and Russia sign a ceasefire?")   # 'ukRAINe'
    assert not is_weather("Will the training run finish on time?")        # 'tRAINing'
    assert not is_weather("Who wins the heated mayoral race?")            # 'HEATed' (no \\bheat\\b)
    assert not is_weather("Bitcoin above $100k by year end?")


def test_wtype_classifies():
    assert wtype("Will NYC high temp hit 95 degrees?") == "temperature"
    assert wtype("Total rainfall in Seattle in March?") == "precip"
    assert wtype("Will Boston get 12 inches of snowfall?") == "snow"
    assert wtype("Will a hurricane make landfall in Florida?") == "storm"
    assert wtype("Will the event happen on Tuesday?") == "other"


def test_location_extracts_known_cities():
    assert location("Highest temperature in New York on Friday?") == "new york"
    assert location("Will it rain in London tomorrow?") == "london"
    assert location("Will the global average rise?") == "?"
