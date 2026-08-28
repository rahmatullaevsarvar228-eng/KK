# -*- coding: utf-8 -*-
"""
КК-система (контроль качества) — Uzum/Kapitalbank, сентябрьская волна
Автоматическая проверка анкет по требованиям заказчика + отправка брака в Telegram
"""

import io
import json
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import streamlit as st

# =========================================================================
# НАСТРОЙКИ TELEGRAM
# =========================================================================
DEFAULT_TG_TOKEN = "8221711611:AAE6Mnx40RAD9Qbzk9-_dytJJQiaoR-RmPE"
DEFAULT_TG_CHAT_ID = "-1003962850696"  # группа "📢 UZUM | ИНТЕРВЬЮЕРЛАР" — видят все участники
TG_MAX_LEN = 3800  # запас от лимита Telegram в 4096 символов

# Реестр личных подписчиков бота: кто написал боту /start в личку — только
# им бот вправе слать личные сообщения (правило самого Telegram; состоять в
# общей группе с ботом для этого НЕ достаточно). Реестр ведёт kk_bot.py —
# отдельный постоянно работающий процесс (см. его докстринг); это приложение
# только читает готовый файл, не опрашивая Telegram само — иначе два процесса
# начнут конкурировать за одни и те же обновления getUpdates.
SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "kk_subscribers.json"


BRAND_COLS = [
    "Agro Bank", "NBU", "TBC Bank", "Kapitalbank", "Payme", "Anor Bank",
    "Hamkor", "Click", "Xalq Bank (Xazna)", "Paynet", "Uzum Bank", "Ipak Yo‘li",
]

# =========================================================================
# СТРУКТУРА АНКЕТЫ "Marketing research wave 2" (Top of Mind + аидед-список).
# Другая волна — другая анкета, поэтому все константы и хелперы ниже под неё.
# =========================================================================

# Kobo-выгрузка может кодировать "известен банк" по-разному в зависимости от
# настроек экспорта: числовым кодом (1 = да) в "сыром" экспорте или текстовой
# меткой ("Знаю") в экспорте с человекочитаемыми labels. Понимаем оба варианта.
def _is_known_value(v):
    if pd.isna(v):
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("знаю", "yes", "да", "bilaman")
    try:
        return float(v) == 1
    except (TypeError, ValueError):
        return False


# Спонтанные (Top-of-Mind, БЕЗ подсказки) блоки: вопрос-якорь по тексту,
# сколько колонок в блоке (сам вопрос + пробы "А ещё?"), и минимум ответов,
# который должен набраться у респондента, если он вообще начал отвечать —
# интервьюер обязан спрашивать "А ещё?" нужное число раз по ТЗ заказчика.
TOM_BLOCKS = [
    dict(key="tom_banks", anchor="первым приходит", width=7, min_n=4,
         label_ru="Банки (в.1–2.7)", label_uz="Банклар (1–2.7-сав.)"),
    dict(key="tom_apps", anchor="приложение для оплаты", width=6, min_n=4,
         label_ru="Приложения (в.5–6.6)", label_uz="Иловалар (5–6.6-сав.)"),
    dict(key="tom_credit", anchor="взять кредит или микрозайм", width=5, min_n=3,
         label_ru="Кредит (в.7–7.5)", label_uz="Кредит (7–7.5-сав.)"),
    dict(key="tom_deposit", anchor="открыть вклад или депозит", width=5, min_n=3,
         label_ru="Вклад (в.8–8.5)", label_uz="Омонат (8–8.5-сав.)"),
    dict(key="tom_ads", anchor="рекламу каких банков", width=5, min_n=3,
         label_ru="Реклама (в.9–9.5)", label_uz="Реклама (9–9.5-сав.)"),
    dict(key="tom_cards", anchor="карты какого банка", width=5, min_n=2,
         label_ru="Карты (в.10–10.5)", label_uz="Карталар (10–10.5-сав.)"),
]


def _find_block_cols(raw, anchor, width):
    """Находит первую колонку, чей текст содержит anchor, и берёт её + width-1
    следующих колонок подряд (сам вопрос + пробы 'А ещё?' идут сразу за ним)."""
    for i, c in enumerate(raw.columns):
        if anchor in str(c).lower():
            return list(raw.columns[i:i + width])
    return []


# Ответы вида "999", "не знаю", "bilmayman" — это ответ на открытый вопрос
# без подсказки, но по сути "я не знаю ни одного" — их нельзя засчитывать как
# реально названный банк/приложение, иначе глубина зондажа завышается.
# ВАЖНО: список — точные строки (не подстроки), чтобы не задеть реальные
# бренды вроде "Xazna"/"Gazna" (это настоящий банк, не "не знаю").
INVALID_TOM_ANSWERS = {
    "999", "99", "0",
    "bilmayman", "bilmiyman", "bilmadim",
    "не знаю", "незнаю", "н е знаю", "нет знаю",
    "нет", "yoq", "йук",
}


def _is_valid_tom_answer(v):
    if pd.isna(v):
        return False
    s = str(v).strip().lower()
    if s == "" or s in INVALID_TOM_ANSWERS:
        return False
    return True


def _count_filled(raw, cols):
    """Сколько реально названных банков/приложений/etc. дал респондент в
    блоке — "не знаю"/"999"/подобные заглушки в счёт не идут (см.
    INVALID_TOM_ANSWERS), это не название, а по сути пустой ответ."""
    if not cols:
        return None
    sub = raw[cols]
    filled = sub.apply(lambda col: col.apply(_is_valid_tom_answer))
    return filled.sum(axis=1)


# Аидед-список знания банков (12 банков, "Знаю"/"Не знаю"). У каждого банка
# в выгрузке несколько дублирующихся колонок (рандомизация порядка показа —
# 3-4 варианта на банк), плюс у части банков 4-й вариант назван иначе
# ("Приложение X" вместо просто "X"). Перечислено по фактическому составу
# колонок в этой выгрузке.
BANK_VARIANTS = {
    "xazna": ["XAZNA", "XAZNA.1", "XAZNA.2", "Приложение XAZNA"],
    "payme": ["Payme", "Payme.1", "Payme.2", "Приложение Payme"],
    "tbcbank": ["TBC bank", "TBC bank.1", "TBC bank.2", "TBC bank.3"],
    "agrobank": ["Agrobank", "Agrobank.1", "Agrobank.2", "Agrobank.3"],
    "hamkorbank": ["Hamkor bank", "Hamkor bank.1", "Hamkor bank.2", "Hamkor bank.3"],
    "ipotekabank": ["Ipoteka bank", "Ipoteka bank.1", "Ipoteka bank.2", "Ipoteka bank.3"],
    "paynet": ["Paynet", "Paynet.1", "Paynet.2", "Приложение Paynet"],
    "uzumbank": ["Uzum Bank", "Uzum Bank.1", "Uzum Bank.2", "Uzum Bank.3"],
    "xalqbanki": ["Xalq banki", "Xalq banki.1", "Xalq banki.2", "Xalq banki.3"],
    "nbu": ["NBU / Milliy bank", "NBU / Milliy bank.1", "NBU / Milliy bank.2", "NBU / Milliy bank.3"],
    "anorbank": ["Anor bank", "Anor bank.1", "Anor bank.2", "Anor bank.3"],
    "click": ["Click", "Click.1", "Click.2", "Приложение Click"],
}
BANK_DISPLAY = {
    "xazna": "XAZNA (Halq Bank)", "payme": "Payme", "tbcbank": "TBC Bank",
    "agrobank": "Agrobank", "hamkorbank": "Hamkor Bank", "ipotekabank": "Ipoteka Bank",
    "paynet": "Paynet", "uzumbank": "Uzum Bank", "xalqbanki": "Xalq Banki",
    "nbu": "NBU", "anorbank": "Anor Bank", "click": "Click",
}
# В этой волне Kapitalbank НЕ входит в аидед-список (список выше — это то,
# что реально есть в выгрузке), поэтому его знание можно оценить только по
# спонтанным (Top-of-Mind) упоминаниям — см. _kapital_mentioned ниже.
KAPITAL_PATTERNS = ("kapital", "капитал")


