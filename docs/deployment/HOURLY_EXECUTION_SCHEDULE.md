# Hourly Execution Schedule

**Implemented**: October 2, 2025 12:35am ET
**Update**: Changed from daily to hourly execution for more trading opportunities

---

## Schedule Overview

### Trading Hours: 10:00am - 3:15pm ET (Weekdays only)

**Pattern**:
- **:00** - Market scan for opportunities
- **:15** - Execute best opportunities from scan

**Frequency**: 6 scans + 6 executions = **12 jobs per trading day**

---

## Detailed Schedule

### Morning Session

| Time (ET) | Job Type | Job Name | Action |
|-----------|----------|----------|--------|
| **10:00am** | Scan | `scan-10am` | Scan market for put/call opportunities |
| **10:15am** | Execute | `execute-10-15am` | Execute best opportunities from 10am scan |
| **11:00am** | Scan | `scan-11am` | Scan market for put/call opportunities |
| **11:15am** | Execute | `execute-11-15am` | Execute best opportunities from 11am scan |

### Midday Session

| Time (ET) | Job Type | Job Name | Action |
|-----------|----------|----------|--------|
| **12:00pm** | Scan | `scan-12pm` | Scan market for put/call opportunities |
| **12:15pm** | Execute | `execute-12-15pm` | Execute best opportunities from 12pm scan |
| **1:00pm** | Scan | `scan-1pm` | Scan market for put/call opportunities |
| **1:15pm** | Execute | `execute-1-15pm` | Execute best opportunities from 1pm scan |

### Afternoon Session

| Time (ET) | Job Type | Job Name | Action |
|-----------|----------|----------|--------|
| **2:00pm** | Scan | `scan-2pm` | Scan market for put/call opportunities |
| **2:15pm** | Execute | `execute-2-15pm` | Execute best opportunities from 2pm scan |
| **3:00pm** | Scan | `scan-3pm` | Scan market for put/call opportunities |
| **3:15pm** | Execute | `execute-3-15pm` | Execute best opportunities from 3pm scan |

---

## Why Hourly Execution?

### Benefits:

**1. More Opportunities to Capture Premiums**
- 6 scans per day vs 1 previously
- Catch opportunities at different times
- Adapt to intraday price movements

**2. Better Price Discovery**
- Market conditions change throughout the day
- Volatility varies by time of day
- Can capitalize on mid-day opportunities

**3. Reduced Timing Risk**
- Not dependent on single scan time
- Diversified execution across the day
- Better average entry prices

**4. Increased Volume Potential**
- More frequent scans = more trades
- Especially valuable with 14-stock portfolio
- Lower-priced stocks (F, PFE, KMI, VZ) enable higher volume

### Trade-offs Considered:

**Cloud Run Costs**:
- 12 jobs/day × 5 days = 60 invocations/week
- Previous: 3 jobs/day × 5 days = 15 invocations/week
- **4x increase in invocations**
- Still within free tier limits (2M requests/month)

**Execution Overlap**:
- 15-minute window between scan and execute
- Sufficient time for scan to complete
- Prevents execution without fresh scan data

---

## Execution Flow

### Typical Hourly Cycle:

**10:00am - SCAN** (`/scan` endpoint)
```
1. Fetch current market data (IEX + SIP)
2. Scan all 14 stocks for opportunities
3. Apply gap risk filters
4. Rank by attractiveness score
5. Store top opportunities (in-memory)
```

**10:15am - EXECUTE** (`/run` endpoint)
```
1. Retrieve opportunities from 10am scan
2. Check account buying power
3. Execute highest-ranked opportunity
4. Update portfolio state
5. Log enhanced events to BigQuery
```

**Repeat every hour...**

---

## Data Feed Timing

### With 20-Minute SIP Buffer:

**Example at 2:00pm ET scan**:
```
Current time:     2:00:00 PM ET
Buffer:         - 0:20:00
─────────────────────────────
Query end_date:  1:40:00 PM ET

SIP available:   1:45 PM ET (now - 15 min)
Our query:       1:40 PM ET ✅

Result: 5-minute safety margin
```

**This works perfectly for hourly scans!**

