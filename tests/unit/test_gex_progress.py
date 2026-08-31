"""Progress-contract tests for the GEX CLI commands (Phase 7, Task 7.4).

The plan (§2) requires every new long-running command to:

- report through ``mktmtm.progress`` (rich bars on a TTY, log lines when
  stderr is not a TTY or ``MKTMTM_NO_PROGRESS=1`` is set);
- print a final summary table (rich ``Table`` on a TTY, log lines otherwise).

The rich path is the one that breaks silently in CI — a non-TTY runner never
executes it — so these tests force ``use_rich`` explicitly and assert the rich
rendering works, and separately assert the ``MKTMTM_NO_PROGRESS=1`` log path.
"""

from __future__ import annotations

import logging

import pytest
from typer.testing import CliRunner

from mktmtm.cli import app
from mktmtm.gex_pipeline import print_results_table, print_summary_table

runner = CliRunner()

# A Phase-6-shaped result dict (matches run_exit_test's return contract).
_RESULT = {
    "phase": "Phase 6 — hypothesis tests",
    "data": {"rows": 300, "symbols": 3, "dates": 100, "period": "2026-01-01 -> 2026-05-29"},
    "h1_sign_test": {
        "gex_positive": {"n": 150, "mean_abs_return": 0.010},
        "gex_negative": {"n": 149, "mean_abs_return": 0.014},
        "gex_zero": {"n": 1, "mean_abs_return": 0.011},
        "welch_t_stat": -2.3,
        "welch_p_value": 0.021,
        "mean_diff_pos_minus_neg": -0.004,
        "h1_consistent": True,
        "ic_neg_gex": {"n_days": 99, "mean_ic": 0.042, "icir": 1.8, "ic_t_stat": 2.4, "hit_rate": 0.58},
    },
    "h2_positioning_change": {
        "gex_chg": {
            "gex_positive": {"n": 100, "mean_abs_return": 0.011},
            "gex_negative": {"n": 100, "mean_abs_return": 0.013},
            "gex_zero": {"n": 100, "mean_abs_return": 0.012},
            "partial_ic_controlling_gex": {
                "n_days": 90,
                "mean_ic": 0.015,
                "icir": 0.7,
                "ic_t_stat": 1.1,
                "hit_rate": 0.52,
            },
        }
    },
    "model": {"ran": False, "reason": "no signal in Task 6.1 or 6.2 — model skipped by plan"},
    "robustness": {
        "flip_dealer_sign": {
            "gex_positive": {"n": 149, "mean_abs_return": 0.014},
            "gex_negative": {"n": 150, "mean_abs_return": 0.010},
            "gex_zero": {"n": 1, "mean_abs_return": 0.011},
            "h1_consistent": False,
            "ic_neg_gex": {"n_days": 99, "mean_ic": -0.041, "icir": -1.7, "ic_t_stat": -2.2, "hit_rate": 0.42},
        },
        "liquidity_filter": {"status": "blocked", "reason": "chain has no gamma yet"},
        "index_vs_stock": {"indices": None, "stocks": None},
        "expiry_week": {"status": "blocked", "reason": "days_to_expiry_30d missing from frame"},
    },
}

_SUMMARY = {"symbols": 217, "rows_updated": 250_000, "skipped": [], "columns": ["dex", "gex"]}


@pytest.fixture(autouse=True)
def _no_progress_env(monkeypatch):
    """Keep the env clean: unset MKTMTM_NO_PROGRESS unless a test sets it."""
    monkeypatch.delenv("MKTMTM_NO_PROGRESS", raising=False)


# ───────────────────────────── log path (degraded) ──────────────────────────


def test_summary_table_logs_lines_when_no_progress(monkeypatch, caplog) -> None:
    """MKTMTM_NO_PROGRESS=1 → summary renders as log lines, not a rich table."""
    monkeypatch.setenv("MKTMTM_NO_PROGRESS", "1")
    with caplog.at_level(logging.INFO, logger="mktmtm.gex_pipeline"):
        print_summary_table(_SUMMARY)
    assert any("gex-features summary" in r.message for r in caplog.records)
    assert any("rows updated" in r.message and "250000" in r.message for r in caplog.records)