def _coalesce_bank_value(raw, key):
    cols = [c for c in BANK_VARIANTS[key] if c in raw.columns]
    if not cols:
        return pd.Series([None] * len(raw), index=raw.index)
    out = raw[cols[0]].copy()
    for c in cols[1:]:
        out = out.where(out.notna(), raw[c])
    return out


def build_awareness_matrix(raw):
    """Возвращает (known: bool-DataFrame по каждому банку, answered: bool —
    дошёл ли респондент вообще до этого блока)."""
    known = pd.DataFrame(index=raw.index)
    any_val = pd.Series(False, index=raw.index)
    for key in BANK_VARIANTS:
        val = _coalesce_bank_value(raw, key)
        known[key] = val.apply(_is_known_value)
        any_val |= val.notna()
    return known, any_val


def detect_awareness_fatigue(raw, known):
    """Респондент сначала отвечает 'Знаю', а с какого-то момента — только
    'Не знаю' и больше не возвращается к 'Знаю' (упорядочиваем банки по
    тому, в каком порядке их реально показывали — q12_rnd_<банк>, это
    случайное число розыгрыша порядка, назначается ВСЕМ банкам независимо
    от ответа; q12_rank_<банк> для этого не подходит — он заполнен только
    для банков с ответом 'Знаю', то есть сам зависит от результата).
    Флагуем только если есть и минимум 2 'Знаю' в начале, и минимум 2
    'Не знаю' в конце — иначе это может быть просто честный ответ."""
    rnd_cols = {k: f"q12_rnd_{k}" for k in BANK_VARIANTS if f"q12_rnd_{k}" in raw.columns}
    fatigue = pd.Series(False, index=raw.index)
    if len(rnd_cols) < len(BANK_VARIANTS) // 2:
        return fatigue  # колонок порядка показа почти нет в этой выгрузке — блок недоступен

    order = pd.DataFrame({k: pd.to_numeric(raw[c], errors="coerce") for k, c in rnd_cols.items()})
    for idx in raw.index:
        row_order = order.loc[idx].dropna()
        if len(row_order) < 4:
            continue
        ordered_keys = row_order.sort_values().index.tolist()
        seq = [bool(known.at[idx, k]) for k in ordered_keys]
        if all(seq) or not any(seq):
            continue  # все "знаю" или все "не знаю" — не паттерн утомления
        first_false = seq.index(False)
        has_true_after = any(seq[first_false + 1:])
        n_true_before = sum(seq[:first_false])
        n_false_total = seq.count(False)
        if not has_true_after and n_true_before >= 2 and n_false_total >= 2:
            fatigue.at[idx] = True
    return fatigue


def _kapital_mentioned(raw, tom_cols):
    """% упоминаний Kapitalbank среди спонтанных (Top-of-Mind) ответов —
    аидед-варианта для него в этой анкете нет, поэтому считаем по факту
    упоминания слова в свободном тексте блока 1–2.7."""
    if not tom_cols:
        return pd.Series(False, index=raw.index)
    sub = raw[tom_cols].astype(str).apply(lambda col: col.str.lower())
    mask = pd.Series(False, index=raw.index)
    for p in KAPITAL_PATTERNS:
        mask |= sub.apply(lambda col: col.str.contains(p, na=False)).any(axis=1)
    return mask




# =========================================================================
# ЗАГРУЗКА И ПОДГОТОВКА ДАННЫХ
# =========================================================================
st.set_page_config(page_title="Bank bilish", layout="wide", page_icon="✅")


@st.cache_data(show_spinner=False)
def load_data(file_bytes):
    raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name="data")

    df = pd.DataFrame(index=raw.index)
    df["row_id"] = raw.get("_id", raw.index)
    df["deviceid"] = raw.get("deviceid")
    df["start"] = pd.to_datetime(raw.get("start"), errors="coerce", utc=True).dt.tz_localize(None)
    df["end"] = pd.to_datetime(raw.get("end"), errors="coerce", utc=True).dt.tz_localize(None)
    df["city"] = raw.get("Город")
    # Иногда в "Город" попадают числовые/тестовые значения (например, обрывки
    # квотного номера из тестовых отправок формы) — приводим к строке сразу,
    # иначе дальнейшие sort/groupby падают на смеси int и str в одной колонке.
    df["city"] = df["city"].apply(lambda v: str(v).strip() if pd.notna(v) else v)
    df["inter"] = raw.get("Inter")
    # Как и "Город" — иногда в код интервьюера попадают числовые обрывки из
    # тестовых отправок формы (например, "26" вместо "Inter 26"). Приводим
    # к строке сразу, иначе sort/groupby падают на смеси int и str.
    df["inter"] = df["inter"].apply(lambda v: str(v).strip() if pd.notna(v) else v)
    df["gender"] = raw.get("Пол респондента")
    df["age"] = pd.to_numeric(raw.get("Сколько вам полных лет?"), errors="coerce")
    df["phone"] = raw.get("Номер телефона респондента")

    # ВАЖНО: раньше здесь была рассинхронизация индексов между df (после
    # фильтрации + reset_index) и raw (индекс не менялся) — из-за этого все
    # дальнейшие .reindex(df.index) на самом деле подставляли данные НЕ тех
    # строк, если фильтр реально что-то отбрасывал. Держим raw и df
    # синхронизированными по позициям на всём протяжении.
    keep_mask = (df["deviceid"].notna() | df["inter"].notna()) & df["city"].notna()
    df = df[keep_mask].copy()
    raw = raw.loc[df.index].copy()
    df = df.reset_index(drop=True)
    raw = raw.reset_index(drop=True)

    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60

    name_col = "Скажите пожалуйста как вас зовут?"
    phone_col = "Номер телефона респондента"
    reached_end = pd.Series(False, index=raw.index)
    if name_col in raw.columns:
        reached_end |= raw[name_col].notna()
    if phone_col in raw.columns:
        reached_end |= raw[phone_col].notna()
    df["completed"] = reached_end

    # --- Top-of-Mind (спонтанные, без подсказки) блоки ---------------------
    for block in TOM_BLOCKS:
        cols = _find_block_cols(raw, block["anchor"], block["width"])
        cnt = _count_filled(raw, cols)
        df[f"{block['key']}_n"] = cnt if cnt is not None else np.nan

    # % упоминаний Kapitalbank — считаем по спонтанному блоку банков (1–2.7),
    # т.к. отдельного аидед-вопроса про Kapitalbank в этой анкете нет
    tom_bank_cols = _find_block_cols(raw, "первым приходит", TOM_BLOCKS[0]["width"])
    df["kapital_mentioned"] = _kapital_mentioned(raw, tom_bank_cols)

    # --- Аидед-список знания банков (12 банков, "Знаю"/"Не знаю") ----------
    known, awareness_answered = build_awareness_matrix(raw)
    df["awareness_answered"] = awareness_answered
    df["aided_known_count"] = known.sum(axis=1).where(awareness_answered)
    for key in BANK_VARIANTS:
        df[f"know_{key}"] = known[key]
    df["awareness_fatigue"] = detect_awareness_fatigue(raw, known)

    return df


