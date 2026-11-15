#!/usr/bin/env python3
"""
US Macroeconomic & FOMC Automated Pipeline (macro/latest.json).

Sources:
  - Official FRED API (fallback: official FRED CSV endpoint if FRED_API_KEY unset)
  - Federal Reserve FOMC Calendar & Minutes HTML
  - Optional Gemini API summary in Traditional Chinese (with extractive fallback)
  - Optional verified consensus/FedWatch override file (no fabricated consensus)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch_macro")

ROOT = Path(__file__).resolve().parents[1]
MACRO_DIR = ROOT / "macro"
LATEST_PATH = MACRO_DIR / "latest.json"
OVERRIDE_PATH = MACRO_DIR / "consensus_override.json"

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
CME_FEDWATCH_URL = (
    "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
)

RETRY_STATUSES = {404, 429, 500, 502, 503, 504}
MONTH_MAP = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

INDICATOR_RANGES: dict[str, tuple[float, float]] = {
    "cpi_yoy": (-15.0, 30.0),
    "core_pce_yoy": (-10.0, 25.0),
    "nfp": (-30000.0, 10000.0),
    "unemployment": (0.5, 30.0),
    "ppi_yoy": (-25.0, 40.0),
    "gdp_qoq": (-40.0, 45.0),
}


def utc_now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def http_get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
    max_attempts: int = 3,
) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code in RETRY_STATUSES:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} for {url}", response=resp
                )
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_err = exc
            if attempt < max_attempts:
                delay = 1.5 * (2 ** (attempt - 1))
                log.warning(
                    "Request attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
    assert last_err is not None
    raise last_err


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Could not parse %s (%s); starting fresh.", path, exc)
        return {}


def parse_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    s = str(raw).strip()
    if not s or s == ".":
        return None
    val = float(s)
    if not math.isfinite(val):
        return None
    return val


def fetch_fred_observations(
    session: requests.Session,
    series_id: str,
    fred_api_key: str | None,
    limit: int = 120,
) -> tuple[list[tuple[dt.date, float]], str]:
    """
    Returns descending [(date, value), ...] excluding missing points and source tag.
    Prefers official api.stlouisfed.org when FRED_API_KEY is available.
    """
    if fred_api_key:
        try:
            resp = http_get_with_retry(
                session,
                FRED_OBS_URL,
                params={
                    "series_id": series_id,
                    "api_key": fred_api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": limit,
                },
            )
            payload = resp.json()
            obs = payload.get("observations", [])
            parsed: list[tuple[dt.date, float]] = []
            for row in obs:
                val = parse_float(row.get("value"))
                if val is None:
                    continue
                d = dt.date.fromisoformat(str(row["date"]))
                parsed.append((d, val))
            if not parsed:
                raise ValueError(f"Empty FRED API response for {series_id}")
            return parsed, f"FRED API ({series_id})"
        except Exception as exc:
            log.warning(
                "FRED API failed for %s (%s); trying official FRED CSV fallback.",
                series_id,
                exc,
            )

    resp = http_get_with_retry(
        session,
        FRED_CSV_URL,
        params={"id": series_id},
    )
    reader = csv.reader(io.StringIO(resp.text))
    next(reader, None)  # header
    parsed_csv: list[tuple[dt.date, float]] = []
    for row in reader:
        if len(row) < 2:
            continue
        val = parse_float(row[1])
        if val is None:
            continue
        try:
            d = dt.date.fromisoformat(row[0].strip())
        except ValueError:
            continue
        parsed_csv.append((d, val))
    parsed_csv.sort(key=lambda item: item[0], reverse=True)
    if not parsed_csv:
        raise ValueError(f"No observations found in FRED CSV for {series_id}")
    return parsed_csv[:limit], f"FRED ({series_id})"


def compute_yoy_by_calendar(
    observations: list[tuple[dt.date, float]],
) -> tuple[float, float, dt.date, dt.date]:
    """
    Computes exact calendar-month YoY percentages for the latest two observations
    that each have a valid observation exactly 12 months earlier.
    """
    by_ym = {(d.year, d.month): (d, v) for d, v in observations}
    pairs: list[tuple[dt.date, float]] = []
    for d, curr in sorted(observations, key=lambda x: x[0], reverse=True):
        base = by_ym.get((d.year - 1, d.month))
        if base is None or base[1] == 0:
            continue
        yoy = round(((curr - base[1]) / base[1]) * 100.0, 1)
        pairs.append((d, yoy))
        if len(pairs) >= 2:
            break
    if len(pairs) < 2:
        raise ValueError("Insufficient matched 12-month pairs for YoY calculation")
    return pairs[0][1], pairs[1][1], pairs[0][0], pairs[1][0]


def prev_ym(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def compute_monthly_diff(
    observations: list[tuple[dt.date, float]],
) -> tuple[int, int, dt.date]:
    """Computes consecutive calendar-month differences (used for NFP PAYEMS)."""
    by_ym = {(d.year, d.month): (d, v) for d, v in observations}
    diffs: list[tuple[dt.date, int]] = []
    for d, curr in sorted(observations, key=lambda x: x[0], reverse=True):
        pm = prev_ym(d.year, d.month)
        prior = by_ym.get(pm)
        if prior is None:
            continue
        diffs.append((d, int(round(curr - prior[1]))))
        if len(diffs) >= 2:
            break
    if len(diffs) < 2:
        raise ValueError("Insufficient consecutive monthly observations for diff")
    return diffs[0][1], diffs[1][1], diffs[0][0]


def extract_fed_target_range(
    low_obs: list[tuple[dt.date, float]],
    high_obs: list[tuple[dt.date, float]],
) -> tuple[str, str | None, dt.date]:
    low_map = {d: v for d, v in low_obs}
    high_map = {d: v for d, v in high_obs}
    common_dates = sorted(set(low_map) & set(high_map), reverse=True)
    if not common_dates:
        raise ValueError("No overlapping dates between DFEDTARL and DFEDTARU")

    def fmt(lo: float, hi: float) -> str:
        return f"{lo:.2f}% - {hi:.2f}%"

    latest_date = common_dates[0]
    curr_pair = (round(low_map[latest_date], 4), round(high_map[latest_date], 4))
    prev_pair: tuple[float, float] | None = None
    for d in common_dates[1:]:
        pair = (round(low_map[d], 4), round(high_map[d], 4))
        if pair != curr_pair:
            prev_pair = pair
            break

    return (
        fmt(*curr_pair),
        fmt(*prev_pair) if prev_pair else None,
        latest_date,
    )


def _parse_fomc_end_date(year: int, month_text: str, day_text: str) -> dt.date | None:
    clean_m = month_text.strip().lower().split("/")[-1].strip()
    m_num = MONTH_MAP.get(clean_m)
    if not m_num:
        return None
    nums = [int(n) for n in re.findall(r"\d+", day_text)]
    if not nums:
        return None
    end_day = nums[-1]
    try:
        return dt.date(year, m_num, end_day)
    except ValueError:
        return None


def parse_html(html: str) -> BeautifulSoup:
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def scrape_fomc_calendar(
    session: requests.Session, today_utc: dt.date
) -> dict[str, Any]:
    resp = http_get_with_retry(session, FED_CALENDAR_URL)
    soup = parse_html(resp.text)

    upcoming_dates: list[dt.date] = []
    statements: list[tuple[dt.date, str]] = []
    minutes_list: list[tuple[dt.date, str | None, str]] = []

    for panel in soup.select(".panel"):
        heading = panel.select_one(".panel-heading")
        if not heading:
            continue
        year_match = re.search(r"(20\d{2})\s+FOMC", heading.get_text(" ", strip=True))
        if not year_match:
            continue
        panel_year = int(year_match.group(1))
        for row in panel.select(".fomc-meeting"):
            m_el = row.select_one(".fomc-meeting__month")
            d_el = row.select_one(".fomc-meeting__date")
            if m_el and d_el:
                end_date = _parse_fomc_end_date(
                    panel_year, m_el.get_text(" ", strip=True), d_el.get_text(" ", strip=True)
                )
                if end_date and end_date >= today_utc:
                    upcoming_dates.append(end_date)

            for link in row.select("a[href]"):
                href = str(link.get("href", "")).strip()
                if not href.endswith(".htm"):
                    continue
                if "fomcminutes" in href:
                    m = re.search(r"fomcminutes(\d{4})(\d{2})(\d{2})\.htm", href)
                    if m:
                        m_date = dt.date(int(m[1]), int(m[2]), int(m[3]))
                        full_url = (
                            href
                            if href.startswith("http")
                            else f"https://www.federalreserve.gov{href}"
                        )
                        parent_txt = link.parent.get_text(" ", strip=True) if link.parent else ""
                        rel_match = re.search(
                            r"Released\s+([A-Za-z]+\s+\d{1,2},\s+20\d{2})", parent_txt
                        )
                        rel_iso: str | None = None
                        if rel_match:
                            try:
                                rel_iso = dt.datetime.strptime(
                                    rel_match.group(1), "%B %d, %Y"
                                ).date().isoformat()
                            except ValueError:
                                rel_iso = None
                        minutes_list.append((m_date, rel_iso, full_url))
                elif re.search(r"monetary(\d{4})(\d{2})(\d{2})a\.htm", href):
                    s_match = re.search(r"monetary(\d{4})(\d{2})(\d{2})a\.htm", href)
                    if s_match:
                        s_date = dt.date(int(s_match[1]), int(s_match[2]), int(s_match[3]))
                        full_url = (
                            href
                            if href.startswith("http")
                            else f"https://www.federalreserve.gov{href}"
                        )
                        statements.append((s_date, full_url))

    upcoming_dates.sort()
    minutes_list.sort(key=lambda x: x[0], reverse=True)
    statements.sort(key=lambda x: x[0], reverse=True)

    result: dict[str, Any] = {
        "next_fomc_date": upcoming_dates[0].isoformat() if upcoming_dates else None,
        "statement_date": statements[0][0].isoformat() if statements else None,
        "statement_url": statements[0][1] if statements else None,
        "minutes_meeting_date": minutes_list[0][0].isoformat() if minutes_list else None,
        "minutes_release_date": minutes_list[0][1] if minutes_list else None,
        "minutes_url": minutes_list[0][2] if minutes_list else None,
    }
    return result


def extract_minutes_text(session: requests.Session, minutes_url: str) -> str:
    resp = http_get_with_retry(session, minutes_url)
    soup = parse_html(resp.text)
    article = soup.select_one("#article") or soup.body or soup
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in article.select("p")
        if len(p.get_text(" ", strip=True)) > 40
    ]
    return "\n\n".join(paragraphs)


def _format_rate_token(raw: str) -> str:
    token = raw.strip().replace("-", " ").split()
    if len(token) == 2 and "/" in token[1]:
        whole = int(token[0])
        num, den = token[1].split("/", 1)
        val = whole + int(num) / int(den)
        return f"{val:.2f}%"
    try:
        return f"{float(raw.strip()):.2f}%"
    except ValueError:
        return f"{raw.strip()}%"


def fallback_extractive_summary(text: str) -> tuple[str, list[str]]:
    lower = text.lower()
    hawk_hits = sum(
        lower.count(w)
        for w in (
            "upside risks to inflation",
            "elevated inflation",
            "inflation remained elevated",
            "restrictive",
            "tightening",
            "higher for longer",
            "another increase in the target range",
            "raising the target range",
        )
    )
    dove_hits = sum(
        lower.count(w)
        for w in (
            "downside risks to employment",
            "easing",
            "rate cut",
            "lower the target range",
            "lowering the target range",
            "disinflation",
            "moving toward 2 percent",
            "further reduction",
        )
    )
    if dove_hits >= hawk_hits + 2:
        tone = "偏鴿"
    elif hawk_hits >= dove_hits + 2:
        tone = "偏鷹"
    else:
        tone = "中性"

    bullets: list[str] = []

    # 1. 利率決策與共識
    range_match = re.search(
        r"(raising|lowering|maintaining)\s+the\s+target\s+range\s+for\s+the\s+federal\s+funds\s+rate"
        r"(?:\s+(?:by\s+)?(?:1/4|25|1/2|50)\s+(?:percentage\s+point|basis\s+points?))?"
        r"\s+to\s+([\d\-/\s]+?)\s+to\s+([\d\-/\s]+?)\s+percent",
        lower,
    )
    if not range_match:
        range_match = re.search(
            r"to\s+(raise|lower|maintain)\s+the\s+target\s+range\s+for\s+the\s+federal\s+funds\s+rate"
            r"(?:\s+by\s+(?:1/4|25|1/2|50)\s+(?:percentage\s+point|basis\s+points?))?"
            r"\s+to\s+([\d\-/\s]+?)\s+to\s+([\d\-/\s]+?)\s+percent",
            lower,
        )
    unanimous = (
        "all members agreed" in lower
        or "all participants supported" in lower
        or "voted unanimously" in lower
    )
    consensus_label = "全體與會成員一致支持" if unanimous else "委員會經討論後決議"
    if range_match:
        verb = range_match.group(1)
        lo = _format_rate_token(range_match.group(2))
        hi = _format_rate_token(range_match.group(3))
        if verb.startswith("rais") or verb == "raise":
            action_zh = "升息（調升目標區間"
        elif verb.startswith("lower"):
            action_zh = "降息（調降目標區間"
        else:
            action_zh = "維持利率不變（目標區間維持"
        bullets.append(f"利率決策：{consensus_label}{action_zh}至 {lo}–{hi}）。")
    elif "maintain the target range" in lower or "maintaining the target range" in lower:
        bullets.append(f"利率決策：{consensus_label}維持聯邦基金利率目標區間不變。")
    else:
        bullets.append(f"利率決策：{consensus_label}當前聯邦基金利率目標區間立場。")

    # 2. 通膨評估與風險
    if "inflation remained elevated" in lower or "remain elevated in the near term" in lower:
        if "decline toward 2 percent" in lower or "moving toward 2 percent" in lower:
            bullets.append("通膨評估：短期物價壓力仍偏高，官員預期需配合適當貨幣政策才會逐步回落至 2%。")
        else:
            bullets.append("通膨評估：多數官員認為通膨水準仍高於 2% 目標，物價上行風險尚未完全消除。")
    elif "disinflation" in lower or "moving toward 2 percent" in lower:
        bullets.append("通膨評估：與會官員指出通膨朝 2% 目標降溫，但仍需留意上行風險。")
    else:
        bullets.append("通膨評估：委員會持續關注物價動能變化，以確保通膨穩定朝 2% 目標前進。")

    # 3. 就業與經濟動能
    if "near full employment" in lower or "remain stable" in lower:
        growth_note = "經濟活動維持穩健擴張步調，" if "solid pace" in lower else ""
        bullets.append(f"經濟與就業：{growth_note}勞動市場維持穩定且接近充分就業，上下行風險大致平衡。")
    elif "downside risks to employment" in lower or "softened" in lower:
        bullets.append("經濟與就業：勞動市場呈溫和降溫跡象，部分官員留意就業下行風險。")
    else:
        bullets.append("經濟與就業：整體經濟活動維持溫和擴張，勞動市場與就業情勢保持審慎觀察。")

    # 4. 前瞻指引與利率路徑
    if "another increase in the target range" in lower:
        bullets.append("政策展望：多數官員評估年底前可能仍需再次升息，但強調逐次會議將依最新數據審慎決定。")
    elif "further reduction" in lower or "easing" in lower:
        bullets.append("政策展望：政策步調保留調整彈性，後續將視通膨回落與就業變化決定降息節奏。")
    else:
        bullets.append("政策展望：與會成員強調不預設立場，將依據後續總經數據與風險平衡逐次評估政策利率。")

    return tone, bullets


def summarize_minutes_with_gemini(
    text: str, gemini_api_key: str
) -> tuple[str, list[str]] | None:
    try:
        from google import genai
    except Exception as exc:
        log.warning("google-genai not available (%s); using fallback.", exc)
        return None

    try:
        client = genai.Client(api_key=gemini_api_key)
        prompt = (
            "你是美國總經分析助手。請根據以下聯準會 FOMC 會議紀要原文（英文），"
            "輸出嚴格 JSON 格式，不要加 Markdown 圍欄，欄位如下：\n"
            '{"tone": "偏鴿|中性|偏鷹", "summary": ["...", "...", "..."]}\n'
            "要求：\n"
            "1. summary 必須為 4 條簡潔精準的繁體中文重點，分別涵蓋：\n"
            "   (1) 利率決策與投票／政策分歧\n"
            "   (2) 通膨評估\n"
            "   (3) 就業市場與經濟展望\n"
            "   (4) 未來利率路徑條件\n"
            "2. 僅依據原文陳述，不可編造原文未提及的數字或結論。\n\n"
            f"原文節錄：\n{text[:18000]}"
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        raw = (response.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        parsed = json.loads(raw)
        tone = str(parsed.get("tone", "中性")).strip()
        if tone not in {"偏鴿", "中性", "偏鷹"}:
            tone = "中性"
        summary = [
            str(item).strip()
            for item in parsed.get("summary", [])
            if str(item).strip()
        ]
        if not summary:
            return None
        return tone, summary
    except Exception as exc:
        log.warning("Gemini summary generation failed (%s); using fallback.", exc)
        return None


def update_indicator(
    prev_ind: dict[str, Any],
    *,
    key: str,
    actual: float | int,
    previous: float | int,
    period: str,
    release_date: str | None,
    unit: str,
    source: str,
    seasonal_adjustment: str,
    now_iso: str,
    override_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    consensus = None
    consensus_source = None
    consensus_updated_at = None
    if isinstance(override_entry, dict) and override_entry.get("consensus") is not None:
        src = override_entry.get("source")
        ts = override_entry.get("updated_at")
        ov_period = override_entry.get("period")
        if src and ts and (ov_period is None or ov_period == period):
            consensus = override_entry["consensus"]
            consensus_source = str(src)
            consensus_updated_at = str(ts)

    entry = {
        "actual": actual,
        "consensus": consensus,
        "previous": previous,
        "unit": unit,
        "period": period,
        "release_date": release_date or prev_ind.get("release_date"),
        "source": source,
        "seasonal_adjustment": seasonal_adjustment,
        "updated_at": now_iso,
        "status": "ok",
    }
    if consensus_source:
        entry["consensus_source"] = consensus_source
        entry["consensus_updated_at"] = consensus_updated_at
    lo, hi = INDICATOR_RANGES[key]
    if not (lo <= float(actual) <= hi):
        raise ValueError(f"Indicator {key} actual={actual} out of range [{lo}, {hi}]")
    return entry


def validate_output(doc: dict[str, Any]) -> None:
    if not doc.get("updated_at"):
        raise ValueError("Missing updated_at")
    fed = doc.get("fed")
    if not isinstance(fed, dict):
        raise ValueError("Missing fed object")
    indicators = doc.get("indicators")
    if not isinstance(indicators, dict):
        raise ValueError("Missing indicators object")
    for k, (lo, hi) in INDICATOR_RANGES.items():
        item = indicators.get(k)
        if not isinstance(item, dict):
            raise ValueError(f"Missing indicator {k}")
        val = item.get("actual")
        if val is not None:
            num = float(val)
            if not math.isfinite(num) or not (lo <= num <= hi):
                raise ValueError(f"Invalid range for {k}: {num}")
    serialized = json.dumps(doc, ensure_ascii=False, allow_nan=False)
    json.loads(serialized)


def run_pipeline(dry_run: bool = False) -> dict[str, Any]:
    now_iso = utc_now_iso()
    today_utc = dt.datetime.now(dt.timezone.utc).date()

    fred_api_key = os.environ.get("FRED_API_KEY", "").strip() or None
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip() or None

    base = load_json_dict(LATEST_PATH)
    override = load_json_dict(OVERRIDE_PATH)
    prev_fed = dict(base.get("fed") or {})
    prev_indicators = dict(base.get("indicators") or {})

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "MacroBriefBot/1.0 (+https://github.com/jimmysun1234/daily-tech-brief)"
            )
        }
    )

    warnings: list[str] = []
    fed_out: dict[str, Any] = dict(prev_fed)

    # 1. Fed Target Rate (DFEDTARL / DFEDTARU)
    try:
        low_obs, low_src = fetch_fred_observations(
            session, "DFEDTARL", fred_api_key, limit=400
        )
        high_obs, _ = fetch_fred_observations(
            session, "DFEDTARU", fred_api_key, limit=400
        )
        curr_rate, prev_rate, rate_date = extract_fed_target_range(low_obs, high_obs)
        fed_out["current_rate"] = curr_rate
        if prev_rate:
            fed_out["previous_rate"] = prev_rate
        fed_out["rate_as_of"] = rate_date.isoformat()
        fed_out["rate_source"] = low_src
    except Exception as exc:
        warnings.append(f"DFEDTARL/DFEDTARU failed: {exc}")
        log.warning("Fed target rate update failed (%s); preserving prior.", exc)

    # 2. Federal Reserve FOMC Calendar & Minutes
    try:
        cal = scrape_fomc_calendar(session, today_utc)
        if cal.get("next_fomc_date"):
            fed_out["next_fomc_date"] = cal["next_fomc_date"]
        if cal.get("statement_url"):
            fed_out["statement_url"] = cal["statement_url"]
            fed_out["statement_date"] = cal["statement_date"]

        new_min_url = cal.get("minutes_url")
        if new_min_url:
            old_min_url = prev_fed.get("minutes_url")
            old_summary = prev_fed.get("minutes_summary")
            has_legacy_boilerplate = isinstance(old_summary, list) and any(
                ("未啟用 LLM" in str(item) or "GEMINI_API_KEY" in str(item))
                for item in old_summary
            )
            need_summary = (
                (new_min_url != old_min_url)
                or not old_summary
                or has_legacy_boilerplate
            )
            fed_out["minutes_url"] = new_min_url
            fed_out["minutes_date"] = (
                cal.get("minutes_release_date") or cal.get("minutes_meeting_date")
            )
            fed_out["minutes_meeting_date"] = cal.get("minutes_meeting_date")
            if need_summary:
                log.info("Analyzing FOMC minutes from %s", new_min_url)
                minutes_text = extract_minutes_text(session, new_min_url)
                gem_res = (
                    summarize_minutes_with_gemini(minutes_text, gemini_api_key)
                    if gemini_api_key
                    else None
                )
                tone, summary = gem_res or fallback_extractive_summary(minutes_text)
                fed_out["minutes_tone"] = tone
                fed_out["minutes_tone_is_analysis"] = True
                fed_out["minutes_summary"] = summary
                fed_out["minutes_summary_method"] = (
                    "gemini-2.5-flash" if gem_res else "extractive-fallback"
                )
        else:
            log.info("No FOMC minutes link found on calendar; preserving previous.")
    except Exception as exc:
        warnings.append(f"FOMC calendar/minutes scrape failed: {exc}")
        log.warning("FOMC calendar scrape failed (%s); preserving prior.", exc)

    # 3. FedWatch Probabilities (Strict: never call unauthorized CME APIs; never fabricate)
    ov_fed = override.get("fed") if isinstance(override.get("fed"), dict) else {}
    ov_probs = (
        ov_fed.get("probabilities")
        if isinstance(ov_fed.get("probabilities"), dict)
        else {}
    )
    if (
        ov_probs.get("cut") is not None
        and ov_probs.get("hold") is not None
        and ov_probs.get("hike") is not None
        and ov_probs.get("source")
        and ov_probs.get("updated_at")
    ):
        fed_out["expected_rate"] = ov_fed.get("expected_rate")
        fed_out["probabilities"] = {
            "cut": float(ov_probs["cut"]),
            "hold": float(ov_probs["hold"]),
            "hike": float(ov_probs["hike"]),
            "status": "ok",
            "source": str(ov_probs["source"]),
            "updated_at": str(ov_probs["updated_at"]),
            "official_url": CME_FEDWATCH_URL,
        }
    else:
        fed_out["expected_rate"] = ov_fed.get("expected_rate")
        fed_out["probabilities"] = {
            "hike": None,
            "hold": None,
            "cut": None,
            "status": "unavailable",
            "note": "暫無資料（未使用未授權 CME 介面；可點下方官網查詢）",
            "official_url": CME_FEDWATCH_URL,
        }

    # 4. US Macro Indicators
    indicators_out: dict[str, Any] = {}
    ov_inds = (
        override.get("indicators") if isinstance(override.get("indicators"), dict) else {}
    )

    series_jobs = [
        ("cpi_yoy", "CPIAUCSL", "yoy", "%", "SA"),
        ("core_pce_yoy", "PCEPILFE", "yoy", "%", "SA"),
        ("nfp", "PAYEMS", "diff", "千人", "SA"),
        ("unemployment", "UNRATE", "level", "%", "SA"),
        ("ppi_yoy", "PPIFID", "yoy", "%", "NSA"),
        ("gdp_qoq", "A191RL1Q225SBEA", "level", "%", "SAAR"),
    ]

    for key, series_id, mode, unit, adj in series_jobs:
        prev_entry = dict(prev_indicators.get(key) or {})
        try:
            obs, src = fetch_fred_observations(
                session, series_id, fred_api_key, limit=36
            )
            if mode == "yoy":
                actual_v, prev_v, d_curr, _ = compute_yoy_by_calendar(obs)
                period = d_curr.strftime("%Y-%m")
            elif mode == "diff":
                actual_v, prev_v, d_curr = compute_monthly_diff(obs)
                period = d_curr.strftime("%Y-%m")
            else:
                if len(obs) < 2:
                    raise ValueError(f"Need >=2 points for {series_id}")
                d_curr = obs[0][0]
                actual_v = round(obs[0][1], 1)
                prev_v = round(obs[1][1], 1)
                period = d_curr.strftime("%Y-%m")

            indicators_out[key] = update_indicator(
                prev_entry,
                key=key,
                actual=actual_v,
                previous=prev_v,
                period=period,
                release_date=None,
                unit=unit,
                source=src,
                seasonal_adjustment=adj,
                now_iso=now_iso,
                override_entry=ov_inds.get(key),
            )
        except Exception as exc:
            warnings.append(f"{key} ({series_id}) failed: {exc}")
            log.warning("Indicator %s failed (%s); preserving history.", key, exc)
            if prev_entry and prev_entry.get("actual") is not None:
                prev_entry["status"] = "stale"
                prev_entry["last_error"] = str(exc)
                indicators_out[key] = prev_entry
            else:
                indicators_out[key] = {
                    "actual": None,
                    "consensus": None,
                    "previous": None,
                    "unit": unit,
                    "period": None,
                    "release_date": None,
                    "source": f"FRED ({series_id})",
                    "seasonal_adjustment": adj,
                    "updated_at": now_iso,
                    "status": "missing",
                }

    overall_status = (
        "ok"
        if not warnings
        else ("error" if all(v.get("status") == "missing" for v in indicators_out.values()) else "stale")
    )

    doc: dict[str, Any] = {
        "schema_version": "macro-brief-v2",
        "updated_at": now_iso,
        "status": overall_status,
        "warnings": warnings,
        "fed": fed_out,
        "indicators": indicators_out,
    }

    validate_output(doc)

    if not dry_run:
        MACRO_DIR.mkdir(parents=True, exist_ok=True)
        LATEST_PATH.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("Wrote %s (status=%s)", LATEST_PATH, overall_status)
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch US macro indicators and FOMC data.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print summary without modifying macro/latest.json.",
    )
    args = parser.parse_args()
    doc = run_pipeline(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
