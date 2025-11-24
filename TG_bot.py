# TG_bot.py — Телеграм-бот для анализа рекламных расходов (v21+, Python 3.13)
# ==================================================================================
# Что умеет:
# • принимает CSV/XLSX, автоматически распознаёт колонки (рус/англ),
# • считает CTR, CPC, CPA, ROAS, делает краткую сводку,
# • показывает кнопки: "Расходы по датам", "ROAS по каналам", агрегаты CSV по каналам/кампаниям/датам,
#  • строит простой прогноз расходов на 7 дней вперёд (LinearRegression).
# Установка (из терминала VS Code в активной .venv): pip install -U "python-telegram-bot>=21.7" pandas matplotlib openpyxl scikit-learn
# Запуск:TELEGRAM_BOT_TOKEN=<токен> python TG_bot.py или положите токен в файл bot_token.txt рядом со скриптом
# ==================================================================================

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime
from datetime import timedelta
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple

# проверка распространенных ошибок загрузки библиотек 
try:
    import pandas as pd
except Exception as e:
    raise SystemExit("Не установлен pandas. Выполните: pip install -U pandas") from e
try:
    import matplotlib.pyplot as plt
except Exception as e:
    raise SystemExit("Не установлен matplotlib. Выполните: pip install -U matplotlib") from e

# scikit-learn для прогноза (если нет — прогноз отключится)
try:
    from sklearn.linear_model import LinearRegression
    _SKLEARN_OK = True
except Exception:
    _SKLEARN_OK = False

# python-telegram-bot v21+
try:
    from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except Exception as e:
    raise SystemExit(
        "Не установлен или слишком старый python-telegram-bot. "
        "Установите: pip install -U \"python-telegram-bot>=21.7\""
    ) from e

# Конфигурация логов
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("adsbot")