def run_qc(df, min_interval_min, min_duration_min, max_duration_min,
           brand_median_pct, max_share_city_pct):
    df = df.sort_values(["deviceid", "start"]).reset_index(drop=True)
    df["reasons"] = [[] for _ in range(len(df))]       # реальный брак — влияет на is_defect
    df["warnings"] = [[] for _ in range(len(df))]       # предупреждения — НЕ считаются браком

    mask = df["deviceid"].isna() | (df["deviceid"].astype(str).str.strip() == "")
    for i in df[mask].index:
        df.at[i, "reasons"].append("нет deviceid")

    # ПРЕДУПРЕЖДЕНИЕ, не брак: один код интервьюера — с нескольких устройств
    dev_codes = df.dropna(subset=["deviceid"]).groupby("deviceid")["inter"].nunique()
    bad_devices = dev_codes[dev_codes > 1].index.tolist()
    for i in df[df["deviceid"].isin(bad_devices)].index:
        codes = sorted(df.loc[df["deviceid"] == df.at[i, "deviceid"], "inter"].dropna().unique().tolist())
        df.at[i, "warnings"].append(f"1 устройство → {len(codes)} кодов интервьюера ({', '.join(map(str, codes))})")

    # ПРЕДУПРЕЖДЕНИЕ, не брак: одно устройство — с нескольких кодов интервьюера
    code_devices = df.dropna(subset=["deviceid", "inter"]).groupby("inter")["deviceid"].nunique()
    bad_codes = code_devices[code_devices > 1].index.tolist()
    for i in df[df["inter"].isin(bad_codes) & df["deviceid"].notna()].index:
        devices = sorted(df.loc[df["inter"] == df.at[i, "inter"], "deviceid"].dropna().unique().tolist())
        df.at[i, "warnings"].append(f"1 код интервьюера → {len(devices)} устройств ({', '.join(map(str, devices))})")

    long_mask = df["duration_min"] > max_duration_min
    for i in df[long_mask].index:
        df.at[i, "reasons"].append(f"анкета длилась {df.at[i,'duration_min']:.0f} мин (> {max_duration_min})")

    # БРАК: короткая длительность завершённой анкеты
    short_mask = (df["duration_min"] < min_duration_min) & df["duration_min"].notna() & df["completed"]
    for i in df[short_mask].index:
        df.at[i, "reasons"].append(f"полное интервью длилось {df.at[i,'duration_min']:.1f} мин (< {min_duration_min})")

    df["gap_min"] = df.groupby("deviceid")["start"].diff().dt.total_seconds() / 60
    gap_mask = (df["gap_min"] < min_interval_min) & df["gap_min"].notna()
    for i in df[gap_mask].index:
        df.at[i, "reasons"].append(f"интервал с предыдущей анкетой {df.at[i,'gap_min']:.1f} мин (< {min_interval_min})")

    # --- Слишком короткий "отдых" после завершения предыдущей анкеты -------
    # Это НЕ то же самое, что интервал между стартами выше: если предыдущая
    # анкета была длинной, интервал между стартами может выглядеть нормальным,
    # даже если интервьюер начал следующую анкету сразу же, без реального
    # перерыва (найти респондента, дойти до него и т.п.). Считаем отдельно
    # разрыв между КОНЦОМ предыдущей и НАЧАЛОМ следующей.
    df["prev_end"] = df.groupby("deviceid")["end"].shift(1)
    df["gap_after_prev_end_min"] = (df["start"] - df["prev_end"]).dt.total_seconds() / 60
    rest_mask = (df["gap_after_prev_end_min"] < min_interval_min) & df["gap_after_prev_end_min"].notna()
    for i in df[rest_mask].index:
        df.at[i, "reasons"].append(
            f"начал следующую анкету через {df.at[i,'gap_after_prev_end_min']:.1f} мин после завершения "
            f"предыдущей (< {min_interval_min})"
        )

    # --- "Конвейер": слишком много анкет подряд за короткое окно -----------
    # У одного и того же устройства/интервьюера: если за MASS_WINDOW_MIN минут
    # набирается MASS_MIN_COUNT+ анкет подряд — похоже на массовое штампование
    # анкет, а не реальные интервью одно за другим.
    MASS_WINDOW_MIN = 10
    MASS_MIN_COUNT = 6
    df["mass_burst_count"] = 0
    for dev, g in df[df["deviceid"].notna()].groupby("deviceid"):
        g = g.sort_values("start")
        starts = g["start"]
        idxs = g.index.to_numpy()
        starts_arr = starts.to_numpy()
        for pos in range(len(g)):
            window_end = starts.iloc[pos] + pd.Timedelta(minutes=MASS_WINDOW_MIN)
            in_window = (starts_arr >= starts_arr[pos]) & (starts_arr <= np.datetime64(window_end))
            cnt = int(in_window.sum())
            if cnt >= MASS_MIN_COUNT:
                for j in idxs[in_window]:
                    df.at[j, "mass_burst_count"] = max(df.at[j, "mass_burst_count"], cnt)
    mass_mask = df["mass_burst_count"] >= MASS_MIN_COUNT
    for i in df[mass_mask].index:
        df.at[i, "reasons"].append(
            f"конвейер: {int(df.at[i,'mass_burst_count'])} анкет за {MASS_WINDOW_MIN} мин у одного устройства "
            f"(≥ {MASS_MIN_COUNT})"
        )

    # --- Глубина зондажа: спонтанные (Top-of-Mind) блоки -------------------
    # Если респондент вообще начал отвечать (count>0), интервьюер обязан
    # спрашивать "А ещё?" минимум нужное число раз по ТЗ; 0 ответов — это
    # легитимный "не знаю ничего", не брак. Все блоки Top-of-Mind (банки,
    # приложения, кредит, вклад, реклама, карты) — ПРЕДУПРЕЖДЕНИЕ, а не брак:
    # это сигнал для ручной проверки зондажа, а не однозначное основание
    # для отбраковки анкеты.
    for block in TOM_BLOCKS:
        col_name = f"{block['key']}_n"
        cnt = df[col_name]
        bad = (cnt > 0) & (cnt < block["min_n"]) & df["completed"]
        for i in df[bad].index:
            df.at[i, "warnings"].append(
                f"{block['label_ru']}: назвал(а) {int(df.at[i, col_name])} (< {block['min_n']})"
            )

    # --- Знание банков: "сначала знаю, потом резко не знаю" ----------------
    fatigue_mask = df["awareness_fatigue"] & df["completed"]
    for i in df[fatigue_mask].index:
        df.at[i, "reasons"].append(
            "знание банков: сначала «знаю», потом резко «не знаю» — похоже на утомление/невнимательность"
        )

    missing_aware = df["completed"] & ~df["awareness_answered"]
    for i in df[missing_aware].index:
        df.at[i, "reasons"].append("завершённое интервью, но блок знания банков не заполнен")

    # --- Номер телефона: предупреждение по региону (не построчный брак) ----
    # Считается ниже, в блоке agg_issues — там же логика "по городам".

    # --- Качество зондажа у интервьюера: по СПОНТАННО названным банкам -----
    # (не по аидед-списку — аидед отражает реальное знание респондента,
    # а не старание интервьюера; спонтанный счёт из блока 1–2.7 — наоборот,
    # напрямую зависит от того, сколько раз интервьюер спросил "А ещё?").
    answered = df[df["completed"] & df["tom_banks_n"].notna()]
    wave_median = answered["tom_banks_n"].median() if len(answered) else np.nan
    inter_avg = answered.groupby("inter")["tom_banks_n"].mean()
    bad_inters_brand = (
        inter_avg[inter_avg < wave_median * (brand_median_pct / 100)].index.tolist()
        if pd.notna(wave_median) else []
    )
    for i in df[df["inter"].isin(bad_inters_brand)].index:
        avg = inter_avg.get(df.at[i, "inter"], np.nan)
        df.at[i, "reasons"].append(
            f"у интервьюера в среднем {avg:.1f} названных банков "
            f"(< {brand_median_pct}% медианы волны {wave_median:.1f})"
        )

    df["is_defect"] = df["reasons"].apply(lambda x: len(x) > 0)
    df["reason_text"] = df["reasons"].apply(lambda x: "; ".join(x))
    df["is_warning"] = df["warnings"].apply(lambda x: len(x) > 0)
    df["warning_text"] = df["warnings"].apply(lambda x: "; ".join(x))

    agg_issues = []
    city_stats = df.groupby("city").agg(anketas=("row_id", "count"))
    for city, row in city_stats.iterrows():
        city_safe = _tg_safe(city)
        n_inters = df[df["city"] == city]["inter"].nunique()
        if n_inters < 2:
            agg_issues.append({"type": "few_interviewers", "city": city_safe, "n_inters": n_inters})
        city_df = df[df["city"] == city]
        counts = city_df["inter"].value_counts()
        total = counts.sum()
        for inter, cnt in counts.items():
            share = cnt / total * 100
            if share > max_share_city_pct:
                agg_issues.append({
                    "type": "high_share", "city": city_safe, "inter": _tg_safe(inter),
                    "share": share, "cnt": int(cnt), "total": int(total), "limit": max_share_city_pct,
                })

        # --- номер телефона: если >20% завершённых анкет без номера в городе
        completed_city = city_df[city_df["completed"]]
        n_completed = len(completed_city)
        if n_completed:
            no_phone = int(completed_city["phone"].isna().sum())
            pct = no_phone / n_completed * 100
            if pct > 20:
                agg_issues.append({
                    "type": "phone_missing", "city": city_safe, "pct": pct,
                    "no_phone": no_phone, "n": n_completed,
                })

    return df, agg_issues, wave_median


