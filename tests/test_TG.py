# tests/test_TG_bot.py
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
import math
import pandas as pd
import pytest
from TG_bot import (
    standardize_columns,
    parse_dates_inplace,
    add_derived_metrics_inplace,
    build_daily_cost_series,
    forecast_cost_7d,
    make_quick_summary,
    _SKLEARN_OK,
)
# ---------- Вспомогательные фабрики ----------
def make_sample_df():
    return pd.DataFrame(
        {
            "Дата": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "Канал": ["Meta", "Google", "Meta"],
            "Показы": [1000, 2000, 1500],
            "Клики": [100, 150, 120],
            "Конверсии": [5, 3, 4],
            "Расход": [1000.0, 1500.0, 900.0],
            "Выручка": [2000.0, 2200.0, 1800.0],
        }
    )
# ---------- Тесты на маппинг колонок ----------
def test_standardize_columns_basic():
    df = make_sample_df()
    df_std, mapping = standardize_columns(df.copy())
    assert "date" in df_std.columns
    assert "channel" in df_std.columns
    assert "cost" in df_std.columns
    assert mapping["date"] == "Дата"
    assert mapping["channel"] == "Канал"
    assert mapping["cost"] == "Расход"
def test_parse_dates_inplace():
    df = make_sample_df()
    df_std, _ = standardize_columns(df)
    parse_dates_inplace(df_std)
    assert "date" in df_std.columns
    assert pd.api.types.is_datetime64_any_dtype(df_std["date"])
    assert df_std["date"].isna().sum() == 0
def test_add_derived_metrics_inplace():
    df = make_sample_df()
    df_std, _ = standardize_columns(df)
    parse_dates_inplace(df_std)
    add_derived_metrics_inplace(df_std)
    assert "ctr" in df_std.columns
    assert "cpc" in df_std.columns
    assert "cpa" in df_std.columns
    assert "roas" in df_std.columns
    total_impr = df_std["impressions"].sum()
    total_clicks = df_std["clicks"].sum()
    total_cost = df_std["cost"].sum()
    total_conv = df_std["conversions"].sum()
    total_rev = df_std["revenue"].sum()
    # Проверяем что агрегированные метрики разумные
    mean_ctr = df_std["ctr"].mean()
    assert 0 <= mean_ctr <= 1
    # CPC > 0
    assert (df_std["cpc"] > 0).all()
    # ROAS > 0
    assert (df_std["roas"] > 0).all()
# ---------- Тесты агрегатов и прогноза ----------
def test_build_daily_cost_series():
    df = make_sample_df()
    df_std, _ = standardize_columns(df)
    parse_dates_inplace(df_std)
    daily = build_daily_cost_series(df_std)
    assert list(daily.columns) == ["date", "cost"]
    # Должно быть 2 уникальных даты
    assert len(daily) == 2
    assert daily["date"].is_monotonic_increasing
@pytest.mark.skipif(not _SKLEARN_OK, reason="scikit-learn is not installed")
def test_forecast_cost_7d():
    # Генерируем 10 дней с растущими расходами
    dates = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame(
        {
            "date": dates,
            "cost": [100 + i * 10 for i in range(10)],
        }
    )
    fc_df, img, meta = forecast_cost_7d(df)
    # Исторических точек 10, +7 дней прогноза
    assert len(fc_df) == 17
    assert "r2_train" in meta
    assert meta["n_train_days"] == 10.0
    # R^2 должен быть близок к 1 на идеально линейных данных
    assert meta["r2_train"] > 0.9
def test_make_quick_summary_contains_key_metrics():
    df = make_sample_df()
    df_std, _ = standardize_columns(df)
    parse_dates_inplace(df_std)
    add_derived_metrics_inplace(df_std)
    summary = make_quick_summary(df_std)
    assert "Общий расход" in summary
    assert "ROAS" in summary or "Выручка" in summary
    assert "CTR" in summary
    assert "CPC" in summary
    assert "CPA" in summary