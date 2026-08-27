"""
Registry of controllable ares-* systemd units on hostinger-vps.

Pattern verified against HKUDS/AI-Trader (service/server/tasks.py's
BACKGROUND_TASK_REGISTRY) -- a single source-of-truth dict, not scattered
strings, so the API and worker never drift out of sync on what's real.

Category matters for safety: EXECUTION units place real orders or sign
real transactions. They can never be toggled through the plain
start/stop endpoints -- only through /daemons/{name}/toggle with
require_approval=True acknowledged explicitly by the caller.
"""

from enum import Enum


class Category(str, Enum):
    INTEL = "intel"          # scanning/analytics, safe to freely toggle
    BRIDGE = "bridge"        # connects to Vantage/other pillars
    DASHBOARD = "dashboard"  # read-only display, safe to toggle
    BACKUP = "backup"        # scheduled backup jobs
    EXECUTION = "execution"  # places orders / signs transactions -- gated


# "local" = the box ares-control's own worker process runs on (Contabo, as
# of the 2026-08-27 migration off hostinger-vps). Everything else is
# reached over SSH -- see systemd_control.py. Default is "hostinger-vps"
# since that's still where the vast majority of the fleet actually runs;
# only list a unit here if it's NOT there.
DAEMON_HOSTS: dict[str, str] = {
    "ares-kanban-core.service": "local",
}


def host_for(unit_name: str) -> str:
    return DAEMON_HOSTS.get(unit_name, "hostinger-vps")


# Real unit names, confirmed live via `systemctl list-unit-files` on
# hostinger-vps 2026-08-25. Keep this in sync by re-running that command --
# do not hand-edit names without verifying against the box.
DAEMON_REGISTRY: dict[str, Category] = {
    "ares-advanced-analytics.service": Category.INTEL,
    "ares-alpha-hunter.service": Category.INTEL,
    "ares-alpha-sources.service": Category.INTEL,
    "ares-atomic-daemon.service": Category.INTEL,
    "ares-backup-gitea.service": Category.BACKUP,
    "ares-backup-vantage.service": Category.BACKUP,
    "ares-bridge.service": Category.BRIDGE,
    "ares-council-dashboard.service": Category.DASHBOARD,
    "ares-council.service": Category.INTEL,
    "ares-dashboard.service": Category.DASHBOARD,
    "ares-deepseek-intel.service": Category.INTEL,
    "ares-degen-alpha-fusion.service": Category.INTEL,
    "ares-degen-loop.service": Category.INTEL,
    "ares-frankenstream.service": Category.INTEL,
    "ares-control-notify.service": Category.BRIDGE,
    "ares-axiom-dashboard.service": Category.DASHBOARD,
    "ares-freqtrade-bridge.service": Category.BRIDGE,
    "ares-freqtrade.service": Category.EXECUTION,
    "ares-jupiter-signer.service": Category.EXECUTION,
    "ares-loom.service": Category.INTEL,
    "ares-metabase-mirror.service": Category.BACKUP,
    "ares-ogun-multiscan.service": Category.INTEL,
    "ares-ogun-orchestrator.service": Category.INTEL,
    "ares-omokoda-birth.service": Category.BRIDGE,
    "ares-omokoda-buzz-acp.service": Category.BRIDGE,
    "ares-omokoda-buzz-acp@.service": Category.BRIDGE,
    "ares-onion-scanner.service": Category.INTEL,
    "ares-opencode-runner.service": Category.INTEL,
    "ares-playwright.service": Category.INTEL,
    "ares-poison-radar.service": Category.INTEL,
    "ares-polymarket-bridge.service": Category.BRIDGE,
    "ares-poolhealth.service": Category.DASHBOARD,
    "ares-pumpfun-launch-radar.service": Category.INTEL,
    "ares-pumpfun-scalp-manager.service": Category.EXECUTION,
    "ares-pumpfun-tier-scanner.service": Category.INTEL,
    "ares-pumpfun-trader.service": Category.EXECUTION,
    "ares-pumpfun-wallet-intel.service": Category.INTEL,
    "ares-rpc.service": Category.BRIDGE,
    "ares-sango-relay.service": Category.BRIDGE,
    "ares-seemplify.service": Category.INTEL,
    "ares-signal-aggregator.service": Category.INTEL,
    "ares-signal-fusion-picks.service": Category.INTEL,
    "ares-signal-fusion.service": Category.INTEL,
    "ares-signal-gate.service": Category.INTEL,
    "ares-social-tracker.service": Category.INTEL,
    "ares-specialist-worker.service": Category.INTEL,
    "ares-stack.service": Category.INTEL,
    "ares-strategy-lab-hub.service": Category.INTEL,
    "ares-kanban-core.service": Category.INTEL,
    "ares-stix-ingester.service": Category.INTEL,
    "ares-stix-scanner.service": Category.INTEL,
    "ares-stix-webhook.service": Category.BRIDGE,
    "ares-strategy-bots.service": Category.EXECUTION,
    "ares-strategy-executor-30.service": Category.EXECUTION,
    "ares-strategy-executor-60.service": Category.EXECUTION,
    "ares-strix-runner.service": Category.INTEL,
    "ares-stt-relay.service": Category.BRIDGE,
    "ares-swarm-orchestrator.service": Category.INTEL,
    "ares-tiered-intel.service": Category.INTEL,
    "ares-tracked-wallet-balance.service": Category.INTEL,
    "ares-trade-outcome-learner.service": Category.INTEL,
    "ares-trader-base.service": Category.EXECUTION,
    "ares-trader-hyperliquid.service": Category.EXECUTION,
    "ares-trader-polymarket.service": Category.EXECUTION,
    "ares-trader-sui.service": Category.EXECUTION,
    "ares-trader.service": Category.EXECUTION,
    "ares-trading-agents.service": Category.EXECUTION,
    "ares-unified-ingester.service": Category.INTEL,
    "ares-vantage-buzz-acp.service": Category.BRIDGE,
    "ares-vantage-predictor.service": Category.BRIDGE,
    "ares-vantage-publisher.service": Category.BRIDGE,
    "ares-vantage-signal-bridge.service": Category.BRIDGE,
    "ares-vibe-mcp-server.service": Category.BRIDGE,
    "ares-wallet-intel.service": Category.INTEL,
    "ares-wallet-learner.service": Category.INTEL,
    "ares-wigolo.service": Category.INTEL,
    "ares-worldmonitor-bridge.service": Category.BRIDGE,
}


def requires_approval(unit_name: str) -> bool:
    return DAEMON_REGISTRY.get(unit_name) == Category.EXECUTION


def all_units() -> list[str]:
    return list(DAEMON_REGISTRY.keys())