def _tg_safe(v):
    """Убирает символы, которые Telegram-Markdown трактует как разметку
    (_ * ` [ ]) — если в названии города/коде интервьюера/device-id случайно
    окажется подчёркивание или звёздочка, сообщение не должно падать с
    ошибкой парсинга разметки."""
    return str(v).replace("_", "").replace("*", "").replace("`", "").replace("[", "").replace("]", "")


def tg_send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15
        )
        return r.ok, r.text
    except Exception as e:
        return False, str(e)


# =========================================================================
# ЛИЧНЫЕ ПОДПИСЧИКИ БОТА (для рассылки каждому интервьюеру в личку)
# =========================================================================
def _load_subscribers():
    if SUBSCRIBERS_FILE.exists():
        try:
            return json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_subscribers(subs):
    try:
        SUBSCRIBERS_FILE.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def broadcast_to_subscribers(token, subscribers, text):
    """Отправляет текст каждому подписчику лично. Возвращает список (chat_id, имя, ok, ответ)."""
    results = []
    for chat_id, info in subscribers.items():
        ok, resp = tg_send(token, chat_id, text)
        name = f"{info.get('first_name','')} {info.get('last_name','')}".strip() or chat_id
        results.append((chat_id, name, ok, resp))
        time.sleep(0.3)
    return results


def chunk_text(lines, max_len=TG_MAX_LEN):
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_len:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


REASON_TAGS_BY_LANG = {
    "ru": [
        ("нет deviceid", "🔌 нет ID"),
        ("1 устройство →", "🔀 общий телефон"),
        ("1 код интервьюера →", "📱 сменил телефон"),
        ("длилась", "⏱ >30 мин"),
        ("длилось", "⏱ <5 мин"),
        ("интервал с предыдущей", "⚡ старт < 2 мин"),
        ("после завершения предыдущей", "😮‍💨 нет отдыха"),
        ("конвейер:", "🏭 конвейер"),
        ("Банки (в.1–2.7)", "🏦 мало банков (ToM)"),
        ("Приложения (в.5–6.6)", "📱 мало приложений (ToM)"),
        ("Кредит (в.7–7.5)", "💳 мало (кредит)"),
        ("Вклад (в.8–8.5)", "💰 мало (вклад)"),
        ("Реклама (в.9–9.5)", "📢 мало (реклама)"),
        ("Карты (в.10–10.5)", "💳 мало (карты)"),
        ("сначала «знаю», потом резко «не знаю»", "😴 утомление (знание банков)"),
        ("блок знания банков не заполнен", "🏦 банки не заполнены"),
        ("в среднем", "📉 мало названо банков"),
    ],
    "uz": [
        ("нет deviceid", "🔌 ID йўқ"),
        ("1 устройство →", "🔀 умумий телефон"),
        ("1 код интервьюера →", "📱 телефон алмашди"),
        ("длилась", "⏱ >30 дақ"),
        ("длилось", "⏱ <5 дақ"),
        ("интервал с предыдущей", "⚡ старт < 2 дақ"),
        ("после завершения предыдущей", "😮‍💨 дам йўқ"),
        ("конвейер:", "🏭 конвейер"),
        ("Банки (в.1–2.7)", "🏦 кам банк (ToM)"),
        ("Приложения (в.5–6.6)", "📱 кам илова (ToM)"),
        ("Кредит (в.7–7.5)", "💳 кам (кредит)"),
        ("Вклад (в.8–8.5)", "💰 кам (омонат)"),
        ("Реклама (в.9–9.5)", "📢 кам (реклама)"),
        ("Карты (в.10–10.5)", "💳 кам (карта)"),
        ("сначала «знаю», потом резко «не знаю»", "😴 чарчаш (банк билими)"),
        ("блок знания банков не заполнен", "🏦 банклар тўлдирилмаган"),
        ("в среднем", "📉 кам номланган банк"),
    ],
}

# Тексты интерфейса отчёта на двух языках. Внутренняя логика (поиск причин в
# REASON_TAGS_BY_LANG, детект брака и т.п.) всегда на русском — переводится
# только то, что реально видит интервьюер.
UI_STRINGS = {
    "ru": {
        "report_header": "📋 КК-отчёт Bank bilish",
        "city_report_header": "📋 КК-отчёт — {city}",
        "total_line": "Всего: {total}  |  Брак: {bad} ({pct:.0f}%)",
        "city_line": "🏙 {city} — брак {bad}/{total} ({pct:.0f}%)",
        "inter_line": "👤 {inter} — {n} анкет",
        "quota_header": "⚠️ По городам (квоты интервьюеров):",
        "few_interviewers": "🏙 {city}: только {n} интервьюер(а) — нужно минимум 2",
        "high_share": "🏙 {city}: {inter} сделал {share:.0f}% анкет города ({cnt}/{total}, > {limit}%)",
        "phone_missing": "☎️ {city}: без номера телефона {pct:.0f}% завершённых анкет ({no_phone}/{n}, > 20%)",
    },
    "uz": {
        "report_header": "📋 Сифат назорати ҳисоботи — Bank bilish",
        "city_report_header": "📋 Ҳисобот — {city}",
        "total_line": "Жами: {total}  |  Нуқсонли: {bad} ({pct:.0f}%)",
        "city_line": "🏙 {city} — нуқсонли {bad}/{total} ({pct:.0f}%)",
        "inter_line": "👤 {inter} — {n} та анкета",
        "quota_header": "⚠️ Шаҳарлар бўйича (интервьюерлар квотаси):",
        "few_interviewers": "🏙 {city}: атиги {n} та интервьюер — камида 2 та керак",
        "high_share": "🏙 {city}: {inter} шаҳар анкеталарининг {share:.0f}% ини қилди ({cnt}/{total}, > {limit}%)",
        "phone_missing": "☎️ {city}: телефон рақамисиз {pct:.0f}% анкета ({no_phone}/{n}, > 20%)",
    },
}


def format_agg_issue(issue, lang="ru"):
    s = UI_STRINGS[lang]
    if issue["type"] == "few_interviewers":
        return s["few_interviewers"].format(city=issue["city"], n=issue["n_inters"])
    if issue["type"] == "phone_missing":
        return s["phone_missing"].format(**issue)
    return s["high_share"].format(**issue)


SEP_LINE = "─" * 24  # тонкая разделительная линия между блоками — чтобы отчёт не сливался в стену текста


