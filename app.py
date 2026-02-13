"""
Law Firm Intranet (ELF demo)

Entrypoint module. App code is organized under the `intranet/` package.
"""

from __future__ import annotations

import argparse
import os
import socket
import time
from pathlib import Path

from intranet import create_app
from intranet.cli import create_user, init_db, run_server, seed_demo_data
from intranet.config import env_int
from intranet.jobs.scheduler import DEFAULT_PERIODIC_JOBS, schedule_due_jobs
from intranet.jobs.worker import run_worker_once
from intranet.models import ScheduledJob
from intranet.extensions import db


def _load_dotenv_if_present() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


_load_dotenv_if_present()
app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    run_cmd.add_argument("--port", type=int, default=env_int("PORT", 5000))
    run_cmd.add_argument("--debug", action="store_true", help="Enable Flask debug mode (development only)")

    sub.add_parser("init-db")

    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--password", required=True)
    cu.add_argument("--role", default="lawyer", choices=["admin", "lawyer", "staff", "paralegal"])
    cu.add_argument("--name", default="(Unnamed)")

    seed_cmd = sub.add_parser("seed-demo")
    seed_cmd.add_argument("--password", default="ClientDemo2026!", help="Password applied to all demo users")
    seed_cmd.add_argument("--reset", action="store_true", help="Delete existing data before seeding")

    worker_cmd = sub.add_parser("worker")
    worker_cmd.add_argument("--max-jobs", type=int, default=50, help="Maximum jobs processed in this run")
    worker_cmd.add_argument("--loop", action="store_true", help="Run continuously")
    worker_cmd.add_argument(
        "--sleep-seconds",
        type=int,
        default=env_int("WORKER_LOOP_SLEEP_SECONDS", 5),
        help="Idle sleep between polling cycles in loop mode",
    )
    worker_cmd.add_argument(
        "--worker-id",
        default=f"{socket.gethostname()}-{os.getpid()}",
        help="Stable worker identifier for queue lease records",
    )

    scheduler_cmd = sub.add_parser("scheduler")
    scheduler_cmd.add_argument("--seed-defaults", action="store_true", help="Seed default periodic jobs if missing")
    scheduler_cmd.add_argument("--loop", action="store_true", help="Run continuously")
    scheduler_cmd.add_argument(
        "--sleep-seconds",
        type=int,
        default=env_int("SCHEDULER_LOOP_SLEEP_SECONDS", 30),
        help="Sleep between scheduling cycles in loop mode",
    )

    args = parser.parse_args()

    if args.cmd == "init-db":
        init_db(app)
        print("DB initialized.")
    elif args.cmd == "create-user":
        uid = create_user(app, args.email, args.password, args.role, args.name)
        print(f"Created user id={uid}")
    elif args.cmd == "seed-demo":
        summary = seed_demo_data(app, password=args.password, reset=args.reset)
        print("Demo data seeded:")
        primary_keys = [
            "users",
            "announcements",
            "matters",
            "matter_memberships",
            "tasks",
            "documents",
            "contacts",
            "knowledge_articles",
            "timeline_events",
            "matter_activity",
            "incidents",
            "audit_logs",
        ]
        for key in primary_keys:
            if key in summary:
                print(f"  {key}={summary[key]}")

        extra_keys = sorted(k for k in summary.keys() if k not in set(primary_keys + ["password"]))
        for key in extra_keys:
            print(f"  {key}={summary[key]}")
        print("Login credentials:")
        print("  admin@elf-ai-demo.co.za")
        print("  partner@elf-ai-demo.co.za")
        print("  associate@elf-ai-demo.co.za")
        print("  paralegal@elf-ai-demo.co.za")
        print("  staff@elf-ai-demo.co.za")
        print(f"  password={summary['password']}")
    elif args.cmd == "run":
        run_server(app, host=args.host, port=args.port, debug=args.debug)
    elif args.cmd == "worker":
        processed = 0
        with app.app_context():
            if args.loop:
                print(f"Worker loop started (worker_id={args.worker_id})")
                try:
                    while True:
                        cycle_processed = 0
                        for _ in range(max(1, args.max_jobs)):
                            if not run_worker_once(worker_id=args.worker_id):
                                break
                            processed += 1
                            cycle_processed += 1
                        if cycle_processed == 0:
                            time.sleep(max(1, args.sleep_seconds))
                except KeyboardInterrupt:
                    print("Worker loop stopped by signal.")
            else:
                for _ in range(max(1, args.max_jobs)):
                    if not run_worker_once(worker_id=args.worker_id):
                        break
                    processed += 1
        print(f"Worker processed jobs={processed}")
    elif args.cmd == "scheduler":
        with app.app_context():
            if args.seed_defaults:
                for job_type, interval in DEFAULT_PERIODIC_JOBS:
                    exists = ScheduledJob.query.filter_by(job_type=job_type).first()
                    if not exists:
                        db.session.add(ScheduledJob(job_type=job_type, interval_minutes=interval))
                db.session.commit()
            if args.loop:
                print("Scheduler loop started.")
                queued = 0
                try:
                    while True:
                        queued += schedule_due_jobs()
                        time.sleep(max(1, args.sleep_seconds))
                except KeyboardInterrupt:
                    print("Scheduler loop stopped by signal.")
            else:
                queued = schedule_due_jobs()
        print(f"Scheduler queued jobs={queued}")


if __name__ == "__main__":
    main()