# Чтение токена
# ----------------------------------------------------------------------------------
def _read_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()
    path = os.path.join(os.getcwd(), "bot_token.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    raise RuntimeError("Укажи токен в TELEGRAM_BOT_TOKEN или в файле bot_token.txt")

# Сопоставление самых распрострараненных наименований колонок (рус/англ) и расчёт метрик
# ----------------------------------------------------------------------------------
COL_MAP: Dict[str, List[str]] = {
    "date": ["date", "дата", "day", "report_date", "Дата"],
    "channel": ["channel", "канал", "source", "источник", "medium", "источник/канал", "utm_source", "utm_medium"],
    "campaign": ["campaign", "кампания", "utm_campaign", "adset", "ad_group"],
    "impressions": ["impressions", "показы", "views"],
    "clicks": ["clicks", "клики", "click"],
    "conversions": ["conversions", "конверсии", "orders", "sales", "purchases"],
    "cost": ["cost", "расход", "spend", "затраты"],
    "revenue": ["revenue", "доход", "выручка", "revenue_amount", "sales", "Sales ($)"],
}

SUPPORTED_EXT = {".csv", ".xlsx", ".xls"}


def _norm_col(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip()).lower()


def _find_col(df: pd.DataFrame, keys: Iterable[str]) -> Optional[str]:
    cols_norm = {i: _norm_col(i) for i in df.columns}
    keys_norm = [_norm_col(k) for k in keys]
    # точное совпадение
    for raw, normed in cols_norm.items():
        if normed in keys_norm:
            return raw
    # частичное совпадение (например, "источник/канал")
    for raw, normed in cols_norm.items():
        for k in keys_norm:
            if k in normed:
                return raw
    return None


def standardize_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
    mapping: Dict[str, Optional[str]] = {}
    for canonical, variants in COL_MAP.items():
        col = _find_col(df, variants)
        mapping[canonical] = col
    rename_dict = {mapping[k]: k for k in mapping if mapping[k] is not None}
    df = df.rename(columns=rename_dict)
    return df, mapping


def parse_dates_inplace(df: pd.DataFrame) -> None:
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")


def add_derived_metrics_inplace(df: pd.DataFrame) -> None:
    # Не бросаем исключения, просто добавляем что можем
    if {"impressions", "clicks"}.issubset(df.columns):
        df["ctr"] = (df["clicks"] / df["impressions"]).replace([pd.NA, pd.NaT], 0.0)
    if {"cost", "clicks"}.issubset(df.columns):
        df["cpc"] = (df["cost"] / df["clicks"]).replace([pd.NA, pd.NaT], None)
    if {"cost", "conversions"}.issubset(df.columns):
        df["cpa"] = (df["cost"] / df["conversions"]).replace([pd.NA, pd.NaT], None)
    if {"revenue", "cost"}.issubset(df.columns):
        df["roas"] = (df["revenue"] / df["cost"]).replace([pd.NA, pd.NaT], None)


def make_quick_summary(df: pd.DataFrame) -> str:
    parts: List[str] = []
    if "cost" in df.columns:
        parts.append(f"• Общий расход: <b>{float(df['cost'].sum()):,.2f}</b>")
    if {"revenue", "cost"}.issubset(df.columns):
        total_rev = float(df["revenue"].sum())
        roas = total_rev / df["cost"].sum() if df["cost"].sum() else float("nan")
        parts.append(f"• Выручка: <b>{total_rev:,.2f}</b>; ROAS: <b>{roas:.2f}</b>")
    if {"impressions", "clicks"}.issubset(df.columns):
        ctr = df["clicks"].sum() / df["impressions"].sum() if df["impressions"].sum() else float("nan")
        parts.append(f"• CTR: <b>{ctr*100:.2f}%</b>")
    if {"clicks", "cost"}.issubset(df.columns):
        cpc = df["cost"].sum() / df["clicks"].sum() if df["clicks"].sum() else float("nan")
        parts.append(f"• CPC: <b>{cpc:.2f}</b>")
    if {"conversions", "cost"}.issubset(df.columns):
        cpa = df["cost"].sum() / df["conversions"].sum() if df["conversions"].sum() else float("nan")
        parts.append(f"• CPA: <b>{cpa:.2f}</b>")
    if {"channel", "cost"}.issubset(df.columns):
        top_ch = df.groupby("channel")["cost"].sum().sort_values(ascending=False).head(5)
        parts.append("• Топ-5 каналов по расходу: " + ", ".join([f"{k}: {v:,.0f}" for k, v in top_ch.items()]))
    return "\n".join(parts) if parts else "Не нашёл нужных колонок."


# ----------------------------------------------------------------------------------
# Вспомогательное чтение файлов и построение графиков
# ----------------------------------------------------------------------------------
def _read_table(local_path: str, ext: str) -> pd.DataFrame:
    if ext == ".csv":
        try:
            return pd.read_csv(local_path)
        except Exception:
            return pd.read_csv(local_path, sep=";")
    else:
        return pd.read_excel(local_path)


def _bytes_plot(fig) -> BytesIO:
    """Упаковать matplotlib-figure в BytesIO (PNG) для отправки в Telegram."""
    bio = BytesIO()
    fig.savefig(bio, format="png")
    plt.close(fig)
    bio.seek(0)
    return bio


def plot_cost_by_date(df: pd.DataFrame) -> BytesIO:
    need = {"date", "cost"}
    if not need.issubset(df.columns):
        raise ValueError(f"Для графика нужны колонки: {need}")
    tmp = df.dropna(subset=["date"]).groupby("date", as_index=False)["cost"].sum().sort_values("date")
    fig = plt.figure(figsize=(9, 4.5))
    plt.plot(tmp["date"], tmp["cost"])
    plt.title("Расходы по датам")
    plt.xlabel("Дата")
    plt.ylabel("Расход")
    plt.tight_layout()
    return _bytes_plot(fig)


def plot_roas_by_channel(df: pd.DataFrame) -> BytesIO:
    need = {"channel", "revenue", "cost"}
    if not need.issubset(df.columns):
        raise ValueError(f"Для графика нужны колонки: {need}")
    tmp = df.groupby("channel", as_index=False).agg(revenue=("revenue", "sum"), cost=("cost", "sum"))
    tmp["roas"] = tmp["revenue"] / tmp["cost"]
    tmp = tmp.sort_values("roas", ascending=False)
    fig = plt.figure(figsize=(9, 4.5))
    plt.bar(tmp["channel"].astype(str), tmp["roas"].astype(float))
    plt.xticks(rotation=30, ha="right")
    plt.title("ROAS по каналам")
    plt.xlabel("Канал")
    plt.ylabel("ROAS")
    plt.tight_layout()
    return _bytes_plot(fig)
# ==== Экспорт в Excel (мультилистовый отчёт) ====
def _excel_bytes_for_report(df: pd.DataFrame) -> BytesIO:
    """
    Формирует один .xlsx-файл с несколькими листами:
      - summary (общие метрики)
      - by_channel (агрегат по каналам)
      - by_campaign (агрегат по кампаниям, если есть колонка campaign)
      - by_date (агрегат по датам)
      - raw_sample (первые строки исходных данных)
    Возвращает BytesIO для отправки в Telegram как документ.
    """
    # Агрегаты
    by_channel = None
    if "channel" in df.columns:
        by_channel = df.groupby("channel").agg(
            cost=("cost", "sum"),
            revenue=("revenue", "sum"),
            impressions=("impressions", "sum") if "impressions" in df.columns else ("cost", "count"),
            clicks=("clicks", "sum") if "clicks" in df.columns else ("cost", "count"),
            conv=("conversions", "sum") if "conversions" in df.columns else ("cost", "count"),
        ).reset_index()
        if {"revenue","cost"}.issubset(by_channel.columns):
            by_channel["roas"] = by_channel["revenue"] / by_channel["cost"]
        if {"clicks","cost"}.issubset(by_channel.columns):
            by_channel["cpc"] = by_channel["cost"] / by_channel["clicks"].replace({0: pd.NA})
        if {"conv","cost"}.issubset(by_channel.columns):
            by_channel["cpa"] = by_channel["cost"] / by_channel["conv"].replace({0: pd.NA})
        if {"impressions","clicks"}.issubset(by_channel.columns):
            by_channel["ctr"] = by_channel["clicks"] / by_channel["impressions"].replace({0: pd.NA})

    by_campaign = None
    if "campaign" in df.columns:
        by_campaign = df.groupby("campaign").agg(
            cost=("cost", "sum"),
            revenue=("revenue", "sum"),
            impressions=("impressions", "sum") if "impressions" in df.columns else ("cost", "count"),
            clicks=("clicks", "sum") if "clicks" in df.columns else ("cost", "count"),
            conv=("conversions", "sum") if "conversions" in df.columns else ("cost", "count"),
        ).reset_index()
        if {"revenue","cost"}.issubset(by_campaign.columns):
            by_campaign["roas"] = by_campaign["revenue"] / by_campaign["cost"]
        if {"clicks","cost"}.issubset(by_campaign.columns):
            by_campaign["cpc"] = by_campaign["cost"] / by_campaign["clicks"].replace({0: pd.NA})
        if {"conv","cost"}.issubset(by_campaign.columns):
            by_campaign["cpa"] = by_campaign["cost"] / by_campaign["conv"].replace({0: pd.NA})
        if {"impressions","clicks"}.issubset(by_campaign.columns):
            by_campaign["ctr"] = by_campaign["clicks"] / by_campaign["impressions"].replace({0: pd.NA})

    by_date = None
    if "date" in df.columns:
        by_date = df.groupby("date").agg(
            cost=("cost", "sum"),
            revenue=("revenue", "sum"),
            impressions=("impressions", "sum") if "impressions" in df.columns else ("cost", "count"),
            clicks=("clicks", "sum") if "clicks" in df.columns else ("cost", "count"),
            conv=("conversions", "sum") if "conversions" in df.columns else ("cost", "count"),
        ).reset_index().sort_values("date")
        if {"revenue","cost"}.issubset(by_date.columns):
            by_date["roas"] = by_date["revenue"] / by_date["cost"]
        if {"clicks","cost"}.issubset(by_date.columns):
            by_date["cpc"] = by_date["cost"] / by_date["clicks"].replace({0: pd.NA})
        if {"conv","cost"}.issubset(by_date.columns):
            by_date["cpa"] = by_date["cost"] / by_date["conv"].replace({0: pd.NA})
        if {"impressions","clicks"}.issubset(by_date.columns):
            by_date["ctr"] = by_date["clicks"] / by_date["impressions"].replace({0: pd.NA})

    # Пишем в Excel (engine=openpyxl)
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        # summary
        summary_rows = []
        if "cost" in df.columns:
            summary_rows.append(["total_cost", float(df["cost"].sum())])
        if {"revenue","cost"}.issubset(df.columns):
            total_rev = float(df["revenue"].sum())
            total_cost = float(df["cost"].sum())
            roas = (total_rev / total_cost) if total_cost else float("nan")
            summary_rows += [["total_revenue", total_rev], ["total_roas", roas]]
        if {"impressions","clicks"}.issubset(df.columns):
            s_imp, s_clk = float(df["impressions"].sum()), float(df["clicks"].sum())
            ctr = (s_clk / s_imp) if s_imp else float("nan")
            summary_rows.append(["ctr", ctr])
        if {"clicks","cost"}.issubset(df.columns):
            s_clk = float(df["clicks"].sum())
            cpc = (float(df["cost"].sum()) / s_clk) if s_clk else float("nan")
            summary_rows.append(["cpc", cpc])
        if {"conversions","cost"}.issubset(df.columns):
            s_conv = float(df["conversions"].sum())
            cpa = (float(df["cost"].sum()) / s_conv) if s_conv else float("nan")
            summary_rows.append(["cpa", cpa])

        pd.DataFrame(summary_rows, columns=["metric", "value"]).to_excel(writer, sheet_name="summary", index=False)

        if by_channel is not None:
            by_channel.to_excel(writer, sheet_name="by_channel", index=False)
        if by_campaign is not None:
            by_campaign.to_excel(writer, sheet_name="by_campaign", index=False)
        if by_date is not None:
            by_date.to_excel(writer, sheet_name="by_date", index=False)

        # небольшой сэмпл исходника
        df.head(2000).to_excel(writer, sheet_name="raw_sample", index=False)

    bio.seek(0)
    return bio
# ----------------------------------------------------------------------------------
# Прогноз расходов на 7 дней (LinearRegression baseline)
# ----------------------------------------------------------------------------------
def build_daily_cost_series(df: pd.DataFrame) -> pd.DataFrame:
    need = {"date", "cost"}
    if not need.issubset(df.columns):
        raise ValueError(f"Для прогноза нужны колонки: {need}")
    daily = (
        df.dropna(subset=["date"])
          .groupby("date", as_index=False)["cost"]
          .sum()
          .sort_values("date")
    )
    return daily


def forecast_cost_7d(df: pd.DataFrame) -> Tuple[pd.DataFrame, BytesIO, Dict[str, float]]:
    if not _SKLEARN_OK:
        raise RuntimeError(
            "scikit-learn не установлен. Установите: pip install -U scikit-learn"
        )
    daily = build_daily_cost_series(df)
    if len(daily) < 7:
        raise ValueError("Недостаточно данных для прогноза (нужно ≥ 7 дней).")

    # Ось времени t = дни с нулевой точки
    t0 = daily["date"].min()
    daily = daily.reset_index(drop=True)
    daily["t"] = (daily["date"] - t0).dt.days
    X = daily[["t"]].values
    y = daily["cost"].values

    model = LinearRegression().fit(X, y)
    y_pred = model.predict(X)
    r2 = float(model.score(X, y))

    # Будущее: 7 дней
    last_date = daily["date"].max()
    future_dates = [last_date + timedelta(days=i) for i in range(1, 8)]
    import numpy as np
    future_t = np.array([(d - t0).days for d in future_dates]).reshape(-1, 1)
    future_pred = model.predict(future_t)

    hist = daily[["date", "cost"]].rename(columns={"cost": "cost_actual"})
    hist["cost_pred"] = y_pred
    fut = pd.DataFrame({"date": future_dates, "cost_actual": pd.NA, "cost_pred": future_pred})
    fc_df = pd.concat([hist, fut], ignore_index=True)

    fig = plt.figure(figsize=(9, 4.5))
    plt.plot(hist["date"], hist["cost_actual"], label="Факт")
    plt.plot(fc_df["date"], fc_df["cost_pred"], linestyle="--", label="Прогноз (7д)")
    plt.title(f"Прогноз расходов (7 дней). R² на обучении = {r2:.3f}")
    plt.xlabel("Дата"); plt.ylabel("Расход")
    plt.xticks(rotation=30, ha="right")
    plt.legend(); plt.tight_layout()
    img = _bytes_plot(fig)

    return fc_df, img, {"r2_train": r2, "n_train_days": float(len(daily))}

# Telegram-хендлеры
# ----------------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для анализа расходов на рекламу.Пришли CSV/XLSX с колонками: дата/канал/кампания/показы/клики/конверсии/расход/выручка. "
        "Я посчитаю метрики и покажу графики."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "1) Отправьте .csv или .xlsx (как ФАЙЛ, без сжатия)\n"
        "2) Получите сводку и кнопки: графики, агрегаты, прогноз."
    )