def build_city_block(city_df, city_name, lang="ru"):
    s = UI_STRINGS[lang]
    tags = REASON_TAGS_BY_LANG[lang]
    city_defects = city_df[city_df["is_defect"]]
    total = len(city_df)
    bad = len(city_defects)
    lines = [s["city_line"].format(city=f"*{_tg_safe(city_name)}*", bad=bad, total=total,
                                     pct=bad / max(total, 1) * 100)]
    for inter, g in city_defects.groupby("inter"):
        tag_counts = {}
        for reasons in g["reasons"]:
            for r in reasons:
                for key, tag in tags:
                    if key in r:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                        break
        tag_summary = ", ".join(f"{t} ×{c}" for t, c in tag_counts.items())
        # интервьюер и причины — на отдельных строках: одна длинная строка со
        # всем сразу тяжело читать в Telegram, особенно на телефоне
        lines.append(s["inter_line"].format(inter=_tg_safe(inter), n=len(g)))
        lines.append(f"    {tag_summary}")
    return lines


def build_report_lines(df, agg_issues, wave_median, lang="ru"):
    s = UI_STRINGS[lang]
    defects = df[df["is_defect"]]
    lines = []
    lines.append(s["report_header"])
    lines.append(f"{datetime.now().strftime('%d.%m.%Y %H:%M')}")
    lines.append(s["total_line"].format(total=len(df), bad=len(defects),
                                          pct=len(defects)/max(len(df), 1)*100))
    lines.append(SEP_LINE)
    if agg_issues:
        lines.append(s["quota_header"])
        lines.extend(f"  {format_agg_issue(x, lang)}" for x in agg_issues)
        lines.append(SEP_LINE)
    cities = list(df.groupby("city"))
    for i, (city, g) in enumerate(cities):
        lines.extend(build_city_block(g, city, lang))
        if i < len(cities) - 1:
            lines.append(SEP_LINE)
    return lines


def build_city_report_lines(df, city_name, lang="ru"):
    s = UI_STRINGS[lang]
    city_df = df[df["city"] == city_name]
    lines = [s["city_report_header"].format(city=_tg_safe(city_name)),
             f"{datetime.now().strftime('%d.%m.%Y %H:%M')}", SEP_LINE]
    lines.extend(build_city_block(city_df, city_name, lang))
    return lines


st.title("✅ Bank bilish — сентябрьская волна")
st.caption("Автоматическая проверка анкет по требованиям заказчика + алерты в Telegram")

with st.sidebar:
    st.header("⚙️ Параметры проверки")
    st.caption("Официальный ТЗ заказчика: интервал ≥ 2 мин, длит-сть ≤ 30 мин, "
               "минимум ответов в блоках Top-of-Mind (см. вкладку «Обзор»), "
               "телефон известен ≥ 80% анкет по региону, доля интервьюера в городе ≤ 50%.")
    min_interval_min = st.slider("Мин. интервал между стартами анкет (мин)", 1, 10, 2)
    min_duration_min = st.slider(
        "Мин. длительность ЗАВЕРШЁННОГО интервью (мин)", 1, 15, 5,
        help="Применяется только к интервью, дошедшим до конца анкеты. "
             "Скринауты (отсеяны по возрасту/критериям) короткими быть обязаны — "
             "к ним этот порог не применяется."
    )
    max_duration_min = st.slider("Макс. длительность анкеты (мин)", 15, 60, 30)
    brand_median_pct = st.slider(
        "Мин. % от медианы спонтанно названных банков", 30, 100, 70,
        help="Считается по блоку 1–2.7 (Top-of-Mind, БЕЗ подсказки) — отражает "
             "качество зондажа интервьюера ('А ещё?'), а не реальное знание "
             "респондента (для этого есть отдельный аидед-список)."
    )
    max_share_city_pct = st.slider("Макс. доля анкет одного интервьюера в городе (%)", 20, 90, 50)
    st.caption(
        "Минимумы по блокам Top-of-Mind (1–2.7 ≥4, 5–6.6 ≥4, 7–7.5 ≥3, "
        "8–8.5 ≥3, 9–9.5 ≥3, 10–10.5 ≥2) и порог по телефону (>20% без "
        "номера на регион) — фиксированные требования ТЗ, не регулируются "
        "ползунками."
    )

uploaded = st.file_uploader("Загрузите файл выгрузки (.xlsx, лист 'data')", type=["xlsx"])

if uploaded is None:
    st.info("Загрузите Excel-файл выгрузки, чтобы начать проверку.")
    st.stop()

file_bytes = uploaded.getvalue()
df_raw = load_data(file_bytes)

df, agg_issues, wave_median = run_qc(
    df_raw, min_interval_min, min_duration_min, max_duration_min,
    brand_median_pct, max_share_city_pct,
)

defects = df[df["is_defect"]]

# Срез по городам для интерактивного бота (kk_bot.py) — он читает этот файл
# при каждом обращении интервьюера, поэтому сохраняем сразу после подсчёта,
# не дожидаясь, пока аналитик откроет вкладку Telegram.
KK_STATUS_FILE = Path(__file__).resolve().parent / "kk_status.json"
try:
    city_list = sorted(df["city"].dropna().unique().tolist(), key=str)
    # сохраняем отчёты сразу на двух языках — бот подставит нужный
    # в зависимости от того, что подписчик выбрал в своих настройках
    city_reports = {
        lang: {city: "\n".join(build_city_report_lines(df, city, lang)) for city in city_list}
        for lang in ("ru", "uz")
    }
    # код интервьюера → город: бот использует это, чтобы понять, какой именно
    # отчёт по городу автоматически отправить подписчику, привязанному к коду
    inter_to_city = (
        df.dropna(subset=["inter", "city"]).drop_duplicates("inter").set_index("inter")["city"].to_dict()
    )
    full_report_text = {
        lang: "\n".join(build_report_lines(df, agg_issues, wave_median, lang))
        for lang in ("ru", "uz")
    }
    KK_STATUS_FILE.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "cities": city_reports,
        "inter_to_city": inter_to_city,
        "full_report": full_report_text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    _kk_status_saved = True
    _kk_status_error = None
except OSError as e:
    _kk_status_saved = False
    _kk_status_error = str(e)

if not _kk_status_saved:
    st.warning(
        f"⚠️ Не удалось сохранить `{KK_STATUS_FILE.name}` для бота ({_kk_status_error}) — "
        f"интерактивный kk_bot.py не увидит свежие данные по городам, пока это не исправится."
    )
else:
    st.caption(f"✅ Данные для kk_bot.py сохранены: `{KK_STATUS_FILE}`")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Всего анкет", len(df))
c2.metric("Брак", len(defects), f"{len(defects)/max(len(df),1)*100:.1f}%")
c3.metric("Городов", df["city"].nunique())
c4.metric("Интервьюеров", df["inter"].nunique())

