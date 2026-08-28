#!/bin/bash
# FC-012 follow-up: drop the four inert v1 BigQuery tables.
#
# Background: FC-012 (PR #8, merged 2026-04-24) migrated the dashboard's
# trade-execution data source from runtime-logged BQ tables to Alpaca-sourced
# tables/views. The v1 tables (trades, wheel_cycles, position_snapshots,
# order_statuses) are no longer read or written by any code, but still exist
# in BigQuery with their historical data.
#
# Safety check: for each table this script compares the total row count against
# the count of rows older than 24 hours. If the counts differ, at least one row
# was written in the last 24 hours — the table is still live and the drop is
# skipped. Only tables with zero writes in the last 24 hours are dropped.
#
# Usage:
#   bash scripts/fc012_cleanup_v1_tables.sh
#
# Requirements: Google Cloud SDK installed; authenticated with an identity that
#   has BigQuery Data Owner on gen-lang-client-0607444019.
#   Verify with: gcloud auth list && bq show gen-lang-client-0607444019:options_wheel

set -euo pipefail

PROJECT="gen-lang-client-0607444019"
DATASET="options_wheel"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# check_and_drop TABLE TS_FIELD
#   TABLE    : table name within DATASET
#   TS_FIELD : "timestamp" (TIMESTAMP column) or "date" (DATE column)
check_and_drop() {
    local table="$1"
    local ts_field="$2"
    local bq_ref="${PROJECT}:${DATASET}.${table}"
    local bq_id="${PROJECT}.${DATASET}.${table}"

    echo ""
    echo "--- ${table} ---"

    # Idempotency: skip gracefully if the table no longer exists.
    if ! bq show --format=none "${bq_ref}" 2>/dev/null; then
        echo "  SKIP: table does not exist (already dropped or never created)."
        return 0
    fi

    # Total row count.
    current=$(bq query --use_legacy_sql=false --format=csv --quiet \
        "SELECT COUNT(*) AS n FROM \`${bq_id}\`" | tail -1)

    # Row count for rows written more than 24 hours ago (should equal current
    # for a truly inert table).
    if [[ "${ts_field}" == "date" ]]; then
        # position_snapshots uses DATE partitioning on the `date` column.
        day_ago=$(bq query --use_legacy_sql=false --format=csv --quiet \
            "SELECT COUNT(*) AS n FROM \`${bq_id}\` WHERE date < DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)" \
            | tail -1)
    else
        day_ago=$(bq query --use_legacy_sql=false --format=csv --quiet \
            "SELECT COUNT(*) AS n FROM \`${bq_id}\` WHERE ${ts_field} < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)" \
            | tail -1)
    fi

    echo "  Total rows        : ${current}"
    echo "  Rows before 24h   : ${day_ago}"

    if [[ "${current}" -gt "${day_ago}" ]]; then
        local new_rows=$(( current - day_ago ))
        echo "  WARNING: ${new_rows} row(s) written in the last 24h — SKIPPING DROP."
        echo "           Investigate whether a writer is still active, then rerun."
        return 0
    fi

    echo "  No writes in the last 24h. Dropping..."
    bq rm -f -t "${bq_ref}"
    echo "  DROPPED: ${table}"
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

echo "============================================"
echo "FC-012 v1 BigQuery Table Cleanup"
echo "Project : ${PROJECT}"
echo "Dataset : ${DATASET}"
echo "Targets : trades, wheel_cycles, position_snapshots, order_statuses"
echo "============================================"

if ! command -v bq &>/dev/null; then
    echo "ERROR: 'bq' command not found."
    echo "Install Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

echo ""
echo "Authenticated as: $(gcloud config get-value account 2>/dev/null || echo '(unknown)')"

# ---------------------------------------------------------------------------
# Per-table checks
# ---------------------------------------------------------------------------
# trades and wheel_cycles are partitioned on the `timestamp` column.
# position_snapshots is partitioned on the `date` column.
# order_statuses is partitioned on the `timestamp` column.

check_and_drop "trades"             "timestamp"
check_and_drop "wheel_cycles"       "timestamp"
check_and_drop "position_snapshots" "date"
check_and_drop "order_statuses"     "timestamp"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "============================================"
echo "Cleanup run complete."
echo "Tables confirmed as the new Alpaca-sourced"
echo "replacements remain untouched:"
echo "  options_wheel.trades_from_activities"
echo "  options_wheel.equity_history_from_alpaca"
echo "  options_wheel.trades_with_outcomes (view)"
echo "  options_wheel.wheel_cycles_from_activities (view)"
echo "============================================"
