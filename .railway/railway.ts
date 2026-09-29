// Catalyst Retest Lab on Railway (package cloud, 2026-09-27). PAPER TRADING ONLY.
//
// Railway Infrastructure as Code (https://docs.railway.com/infrastructure-as-code): the Railway
// CLI (5.42.1 or newer) evaluates this file with Node 22 or newer and the SDK pinned in
// .railway/package.json, compares it with the linked environment and changes Railway only
// after `railway config apply` is confirmed. Railway does not read this file during deploys.
// Omitting a service or a variable here means deleting it on the next apply, so every secret
// is declared with preserve(): its value is set outside this file (scripts/cloud_secrets.py
// for generated ones, the owner for the three external keys) and applies never overwrite it.
// No secret value is written in this file. Steps: docs/RAILWAY-DEPLOYMENT.md.
//
// Services: postgres (Railway's managed Postgres; the SDK 3.11.0 helper deploys
// ghcr.io/railwayapp-templates/postgres-ssl:18, which Dockerfile.managed's PG_MAJOR matches),
// trader (the managed engine: one executor), ops (watchdog, audit checkpoints, backups),
// experiment (the public read-only experiment page, package experiment-page), jobs (the nightly
// learning cron, package learning-app) and, only while CATALYST_PROVISION is set in the owner's
// shell, the one-off provision service.

import { readFileSync } from "node:fs";
import { defineRailway, postgres, preserve, project, service, volume } from "railway/iac";

// Engine settings: exactly the private config v2 example's `environment` map (the same names
// and values the Mac launcher injects), minus secrets and Mac-only keys, plus four cloud
// overrides. tests/test_railway_spec.py keeps this derivation honest.
const example = JSON.parse(
  readFileSync(new URL("../deploy/private-paper.example.json", import.meta.url), "utf8"),
) as { environment: Record<string, string> };
const NOT_LITERAL = new Set([
  "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", // Sealed variables the owner enters.
  "TYPESAFE_ENV_FILE", // Mac only; the cloud refuses it and reads TYPESAFE_API_KEY.
  "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL", // Composed below from references.
]);
const engine: Record<string, string> = {};
for (const [name, value] of Object.entries(example.environment)) {
  if (!NOT_LITERAL.has(name)) engine[name] = value;
}
Object.assign(engine, {
  CATALYST_ENVIRONMENT: "railway",
  MANAGED_HTTP_PORT: "8080",
  JEV_CREDENTIAL_SLOT: "paper-railway",
  // Owner decision: Jev monitors every trade every minute (maintenance, the continue-or-exit
  // reviews, exit flags). DISABLED would send Jev nothing: no maintenance, every review exits
  // at T and every exit flag ends unanswered. Switching it off: docs/RAILWAY-DEPLOYMENT.md 9.
  // The trade window (MANAGED_CRYPTO_WINDOW_JSON, package review-window) comes from the
  // example like every other engine setting; the cloud profile requires it.
  MANAGED_MANAGEMENT_REVIEWS: "ENABLED",
});

// Role connections over Railway's private network, TLS required (the Postgres image serves
// TLS; the private network is WireGuard as well). Each password is a variable of the service
// that uses it; the provision service reads the same variables by reference.
const DATABASE_HOST = "${{postgres.RAILWAY_PRIVATE_DOMAIN}}";
function dsn(role: string, password: string): string {
  return `postgresql://${role}:${password}@${DATABASE_HOST}:5432/catalyst_lab?sslmode=require`;
}

const DOCKERFILE = { builder: "DOCKERFILE", dockerfilePath: "Dockerfile.managed" } as const;
// Railway's default restart policy is ON_FAILURE with 10 retries
// (https://docs.railway.com/deployments/restart-policy), and Railway stores a default as null,
// so declaring "ON_FAILURE" left a permanent false difference in every `railway config plan`
// (first apply, 2026-09-28). The policy is therefore left at its default: ON_FAILURE restarts a
// lost lease (exit 75), and the crash-loop breaker's exit 0 stays stopped. Only the retry
// count differs from the default. No overlap and 30 seconds of draining: the old trader stops
// (and releases its lease) before the new one starts, which the attached volume makes Railway
// enforce as well.
const ONE_EXECUTOR = {
  restartPolicyMaxRetries: 100,
  overlapSeconds: 0,
  drainingSeconds: 30,
} as const;
// The region Railway placed the volumes in at the first apply (2026-09-28). A volume without
// one plans as "region -> null" on every later apply, which Railway treats as destructive.
const VOLUME_REGION = "us-east4-eqdc4a";
const PROVISION_MODES = ["initial", "rotate-passwords", "migrate"];
// The guarded migration (docs/RAILWAY-DEPLOYMENT.md 7.8): the two schema versions and the fresh
// backup's reference (printed by `cloud_entry backup` on ops) come from the owner's shell, are
// checked here and again by the entrypoint and the migration (ledger_ops.BACKUP_REFERENCE).
const MIGRATE_VERSION = /^[1-9][0-9]{0,3}$/;
const BACKUP_REFERENCE =
  /^[0-9]{8}T[0-9]{6}Z\.[1-9][0-9]{0,3}\.[1-9][0-9]{0,18}\.[0-9a-f]{64}\.[0-9a-f]{64}$/;