# Полные, профессиональные формулировки причин брака — для таблицы в самом
# приложении, где места достаточно (в отличие от Telegram, где используются
# компактные теги из REASON_TAGS_BY_LANG). Порядок важен: более специфичные
# совпадения должны идти раньше общих.
REASON_LABELS_FULL_RU = [
    ("нет deviceid", "Отсутствует Device ID (идентификатор устройства не передан)"),
    ("1 устройство →", "Одно устройство использовалось с нескольких кодов интервьюера"),
    ("1 код интервьюера →", "Один код интервьюера работал с нескольких разных устройств"),
    ("после завершения предыдущей", "Слишком короткий перерыв после завершения предыдущей анкеты (менее 2 минут)"),
    ("конвейер:", "Массовое заполнение анкет — 6 и более анкет за 10 минут на одном устройстве"),
    ("длилась", "Анкета длилась дольше 30 минут"),
    ("длилось", "Завершённая анкета длилась меньше 5 минут"),
    ("интервал с предыдущей", "Интервал между стартом этой и предыдущей анкеты меньше 2 минут"),
    ("Банки (в.1–2.7)", "Мало банков названо в блоке Top-of-Mind «Какой банк первым приходит на ум?» (вопросы 1–2.7, минимум 4)"),
    ("Приложения (в.5–6.6)", "Мало приложений названо в блоке Top-of-Mind «Какое приложение для оплаты» (вопросы 5–6.6, минимум 4)"),
    ("Кредит (в.7–7.5)", "Мало ответов в блоке «Где можно взять кредит» (вопросы 7–7.5, минимум 3)"),
    ("Вклад (в.8–8.5)", "Мало ответов в блоке «Где можно открыть вклад» (вопросы 8–8.5, минимум 3)"),
    ("Реклама (в.9–9.5)", "Мало ответов в блоке «Реклама каких банков видели» (вопросы 9–9.5, минимум 3)"),
    ("Карты (в.10–10.5)", "Мало ответов в блоке «Карты каких банков знаете» (вопросы 10–10.5, минимум 2)"),
    ("сначала «знаю», потом резко «не знаю»", "Знание банков: сначала отвечал «Знаю», затем резко перешёл на «Не знаю» — признак утомления/невнимательности"),
    ("блок знания банков не заполнен", "Блок знания банков не заполнен у завершённой анкеты"),
    ("в среднем", "У интервьюера слишком мало спонтанно названных банков (ниже нормы от медианы волны)"),
]


def categorize_reason(r):
    """Полное, понятное описание причины брака — для таблицы в приложении
    (в отличие от компактных Telegram-тегов из REASON_TAGS_BY_LANG)."""
    for key, label in REASON_LABELS_FULL_RU:
        if key in r:
            return label
    return r[:120]


tab1, tab_inter, tab2, tab3, tab4 = st.tabs(
    ["📊 Обзор", "👤 По интервьюерам", "🚫 Брак", "🏙 Города", "📨 Telegram"]
)

with tab1:
    st.subheader("Причины брака")
    reasons_flat = defects["reasons"].explode().dropna()
    if len(reasons_flat):
        cats = reasons_flat.apply(categorize_reason)
        cats_df = cats.value_counts().reset_index()
        cats_df.columns = ["Причина брака", "Кол-во анкет"]
        cats_df["% от брака"] = (cats_df["Кол-во анкет"] / len(defects) * 100).round(1)
        st.dataframe(cats_df, use_container_width=True, hide_index=True)
    else:
        st.success("Брака не найдено 🎉")

    st.subheader("⚠️ Предупреждения (не считаются браком)")
    st.caption(
        "Общие устройства у интервьюеров, короткая длительность и слабый зондаж в блоке "
        "«Реклама» — сигналы для проверки, но сами по себе НЕ основание для отбраковки анкеты."
    )
    warn_flat = df["warnings"].explode().dropna()
    if len(warn_flat):
        wcats = warn_flat.apply(categorize_reason)
        wcats_df = wcats.value_counts().reset_index()
        wcats_df.columns = ["Предупреждение", "Кол-во анкет"]
        wcats_df["% от всех анкет"] = (wcats_df["Кол-во анкет"] / len(df) * 100).round(1)
        st.dataframe(wcats_df, use_container_width=True, hide_index=True)
    else:
        st.success("Предупреждений не найдено.")

    st.subheader("Ключевые метрики по волне")
    metrics_rows = []
    metrics_rows.append(("Медиана длительности анкеты, мин", f"{df['duration_min'].median():.1f}"))

    tom_bank_n = df.loc[df["completed"] & (df["tom_banks_n"] > 0), "tom_banks_n"]
    metrics_rows.append(("Ср. кол-во названных банков (спонтанно, без подсказки, в.1–2.7)",
                          f"{tom_bank_n.mean():.2f}" if len(tom_bank_n) else "н/д"))
    metrics_rows.append(("Медиана кол-ва названных банков (в.1–2.7)",
                          f"{tom_bank_n.median():.0f}" if len(tom_bank_n) else "н/д"))

    tom_app_n = df.loc[df["completed"] & (df["tom_apps_n"] > 0), "tom_apps_n"]
    metrics_rows.append(("Ср. кол-во названных приложений (спонтанно, без подсказки)",
                          f"{tom_app_n.mean():.2f}" if len(tom_app_n) else "н/д"))
    metrics_rows.append(("Медиана кол-ва названных приложений",
                          f"{tom_app_n.median():.0f}" if len(tom_app_n) else "н/д"))

    tom_credit_n = df.loc[df["completed"] & (df["tom_credit_n"] > 0), "tom_credit_n"]
    metrics_rows.append(("Ср. кол-во названных мест (кредит/микрозайм, в.7–7.5)",
                          f"{tom_credit_n.mean():.2f}" if len(tom_credit_n) else "н/д"))
    metrics_rows.append(("Медиана кол-ва названных мест (кредит/микрозайм)",
                          f"{tom_credit_n.median():.0f}" if len(tom_credit_n) else "н/д"))

    tom_deposit_n = df.loc[df["completed"] & (df["tom_deposit_n"] > 0), "tom_deposit_n"]
    metrics_rows.append(("Ср. кол-во названных мест (вклад/депозит, в.8–8.5)",
                          f"{tom_deposit_n.mean():.2f}" if len(tom_deposit_n) else "н/д"))
    metrics_rows.append(("Медиана кол-ва названных мест (вклад/депозит)",
                          f"{tom_deposit_n.median():.0f}" if len(tom_deposit_n) else "н/д"))

    aided = df.loc[df["awareness_answered"], "aided_known_count"]
    metrics_rows.append(("Ср. кол-во банков, которых знает (аидед-список, из 12)",
                          f"{aided.mean():.2f}" if len(aided) else "н/д"))

    aware_base = df[df["awareness_answered"]]
    uzum_pct = aware_base["know_uzumbank"].mean() * 100 if len(aware_base) else None
    metrics_rows.append(("% знание Uzum Bank (аидед-список)",
                          f"{uzum_pct:.0f}%" if uzum_pct is not None else "н/д"))

    kapital_base = df[df["completed"]]
    kapital_pct = kapital_base["kapital_mentioned"].mean() * 100 if len(kapital_base) else None
    metrics_rows.append(("% упоминаний Kapitalbank (спонтанно — аидед-варианта в этой анкете нет)",
                          f"{kapital_pct:.0f}%" if kapital_pct is not None else "н/д"))

    metrics_df = pd.DataFrame(metrics_rows, columns=["Метрика", "Значение"])
    st.dataframe(metrics_df, use_container_width=True, hide_index=True)

    st.subheader("Знание банков по волне (аидед-список, % «Знаю»)")
    if df["awareness_answered"].sum():
        aw_rows = [(BANK_DISPLAY[key], df.loc[df["awareness_answered"], f"know_{key}"].mean() * 100)
                   for key in BANK_VARIANTS]
        aw_df = pd.DataFrame(aw_rows, columns=["Банк", "% Знаю"]).sort_values("% Знаю", ascending=False)
        aw_df["% Знаю"] = aw_df["% Знаю"].round(0).astype(int).astype(str) + "%"
        st.dataframe(aw_df, use_container_width=True, hide_index=True)
        st.caption(f"База: {int(df['awareness_answered'].sum())} респондентов, дошедших до блока знания банков "
                   f"(Kapitalbank в этот список не входит — см. метрику выше).")
    else:
        st.info("Нет респондентов, дошедших до блока знания банков.")

    st.subheader("Глубина зондажа по блокам Top-of-Mind (вопросы без подсказки)")
    st.caption(
        "Сколько банков/приложений реально назвал респондент в каждом блоке, без подсказки — "
        "напрямую показывает, сколько раз интервьюер спросил «А ещё?». "
        "Считаем среди тех, кто хоть что-то ответил в блоке (0 ответов — легитимное "
        "«не знаю ничего», в среднее/медиану не входит и браком не считается)."
    )
    depth_rows = []
    for block in TOM_BLOCKS:
        col = f"{block['key']}_n"
        completed_col = df.loc[df["completed"], col].fillna(0)
        sub = completed_col[completed_col > 0]
        below_min = int(((completed_col > 0) & (completed_col < block["min_n"])).sum())
        depth_rows.append((
            block["label_ru"],
            f"{sub.mean():.2f}" if len(sub) else "н/д",
            f"{sub.median():.0f}" if len(sub) else "н/д",
            block["min_n"],
            below_min,
        ))
    depth_df = pd.DataFrame(
        depth_rows,
        columns=["Блок вопроса", "Среднее", "Медиана", "Минимум по ТЗ", "Анкет ниже минимума (брак)"],
    )
    st.dataframe(depth_df, use_container_width=True, hide_index=True)
    st.caption(
        "Первый блок («Банки, в.1–2.7») — это вопрос "
        "«1. Название какого банка, первым приходит Вам на ум?» + пробы 2.2–2.7 «А ещё какой банк?» — "
        "минимум 4 названных банка по ТЗ."
    )

