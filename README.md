# Options Wheel Trading Strategy

[![Cloud Run](https://img.shields.io/badge/Google%20Cloud-Run-blue)](https://cloud.google.com/run)
[![Python](https://img.shields.io/badge/Python-3.11+-green)](https://python.org)
[![License](https://img.shields.io/badge/License-Private-red)]()

## Overview

A fully automated options wheel trading strategy deployed on Google Cloud Run with continuous deployment.

## 🚀 Quick Start

### Cloud Deployment (Production)
The strategy runs automatically on Google Cloud, driven by Cloud Scheduler:
- **Service URL**: https://options-wheel-strategy-omnlacz6ia-uc.a.run.app
- **Schedule**: `/scan` hourly at :00 (10:00–15:00 ET), `/run` at :15,
  `/monitor` at :55, `/roll` daily at 15:30 ET, `/regression` at :45
- **Mode**: Paper trading (safe default)

See `docs/CLAUDE.md` for the architecture that actually runs and `docs/gates.md`
for the full control inventory.

### Local Development
```bash
# Clone and setup
git clone https://github.com/memon1987/agentic_options_wheel.git
cd options_wheel

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Alpaca API credentials

# Scan for opportunities (read-only; there is no CLI trade-execution command)
python main.py --command scan
```

## 📁 Project Structure

```
├── src/                    # Core strategy code (production)
│   ├── strategy/          # Options wheel implementation
│   ├── risk/              # RiskManager (validate_roll — the roller's gate)
│   ├── api/               # Alpaca API integration
│   ├── backtesting/       # Backtesting framework
│   ├── data/              # Data management and scanning
│   └── utils/             # Configuration and utilities
│
├── deploy/                # Cloud deployment configuration
│   ├── cloud_run_server.py # Production web server
│   ├── monitoring/        # Health checks and performance dashboard
│   └── kubernetes/        # K8s configs (optional)
│
├── scripts/               # Standalone utility scripts
│   └── testing/           # Manual test scripts (test_*.py)
│
├── tools/                 # Operational tools
│   ├── backtesting/       # Backtest runners
│   ├── deployment/        # Deployment utilities
│   ├── monitoring/        # Emergency stop, maintenance
│   └── testing/           # Integration test utilities
│
├── tests/                 # Unit tests (pytest)
├── config/                # Strategy configuration (settings.yaml)
├── docs/                  # Documentation
├── examples/              # Example usage and demos
├── research/              # Experimental features
│   └── experiments/       # Active research projects
│
├── main.py                # Local entry point
├── setup.py               # Package setup
└── cloudbuild.yaml        # CI/CD pipeline
```

## 🎯 Strategy Features

### Options Wheel Implementation
- **Cash-secured puts** → Assignment → **Covered calls**
- **7-day DTE** for rapid theta decay
- **Conservative deltas** — puts 0.10–0.20, calls 0.15–0.25 (FC-029)
- **Quality stock universe** (AAPL, MSFT, GOOGL, etc.)

### Risk Management
- **Cost-basis floor** — never write a call below the share cost basis
  (fails closed, sourced from Alpaca `avg_entry_price`)
- **Earnings gate** — puts blocked within 2 days of earnings; calls may not
  expire on or after the earnings date
- **One option position per underlying**, enforced at scan and selection
- **Conservative position sizing** — 1 contract per put; calls sized by
  available shares
- Gap-risk controls were **removed** (FC-069): the study behind them said not
  to arm the gate. Full inventory: `docs/gates.md`

### Cloud Infrastructure
- **Serverless deployment** with scale-to-zero cost optimization
- **Automated CI/CD** with testing and deployment pipeline
- **Comprehensive monitoring** with health checks and alerting
- **Enterprise security** with secret management and authentication

## 🛠️ Development

### Scripts
```bash
# Deployment (routine deploys are Cloud Build on push — see cloudbuild.yaml)
./tools/deployment/deploy.sh              # Deploy to production

# Maintenance
./tools/monitoring/maintenance.sh daily   # Daily health check
./tools/monitoring/maintenance.sh weekly  # Weekly review
./tools/monitoring/maintenance.sh monthly # Monthly analysis

# Emergency controls
./tools/monitoring/emergency_stop.sh      # Stop all trading
./tools/monitoring/resume_trading.sh      # Resume operations
```

Faster, deploy-free kill switches also exist as env levers: `ROLLER_ENABLED`,
`EARNINGS_ENABLED` (`gcloud run services update --update-env-vars`, **never**
`--set-env-vars`).

### Testing
```bash
# Run test suite
pytest tests/ -v

# Test specific modules
pytest tests/test_config.py -v
pytest tests/test_risk_manager.py -v

# Test deployment
python tools/testing/test_deployment.py
```

### Monitoring
```bash
# Health check
python deploy/monitoring/health_monitor.py

# Build monitoring
python tools/development/monitor_build.py
```

## 📊 Configuration

Strategy parameters are configured in `config/settings.yaml`:

- **Target DTE**: 7 days for rapid theta decay
- **Delta ranges**: puts `[0.10, 0.20]`, calls `[0.15, 0.25]`
- **Position sizing**: `max_position_size: 0.35` × portfolio as a feasibility
  bound; puts execute at 1 contract, calls at `available_shares // 100`.
  There is no absolute per-ticker dollar cap (FC-069 operator decision)
- **Stock universe**: high-quality, liquid stocks; `$10–400` price band,
  ≥ 2M average volume

## 🔐 Security

- **Paper trading by default** for safety
- **API credentials** stored in Google Secret Manager
- **Authenticated endpoints** with OIDC tokens
- **No secrets in repository** - all sensitive data externalized

## 📈 Performance

### Expected Costs
- **Monthly cloud costs**: <$1.00
- **Resource usage**: 512MB memory, scale-to-zero
- **Build time**: 7-13 minutes per deployment

### Monitoring Metrics
- Execution success rates
- Position P&L tracking
- API response times

## 🚨 Emergency Procedures

**Stop Trading Immediately**:
```bash
./tools/monitoring/emergency_stop.sh
```

**Resume Trading**:
```bash
./tools/monitoring/resume_trading.sh
```

**Check Health**:
```bash
python deploy/monitoring/health_monitor.py
```

## 📚 Documentation

- [`docs/deployment/DEPLOYMENT_SUMMARY.md`](docs/deployment/DEPLOYMENT_SUMMARY.md) - Complete deployment guide
- [`docs/deployment/PRODUCTION_CONFIG.md`](docs/deployment/PRODUCTION_CONFIG.md) - Strategy configuration details
- [`docs/operations/MAINTENANCE.md`](docs/operations/MAINTENANCE.md) - Operations procedures
- [`docs/deployment/GITHUB_DEPLOYMENT_SETUP.md`](docs/deployment/GITHUB_DEPLOYMENT_SETUP.md) - CI/CD setup guide

## 🎯 Next Steps

1. **Monitor paper trading** for 4-6 weeks
2. **Review the gate inventory** (`docs/gates.md`) after any policy change
3. **Analyze performance metrics** and adjust parameters
4. **Consider live trading** after thorough validation

## ⚡ Quick Commands

```bash
# Check service status
curl https://options-wheel-strategy-omnlacz6ia-uc.a.run.app/health

# Manual strategy execution (there is no CLI execute command — use the endpoints)
curl -X POST -H "X-API-Key: $STRATEGY_API_KEY" https://options-wheel-strategy-omnlacz6ia-uc.a.run.app/scan
curl -X POST -H "X-API-Key: $STRATEGY_API_KEY" https://options-wheel-strategy-omnlacz6ia-uc.a.run.app/run

# View recent logs
gcloud logging read 'resource.type="cloud_run_revision"' --limit=10

# Monitor builds
gcloud builds list --limit=5
```

---

**Status**: ✅ Production Ready - Paper Trading Active
**Deployed**: Google Cloud Run with automated CI/CD
**Next Review**: Monitor for 4-6 weeks before live trading consideration

*Built with ❤️ using Claude Code*