function provisionStart(mode: string): string {
  const start = `python -m catalyst_lab.cloud_entry provision ${mode}`;
  if (mode !== "migrate") return start;
  const from = process.env.CATALYST_MIGRATE_EXPECT_CURRENT ?? "";
  const to = process.env.CATALYST_MIGRATE_TARGET ?? "";
  const backup = process.env.CATALYST_MIGRATE_BACKUP ?? "";
  if (!MIGRATE_VERSION.test(from) || !MIGRATE_VERSION.test(to) || !BACKUP_REFERENCE.test(backup)) {
    throw new Error("CATALYST_PROVISION=migrate needs CATALYST_MIGRATE_EXPECT_CURRENT, " +
      "CATALYST_MIGRATE_TARGET and CATALYST_MIGRATE_BACKUP (the backup reference)");
  }
  return `${start} --expect-current ${from} --target ${to} --backup ${backup}`;
}

export default defineRailway(() => {
  const mode = process.env.CATALYST_PROVISION ?? "";
  if (mode !== "" && !PROVISION_MODES.includes(mode)) {
    throw new Error("CATALYST_PROVISION must be unset, initial, rotate-passwords or migrate");
  }

  const db = postgres("postgres");
  const traderState = volume("trader-state", { sizeMB: 1024, region: VOLUME_REGION });
  const opsState = volume("ops-state", { sizeMB: 4096, region: VOLUME_REGION });

  const trader = service("trader", {
    build: DOCKERFILE,
    start: "python -m catalyst_lab.cloud_entry trader",
    healthcheck: "/health",
    healthcheckTimeout: 300,
    replicas: 1,
    deploy: ONE_EXECUTOR,
    volumeMounts: { "/data": traderState },
    env: {
      ...engine,
      CATALYST_STATE_DIR: "/data",
      PORT: "8080",
      MANAGED_DATABASE_URL: dsn("catalyst_risk", "${{RISK_DATABASE_PASSWORD}}"),
      JEV_WORKER_DATABASE_URL: dsn("catalyst_jev", "${{JEV_DATABASE_PASSWORD}}"),
      RISK_DATABASE_PASSWORD: preserve(),
      JEV_DATABASE_PASSWORD: preserve(),
      MANAGED_API_TOKEN: preserve(),
      MANAGED_STATUS_TOKEN: preserve(),
      MANAGED_OPERATOR_TOKEN: preserve(),
      MANAGED_AGENT_TOKENS_JSON: preserve(),
      APCA_API_KEY_ID: preserve(),
      APCA_API_SECRET_KEY: preserve(),
      TYPESAFE_API_KEY: preserve(),
    },
  });

  const ops = service("ops", {
    build: DOCKERFILE,
    start: "python -m catalyst_lab.cloud_entry ops",
    replicas: 1,
    deploy: ONE_EXECUTOR,
    volumeMounts: { "/data": opsState },
    env: {
      CATALYST_ENVIRONMENT: "railway",
      CATALYST_STATE_DIR: "/data",
      MANAGED_STATUS_API: "http://${{trader.RAILWAY_PRIVATE_DOMAIN}}:8080",
      AUDIT_DATABASE_URL: dsn("catalyst_app", "${{APP_DATABASE_PASSWORD}}"),
      BACKUP_DATABASE_URL: dsn("catalyst_backup", "${{BACKUP_DATABASE_PASSWORD}}"),
      OPERATOR_DATABASE_URL: dsn("catalyst_operator", "${{OPERATOR_DATABASE_PASSWORD}}"),
      CLOUD_BACKUP_RETENTION_DAYS: "14",
      MANAGED_STATUS_TOKEN: preserve(),
      APP_DATABASE_PASSWORD: preserve(),
      BACKUP_DATABASE_PASSWORD: preserve(),
      OPERATOR_DATABASE_PASSWORD: preserve(),
      PUBLIC_DATABASE_PASSWORD: preserve(),
      CLOUD_NOTIFY_JSON: preserve(),
      NOTIFY_PING_URL: preserve(),
    },
  });

  // Package experiment-page: the public read-only page. Its variables are its read-only
  // connection as catalyst_public (migration 024) and, by reference, the trader's research
  // schedule (for "next run"), so the two never disagree. No broker, Jev or trader secret.
  const experiment = service("experiment", {
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile.experiment" },
    start: "python -m catalyst_lab.experiment_page --host 0.0.0.0",
    healthcheck: "/health",
    replicas: 1,
    // Railway's default restart policy (ON_FAILURE, 10 retries); see ONE_EXECUTOR.
    env: {
      EXPERIMENT_DATABASE_URL: dsn("catalyst_public", "${{ops.PUBLIC_DATABASE_PASSWORD}}"),
      EXPERIMENT_RESEARCH_SCHEDULE_JSON: "${{trader.MANAGED_RESEARCH_SCHEDULE_JSON}}",
    },
  });

  // Package learning-app: the nightly learning jobs as a Railway cron service (owner decision of
  // 2026-09-28: not inside the trader, the only executor). Same image and entrypoint; one run
  // records the shadow outcomes, the maintenance replays, the day's market reality, its
  // scorecard and, after each week, the weekly review, then exits 0. Railway runs a cron
  // service's start command on the schedule, in UTC, and expects it to "execute a task, and
  // terminate as soon as that task is finished"; a run still going when the next is due makes
  // Railway skip that one (https://docs.railway.com/reference/cron-jobs), so the process ends
  // itself after 30 minutes at the latest. 05:30 UTC is after midnight in New York all year
  // (01:30 EDT, 00:30 EST). NEVER restart: a failed run waits for the next schedule instead of
  // repeating (the default ON_FAILURE policy would restart it). Its one secret is the trader's
  // catalyst_risk connection, by reference: no broker or Jev key, no token, no volume.
  const jobs = service("jobs", {
    build: DOCKERFILE,
    start: "python -m catalyst_lab.cloud_entry jobs",
    replicas: 1,
    deploy: { cronSchedule: "30 5 * * *", restartPolicyType: "NEVER" },
    env: {
      CATALYST_ENVIRONMENT: "railway",
      MANAGED_DATABASE_URL: dsn("catalyst_risk", "${{trader.RISK_DATABASE_PASSWORD}}"),
    },
  });

  // One-off provisioner on the private network (never a runtime service): present only while
  // the owner runs `CATALYST_PROVISION=initial railway config apply` (or rotate-passwords, or
  // migrate with its three CATALYST_MIGRATE_* values); the next plain apply deletes it, with
  // its admin connection. A migration sets no password, so it gets none of them.
  const passwords: Record<string, string> = mode === "migrate" ? {} : {
    RISK_DATABASE_PASSWORD: "${{trader.RISK_DATABASE_PASSWORD}}",
    JEV_DATABASE_PASSWORD: "${{trader.JEV_DATABASE_PASSWORD}}",
    APP_DATABASE_PASSWORD: "${{ops.APP_DATABASE_PASSWORD}}",
    BACKUP_DATABASE_PASSWORD: "${{ops.BACKUP_DATABASE_PASSWORD}}",
    OPERATOR_DATABASE_PASSWORD: "${{ops.OPERATOR_DATABASE_PASSWORD}}",
    PUBLIC_DATABASE_PASSWORD: "${{ops.PUBLIC_DATABASE_PASSWORD}}",
  };
  const provision = mode === "" ? [] : [service("provision", {
    build: DOCKERFILE,
    start: provisionStart(mode),
    replicas: 1,
    deploy: { restartPolicyType: "NEVER" },
    env: {
      MIGRATION_DATABASE_URL: "${{postgres.DATABASE_URL}}?sslmode=require",
      ...passwords,
    },
  })];

  return project("catalyst-retest-lab", {
    resources: [db, traderState, opsState, trader, ops, experiment, jobs, ...provision],
  });
});
