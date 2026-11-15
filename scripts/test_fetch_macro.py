#!/usr/bin/env python3
"""Unit tests for fetch_macro calculations, state preservation, and validation."""

import datetime as dt
import unittest

import fetch_macro as fm


class FetchMacroTests(unittest.TestCase):
    def test_compute_yoy_skips_month_with_missing_12m_base(self):
        # Simulate CPIAUCSL where 2025-10-01 is missing so 2026-10-01 has no 12m base.
        obs = [
            (dt.date(2026, 10, 1), 337.476),
            (dt.date(2026, 9, 1), 336.298),
            (dt.date(2026, 8, 1), 334.131),
            # 2025-10-01 intentionally absent
            (dt.date(2025, 9, 1), 324.245),
            (dt.date(2025, 8, 1), 323.000),
        ]
        actual, previous, d_curr, d_prev = fm.compute_yoy_by_calendar(obs)
        self.assertEqual(d_curr, dt.date(2026, 9, 1))
        self.assertEqual(d_prev, dt.date(2026, 8, 1))
        self.assertEqual(actual, round((336.298 - 324.245) / 324.245 * 100, 1))
        self.assertEqual(previous, round((334.131 - 323.000) / 323.000 * 100, 1))

    def test_compute_monthly_diff_nfp(self):
        obs = [
            (dt.date(2026, 10, 1), 159293.0),
            (dt.date(2026, 9, 1), 159128.0),
            (dt.date(2026, 8, 1), 159068.0),
        ]
        actual, prev, d_curr = fm.compute_monthly_diff(obs)
        self.assertEqual(d_curr, dt.date(2026, 10, 1))
        self.assertEqual(actual, 165)
        self.assertEqual(prev, 60)

    def test_extract_fed_target_range(self):
        lows = [
            (dt.date(2026, 11, 2), 3.75),
            (dt.date(2026, 11, 1), 3.75),
            (dt.date(2026, 10, 15), 4.00),
        ]
        highs = [
            (dt.date(2026, 11, 2), 4.00),
            (dt.date(2026, 11, 1), 4.00),
            (dt.date(2026, 10, 15), 4.25),
        ]
        curr, prev, as_of = fm.extract_fed_target_range(lows, highs)
        self.assertEqual(curr, "3.75% - 4.00%")
        self.assertEqual(prev, "4.00% - 4.25%")
        self.assertEqual(as_of, dt.date(2026, 11, 2))

    def test_consensus_override_requires_source_and_timestamp(self):
        # Missing source -> consensus stays None (no fabricated data).
        res_no_src = fm.update_indicator(
            {},
            key="cpi_yoy",
            actual=3.7,
            previous=3.4,
            period="2026-09",
            release_date="2026-10-15",
            unit="%",
            source="FRED (CPIAUCSL)",
            seasonal_adjustment="SA",
            now_iso="2026-11-15T00:00:00Z",
            override_entry={"consensus": 3.5, "source": None, "updated_at": None},
        )
        self.assertIsNone(res_no_src["consensus"])

        # Valid source + timestamp + matching period -> consensus applied.
        res_valid = fm.update_indicator(
            {},
            key="cpi_yoy",
            actual=3.7,
            previous=3.4,
            period="2026-09",
            release_date="2026-10-15",
            unit="%",
            source="FRED (CPIAUCSL)",
            seasonal_adjustment="SA",
            now_iso="2026-11-15T00:00:00Z",
            override_entry={
                "consensus": 3.5,
                "period": "2026-09",
                "source": "Market Consensus Override",
                "updated_at": "2026-10-14T12:00:00Z",
            },
        )
        self.assertEqual(res_valid["consensus"], 3.5)

    def test_validate_output_rejects_out_of_range(self):
        bad = {
            "updated_at": "2026-11-15T00:00:00Z",
            "fed": {},
            "indicators": {
                "cpi_yoy": {"actual": 999.0},
                "core_pce_yoy": {"actual": 2.5},
                "nfp": {"actual": 150},
                "unemployment": {"actual": 4.1},
                "ppi_yoy": {"actual": 2.0},
                "gdp_qoq": {"actual": 2.5},
            },
        }
        with self.assertRaises(ValueError):
            fm.validate_output(bad)


if __name__ == "__main__":
    unittest.main()