def test_results_table_logs_lines_when_no_progress(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MKTMTM_NO_PROGRESS", "1")
    with caplog.at_level(logging.INFO, logger="mktmtm.gex_pipeline"):
        print_results_table(_RESULT)
    assert any("test-gex results" in r.message for r in caplog.records)
    assert any("H1 Welch t / p" in r.message for r in caplog.records)


# ───────────────────────────── rich path (explicit) ─────────────────────────


def test_summary_table_renders_rich_table(monkeypatch, capsys) -> None:
    """Rich path: the summary renders as a rich Table (covers CI's blind spot)."""
    monkeypatch.setattr("mktmtm.gex_pipeline.use_rich", lambda: True)
    print_summary_table(_SUMMARY)
    out = capsys.readouterr().out
    assert "gex-features summary" in out
    assert "rows updated" in out
    assert "250000" in out


def test_results_table_renders_rich_table(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mktmtm.gex_pipeline.use_rich", lambda: True)
    print_results_table(_RESULT)
    out = capsys.readouterr().out
    assert "test-gex results" in out
    assert "H1 Welch t / p" in out
    assert "robustness: liquidity_filter" in out
    assert "blocked" in out


# ───────────────────────────── CLI wiring + progress ────────────────────────


def test_gex_features_cli_forwards_flags_and_prints_summary(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_recompute(conn, symbols=None, limit_dates=None):
        captured["symbols"] = symbols
        captured["limit_dates"] = limit_dates
        return dict(_SUMMARY)

    monkeypatch.setattr("mktmtm.gex_features.recompute_dealer_aggregates", fake_recompute)
    result = runner.invoke(
        app,
        ["gex-features", "--limit-symbols", "RELIANCE,TCS", "--limit-dates", "30"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["symbols"] == ["RELIANCE", "TCS"]
    assert captured["limit_dates"] == 30
    assert "gex-features summary" in result.stdout


def test_gex_features_cli_rich_path(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mktmtm.gex_features.recompute_dealer_aggregates", lambda conn, symbols=None, limit_dates=None: dict(_SUMMARY))
    # Force rich even though the test runner is not a TTY.
    monkeypatch.setattr("mktmtm.gex_pipeline.use_rich", lambda: True)
    result = runner.invoke(app, ["gex-features"])
    assert result.exit_code == 0, result.stdout
    assert "rows updated" in result.stdout


def test_test_gex_cli_forwards_flags_and_writes_json(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    out = tmp_path / "gex_h1.json"

    def fake_run_exit_test(conn, out_path, symbols=None, limit_dates=None, run_model_if_signal=True):
        captured["out_path"] = out_path
        captured["symbols"] = symbols
        captured["limit_dates"] = limit_dates
        captured["run_model_if_signal"] = run_model_if_signal
        out_path.write_text("{}\n")  # the real runner writes the JSON
        return dict(_RESULT)

    monkeypatch.setattr("mktmtm.ml.gex_hypothesis.run_exit_test", fake_run_exit_test)
    result = runner.invoke(
        app,
        ["test-gex", "--limit-symbols", "NIFTY", "--limit-dates", "20", "--no-model", "--out", str(out)],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["symbols"] == ["NIFTY"]
    assert captured["limit_dates"] == 20
    assert captured["run_model_if_signal"] is False
    assert captured["out_path"] == out
    assert out.exists()
    assert "test-gex results" in result.stdout


def test_test_gex_cli_reports_missing_data(monkeypatch) -> None:
    from mktmtm.ml.gex_hypothesis import MissingDataError

    def fake_run_exit_test(conn, out_path, symbols=None, limit_dates=None, run_model_if_signal=True):
        raise MissingDataError("ml_dataset is missing columns Phases 2-5 have not landed yet: gex")

    monkeypatch.setattr("mktmtm.ml.gex_hypothesis.run_exit_test", fake_run_exit_test)
    result = runner.invoke(app, ["test-gex"])
    assert result.exit_code == 1
    assert "missing columns" in result.stderr


def test_gex_pipeline_cli_forwards_flags_and_skip(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr("mktmtm.gex_pipeline.run_gex_pipeline", fake_run)
    result = runner.invoke(
        app,
        [
            "gex-pipeline",
            "--limit-symbols", "RELIANCE,TCS",
            "--limit-dates", "10",
            "--skip", "preparse,etl",
            "--no-model",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["limit_symbols"] == "RELIANCE,TCS"
    assert captured["limit_dates"] == 10
    assert captured["run_model"] is False
    assert captured["skip"] == {"preparse", "etl"}