async def _download_to_temp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, str]:
    """Скачать присланный документ в temp-папку и вернуть (local_path, ext)."""
    doc = update.message.document
    if doc is None:
        raise ValueError("Пришлите файл .csv или .xlsx")
    filename = doc.file_name or "file"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError("Поддерживаются только .csv, .xlsx, .xls")
    tgfile = await context.bot.get_file(doc.file_id)
    tmpdir = tempfile.mkdtemp(prefix="adspend_")
    local_path = os.path.join(tmpdir, filename)
    await tgfile.download_to_drive(local_path)
    return local_path, ext
def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("График расходов по датам", callback_data="plot_cost")],
            [InlineKeyboardButton("ROAS по каналам", callback_data="plot_roas")],
            [InlineKeyboardButton("📑 Экспорт в Excel", callback_data="export_excel")],
            [InlineKeyboardButton("Прогноз расходов на 7 дней", callback_data="forecast_cost")],  # Новая кнопка!
        ]
    )
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приём файла → разбор → сводка → кнопки."""
    try:
        # мгновенное подтверждение для UX
        if update.message and update.message.document:
            await update.message.reply_text(
                f"Файл получен: {update.message.document.file_name}. Обрабатываю…"
            )

        local_path, ext = await _download_to_temp(update, context)
        df = _read_table(local_path, ext)
        if df.empty:
            await update.message.reply_text("Файл прочитан, но данных не найдено.")
            return

        df, _ = standardize_columns(df)
        parse_dates_inplace(df)
        add_derived_metrics_inplace(df)
        context.user_data["ad_df"] = df

        await update.message.reply_text(
            make_quick_summary(df),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(),
        )
    except Exception as e:
        logger.exception("on_file failed")
        await update.message.reply_text(f"Ошибка: {e}")
async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    df: Optional[pd.DataFrame] = context.user_data.get("ad_df")
    if df is None:
        await query.edit_message_text("Сначала пришли файл.")
        return
    try:
        if query.data == "plot_cost":
            await query.message.reply_photo(
                photo=plot_cost_by_date(df),
                caption="Расходы по датам",
            )
        elif query.data == "plot_roas":
            await query.message.reply_photo(
                photo=plot_roas_by_channel(df),
                caption="ROAS по каналам",
            )
        elif query.data == "export_excel":
            xls = _excel_bytes_for_report(df)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            await query.message.reply_document(
                document=xls,
                filename=f"ad_report_{ts}.xlsx",
                caption="Экспорт отчётов в Excel (несколько вкладок)",
            )
        elif query.data == "forecast_cost":
            # если scikit-learn не установлен — честно говорим
            if not _SKLEARN_OK:
                await query.message.reply_text(
                    "Прогноз недоступен: не установлен scikit-learn.\n"
                    "Установите: pip install -U scikit-learn"
                )
                return
            try:
                fc_df, img, meta = forecast_cost_7d(df)
                r2 = meta.get("r2_train", float("nan"))
                n_days = int(meta.get("n_train_days", 0))

                caption = (
                    "Прогноз расходов на 7 дней.\n"
                    f"R² на обучении: {r2:.3f}\n"
                    f"Число дней в обучении: {n_days}"
                )
                await query.message.reply_photo(
                    photo=img,
                    caption=caption,
                )
            except Exception as e:
                await query.message.reply_text(f"Ошибка прогноза: {e}")
                return
    except Exception as e:
        logger.exception("on_buttons failed")
        await query.message.reply_text(
            f"Ошибка построения графика/отчёта: {e}"
        )
# Точка вход
def main() -> None:
    # подробный лог PTB (помогает при отладке приёма документов)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)

    app = Application.builder().token(_read_token()).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    # ВАЖНО: некоторые клиенты присылают файлы как ATTACHMENT; берём оба:
    app.add_handler(MessageHandler(filters.Document.ALL | filters.ATTACHMENT, on_file))
    # Кнопки
    app.add_handler(CallbackQueryHandler(on_buttons))
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling()  # v21+: синхронный, сам управляет циклом
if __name__ == "__main__":
    main()