with tab_inter:
    st.subheader("Ключевые метрики по интервьюерам")
    st.caption(
        "Те же метрики, что на вкладке «Обзор» — только в разбивке по каждому интервьюеру, "
        "чтобы сравнить качество зондажа и охват банков между ними."
    )
    rows = []
    for inter, g in df.groupby("inter"):
        completed_g = g[g["completed"]]
        n_completed = len(completed_g)

        tom_bank_g = g.loc[g["completed"] & (g["tom_banks_n"] > 0), "tom_banks_n"]
        tom_app_g = g.loc[g["completed"] & (g["tom_apps_n"] > 0), "tom_apps_n"]
        tom_credit_g = g.loc[g["completed"] & (g["tom_credit_n"] > 0), "tom_credit_n"]
        tom_deposit_g = g.loc[g["completed"] & (g["tom_deposit_n"] > 0), "tom_deposit_n"]
        tom_ads_g = g.loc[g["completed"] & (g["tom_ads_n"] > 0), "tom_ads_n"]
        tom_cards_g = g.loc[g["completed"] & (g["tom_cards_n"] > 0), "tom_cards_n"]
        aided_g = g.loc[g["awareness_answered"], "aided_known_count"]
        aware_base_g = g[g["awareness_answered"]]
        uzum_pct_g = aware_base_g["know_uzumbank"].mean() * 100 if len(aware_base_g) else None
        kapital_pct_g = completed_g["kapital_mentioned"].mean() * 100 if len(completed_g) else None

        cities_g = g["city"].dropna()
        city_label = cities_g.mode().iloc[0] if len(cities_g) else "—"

        rows.append({
            "Интервьюер": inter,
            "Город": city_label,
            "Анкет (заверш.)": n_completed,
            "Брак": int(g["is_defect"].sum()),
            "% брака": round(g["is_defect"].mean() * 100, 1) if len(g) else 0,
            "Ср. банков (ToM)": round(tom_bank_g.mean(), 2) if len(tom_bank_g) else None,
            "Ср. приложений (ToM)": round(tom_app_g.mean(), 2) if len(tom_app_g) else None,
            "Ср. кредит (ToM)": round(tom_credit_g.mean(), 2) if len(tom_credit_g) else None,
            "Ср. вклад (ToM)": round(tom_deposit_g.mean(), 2) if len(tom_deposit_g) else None,
            "Ср. реклама (ToM)": round(tom_ads_g.mean(), 2) if len(tom_ads_g) else None,
            "Ср. карты (ToM)": round(tom_cards_g.mean(), 2) if len(tom_cards_g) else None,
            "Ср. аидед-знание (из 12)": round(aided_g.mean(), 2) if len(aided_g) else None,
            "% знание Uzum": round(uzum_pct_g, 0) if uzum_pct_g is not None else None,
            "% упом. Kapitalbank": round(kapital_pct_g, 0) if kapital_pct_g is not None else None,
        })
    inter_df = pd.DataFrame(rows).sort_values("Интервьюер", key=lambda s: s.astype(str)).reset_index(drop=True)
    st.dataframe(inter_df, use_container_width=True, hide_index=True)