---

## Cost Analysis

### Cloud Run Costs (Estimated):

**Invocations**:
- Scans: 6/day × 22 trading days = 132/month
- Executions: 6/day × 22 trading days = 132/month
- Other jobs: ~30/month
- **Total: ~300 invocations/month**

**Free Tier**: 2M invocations/month
**Cost**: $0 (well within free tier)

**CPU/Memory**:
- Each job: ~2-5 seconds execution
- 512MB memory allocation
- Minimal compute cost (<$1/month estimated)

**Total Estimated Cost**: $0-1/month 💰

---

## Monitoring

### Success Metrics:

**Per Job**:
```bash
# Check scan success
gcloud logging read 'resource.labels.service_name="options-wheel-strategy"
  AND textPayload:"POST /scan"
  AND textPayload:"200"'
  --limit=10 --freshness=2h

# Check execution success
gcloud logging read 'resource.labels.service_name="options-wheel-strategy"
  AND textPayload:"POST /run"
  AND textPayload:"200"'
  --limit=10 --freshness=2h
```

**Daily Summary**:
```bash
# Count successful scans today
gcloud logging read 'resource.labels.service_name="options-wheel-strategy"
  AND textPayload:"POST /scan"
  AND textPayload:"200"'
  --format=json --freshness=24h | grep -c "POST /scan"
```

**Expected**: 6 scans + 6 executions = 12 successful jobs per trading day

---

## Alert Thresholds

### Daily Checks:

| Metric | Threshold | Alert |
|--------|-----------|-------|
| **Successful Scans** | < 4 per day | ⚠️ Review logs |
| **Successful Executions** | < 3 per day | ⚠️ Check buying power |
| **Subscription Errors** | > 0 | 🚨 Feed issue |
| **API Errors** | > 10 per day | ⚠️ Check API status |

---

## Job Management Commands

### List all jobs:
```bash
gcloud scheduler jobs list --location=us-central1
```

### Pause hourly trading (keep maintenance):
```bash
for job in scan-10am scan-11am scan-12pm scan-1pm scan-2pm scan-3pm \
           execute-10-15am execute-11-15am execute-12-15pm \
           execute-1-15pm execute-2-15pm execute-3-15pm; do
  gcloud scheduler jobs pause $job --location=us-central1
done
```

### Resume hourly trading:
```bash
for job in scan-10am scan-11am scan-12pm scan-1pm scan-2pm scan-3pm \
           execute-10-15am execute-11-15am execute-12-15pm \
           execute-1-15pm execute-2-15pm execute-3-15pm; do
  gcloud scheduler jobs resume $job --location=us-central1
done
```

### Trigger manual test:
```bash
gcloud scheduler jobs run scan-10am --location=us-central1
```

---

## Schedule Changes

### Old Schedule (Before Oct 2, 2025):
```
9:00am UTC (5am ET)   - morning-market-scan
4:00pm UTC (12pm ET)  - midday-strategy-execution
7:00pm UTC (3pm ET)   - afternoon-position-check

Total: 3 jobs/day
```

### New Schedule (After Oct 2, 2025):
```
10:00am ET - scan-10am + execute-10-15am
11:00am ET - scan-11am + execute-11-15am
12:00pm ET - scan-12pm + execute-12-15pm
1:00pm ET  - scan-1pm  + execute-1-15pm
2:00pm ET  - scan-2pm  + execute-2-15pm
3:00pm ET  - scan-3pm  + execute-3-15pm

Total: 12 jobs/day
```

**Change**: **4x increase in trading frequency**

---

## Next Steps

**Immediate**:
1. ✅ Jobs created and enabled
2. ⏳ First execution: Tomorrow 10:00am ET

**Tomorrow (Oct 2)**:
1. Monitor first hourly cycle (10:00am scan + 10:15am execute)
2. Check logs for successful execution
3. Verify enhanced logging appears
4. Confirm no subscription errors

**Week 1**:
1. Review daily execution counts
2. Analyze premium collection vs old schedule
3. Monitor Cloud Run costs
4. Optimize if needed

---

**Schedule is now LIVE and ready for tomorrow's trading! 🚀**