with tab2:
    st.subheader(f"Анкеты с браком ({len(defects)})")
    show_cols = ["row_id", "city", "inter", "gender", "age", "deviceid",
                 "start", "end", "duration_min", "reason_text", "warning_text"]
    rename_map = {
        "row_id": "ID анкеты", "city": "Город", "inter": "Интервьюер",
        "gender": "Пол", "age": "Возраст", "deviceid": "Device ID",
        "start": "Старт", "end": "Финиш", "duration_min": "Длительность (мин)",
        "reason_text": "Причина брака", "warning_text": "Предупреждения (справочно)",
    }
    export_df = defects[show_cols].sort_values(["city", "inter"]).rename(columns=rename_map)
    st.dataframe(export_df, use_container_width=True, hide_index=True)

    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Брак")
        ws = writer.sheets["Брак"]
        for i, col in enumerate(export_df.columns, start=1):
            width = min(60, max(12, int(export_df[col].astype(str).str.len().max() or 12) + 2))
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    st.download_button(
        "⬇️ Скачать брак (Excel)",
        xlsx_buf.getvalue(),
        "brak.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

with tab3:
    st.subheader("Проблемы по городам")
    if agg_issues:
        for issue in agg_issues:
            st.warning(format_agg_issue(issue, "ru"))
    else:
        st.success("По городам всё в норме (≥2 интервьюера, ни один не превышает лимит доли).")

    st.subheader("Распределение анкет по городам и интервьюерам")
    pivot = df.pivot_table(index="city", columns="inter", values="row_id", aggfunc="count", fill_value=0)
    st.dataframe(pivot, use_container_width=True)

    st.subheader("Половозрастная структура по городам (справочно)")
    st.caption(
        "Плановых квот по полу/возрасту не задано — показан только факт по "
        "завершённым интервью. Если пришлёшь плановые цифры по городам, "
        "добавлю сравнение план/факт и брак за перекос квоты."
    )
    completed_df = df[df["completed"]]
    if len(completed_df):
        gender_pivot = completed_df.pivot_table(
            index="city", columns="gender", values="row_id", aggfunc="count", fill_value=0
        )
        st.dataframe(gender_pivot, use_container_width=True)

        age_bins = [0, 25, 35, 45, 60, 200]
        age_labels = ["16-24", "25-34", "35-44", "45-59", "60+"]
        completed_df = completed_df.copy()
        completed_df["age_group"] = pd.cut(completed_df["age"], bins=age_bins, labels=age_labels, right=False)
        age_pivot = completed_df.pivot_table(
            index="city", columns="age_group", values="row_id", aggfunc="count", fill_value=0, observed=False
        )
        st.dataframe(age_pivot, use_container_width=True)
    else:
        st.info("Нет завершённых интервью для разбивки.")

with tab4:
    st.subheader("Отправка отчёта в Telegram")
    st.caption("Новый формат: коротко, по городам, теги причин вместо длинного текста. "
               "Полная детализация по каждой анкете — в Excel на вкладке 'Брак'.")

    tg_lang_label = st.radio("Язык отправки", ["🇷🇺 Русский", "🇺🇿 Ўзбек (кирилл)"], horizontal=True)
    tg_lang = "ru" if tg_lang_label.startswith("🇷🇺") else "uz"

    lines = build_report_lines(df, agg_issues, wave_median, tg_lang)
    preview = "\n".join(lines)
    st.text_area("Предпросмотр общего отчёта (все города)", preview, height=300)

    if st.button("📤 Отправить отчёт по ВСЕМ городам", type="primary"):
        chunks = chunk_text(lines)
        ok_all = True
        for ch in chunks:
            ok, resp = tg_send(DEFAULT_TG_TOKEN, DEFAULT_TG_CHAT_ID, ch)
            ok_all = ok_all and ok
            if not ok:
                st.error(f"Ошибка отправки: {resp}")
            time.sleep(0.3)
        if ok_all:
            st.success(f"Отправлено сообщений: {len(chunks)}")

    st.divider()
    st.subheader("📍 Отправить только по одному городу")
    st.caption("Короткое сообщение только с браком этого города — удобно переслать конкретному интервьюеру или куратору города.")
    city_list = sorted(df["city"].dropna().unique().tolist(), key=str)
    picked_city = st.selectbox("Город", city_list)
    city_lines = build_city_report_lines(df, picked_city, tg_lang)
    st.text_area("Предпросмотр по городу", "\n".join(city_lines), height=200)
    if st.button(f"📤 Отправить отчёт по городу «{picked_city}»"):
        ok, resp = tg_send(DEFAULT_TG_TOKEN, DEFAULT_TG_CHAT_ID, "\n".join(city_lines))
        if ok:
            st.success("Отправлено")
        else:
            st.error(f"Ошибка: {resp}")

    st.divider()
    auto = st.checkbox("Автоматически отправлять общий отчёт при каждой загрузке нового файла")
    if auto:
        sent_key = f"autosent_{uploaded.name}_{len(df)}_{tg_lang}"
        if sent_key not in st.session_state:
            chunks = chunk_text(lines)
            ok_all = True
            for ch in chunks:
                ok, resp = tg_send(DEFAULT_TG_TOKEN, DEFAULT_TG_CHAT_ID, ch)
                ok_all = ok_all and ok
                time.sleep(0.3)
            st.session_state[sent_key] = ok_all
            if ok_all:
                st.info("Автоотчёт отправлен в Telegram")

    st.divider()
    st.subheader("👤 Личная рассылка каждому интервьюеру")
    st.caption(
        "Telegram не позволяет боту писать в личку тому, кто ему не писал первым — "
        "нахождения в общей группе для этого недостаточно. Поэтому каждый интервьюер "
        "должен хотя бы раз написать боту **@esmedicine_bot** что угодно (например /start) "
        "в личные сообщения — список ниже пополняет отдельно запущенный **kk_bot.py** "
        "(должен работать постоянно, см. его инструкцию), это приложение только читает "
        "готовый список из файла."
    )

    subs_state_key = "kk_subscribers_cache"
    if subs_state_key not in st.session_state:
        st.session_state[subs_state_key] = _load_subscribers()

    col_r1, col_r2 = st.columns([1, 3])
    with col_r1:
        if st.button("🔄 Перечитать список подписавшихся"):
            st.session_state[subs_state_key] = _load_subscribers()
            st.success("Список обновлён из файла.")
    with col_r2:
        st.caption(f"Сейчас в списке: {len(st.session_state[subs_state_key])} чел. "
                    f"(файл: `{SUBSCRIBERS_FILE.name}`)")

    subs = st.session_state[subs_state_key]
    if subs:
        inter_options = ["— не привязан —"] + sorted(df["inter"].dropna().unique().tolist(), key=str)
        sub_rows = []
        for chat_id, info in subs.items():
            name = f"{info.get('first_name','')} {info.get('last_name','')}".strip()
            uname = f"@{info['username']}" if info.get("username") else ""
            sub_rows.append({"chat_id": chat_id, "Имя": name, "Username": uname,
                              "Привязан к коду": info.get("inter") or "—"})
        st.dataframe(pd.DataFrame(sub_rows), use_container_width=True, hide_index=True)

        with st.expander("🔗 Привязать подписчика к коду интервьюера (для точечной рассылки по городу)"):
            st.caption("Необязательно — без привязки всем уйдёт общий отчёт по всем городам.")
            pick_sub = st.selectbox(
                "Подписчик", list(subs.keys()),
                format_func=lambda cid: f"{subs[cid].get('first_name','')} {subs[cid].get('last_name','')}".strip() or cid,
            )
            pick_inter = st.selectbox("Код интервьюера", inter_options,
                                       index=(inter_options.index(subs[pick_sub].get("inter"))
                                              if subs[pick_sub].get("inter") in inter_options else 0))
            if st.button("Сохранить привязку"):
                subs[pick_sub]["inter"] = "" if pick_inter == "— не привязан —" else pick_inter
                _save_subscribers(subs)
                st.session_state[subs_state_key] = subs
                st.success("Сохранено.")

        st.markdown("**Разослать личным сообщением:**")
        broadcast_mode = st.radio(
            "Что отправить", ["Общий отчёт по всем городам — всем подписчикам",
                               "Только по своему городу — тем, кто привязан к коду"],
            label_visibility="collapsed",
        )
        if st.button("📤 Разослать в личку", type="primary"):
            if broadcast_mode.startswith("Общий"):
                text = "\n".join(lines)
                results = broadcast_to_subscribers(DEFAULT_TG_TOKEN, subs, text)
            else:
                inter_to_city = df.dropna(subset=["inter", "city"]).drop_duplicates("inter").set_index("inter")["city"]
                results = []
                for chat_id, info in subs.items():
                    inter_code = info.get("inter")
                    if not inter_code:
                        continue
                    city_for_inter = inter_to_city.get(inter_code)
                    if city_for_inter is None:
                        continue
                    text = "\n".join(build_city_report_lines(df, city_for_inter, tg_lang))
                    ok, resp = tg_send(DEFAULT_TG_TOKEN, chat_id, text)
                    name = f"{info.get('first_name','')} {info.get('last_name','')}".strip() or chat_id
                    results.append((chat_id, name, ok, resp))
                    time.sleep(0.3)
            ok_count = sum(1 for r in results if r[2])
            st.success(f"Отправлено: {ok_count} из {len(results)}.")
            for chat_id, name, ok, resp in results:
                if not ok:
                    st.error(f"Не доставлено {name} ({chat_id}): {resp}")
    else:
        st.info("Пока никто не подписался. Отправь интервьюерам ссылку на бота "
                 "и попроси написать /start, потом нажми «Обновить список».")

st.divider()
with st.expander("ℹ️ Что проверяется автоматически, а что нет"):
    st.markdown("""
**Проверяется автоматически по листу `data`:**
- deviceid обязателен для каждой анкеты
- 1 устройство = 1 код интервьюера (не более)
- длительность анкеты (мин 5 мин для завершённых / макс 30 мин)
- интервал между стартами анкет на одном устройстве (по умолч. 2 мин, как в ТЗ)
- доля пустых блоков ≤ 5% у завершённых интервью (пол, возраст, активность за
  3 мес, узнаваемость банков, использование банков, отношение к своему банку)
- вопрос про узнаваемость банков заполнен у всех завершённых интервью (иначе брак)
- среднее число известных брендов у интервьюера против медианы волны (≥ 70%)
- минимум 2 интервьюера на город и доля одного интервьюера в городе (≤ 50%)

**Только справочно (НЕ брак):**
- % "Не знаю" по каждому банку и по волне в целом
- половозрастная структура по городам (без плана квот сравнивать не с чем)
- приближённый индикатор по зондажу «А ещё...?» (доля тех, кто назвал только 1 банк)

**Не проверяется автоматически (нет таких полей в выгрузке `data`) — нужен ручной контроль или отдельная выгрузка:**
- точное соответствие точке проведения опроса (GPS-проверка отключена по решению)
- плановые половозрастные квоты по городам (нет плановых цифр — только факт)
- сам факт трёх обязательных проб «А ещё…?» и отметка «респондент отказался продолжать» —
  в экспорте есть только итоговый список известных брендов, а не лог зондажа
- совмещение интервьюера с другими проектами агентства (нет данных о других проектах)
- постоянство кодов интервьюеров между волнами (нужна база кодов за все волны, не только эта)
- реестр интервьюеров с ФИО/табельным номером (ведётся отдельно, не в этой выгрузке)
""")