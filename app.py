"""
Law Firm Intranet (ELF demo)

Entrypoint module. App code is organized under the `intranet/` package.
"""

from __future__ import annotations

import argparse
import os

from intranet import create_app
from intranet.cli import create_user, init_db, run_server, seed_demo_data
from intranet.config import env_int

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
        print(f"  users={summary['users']}")
        print(f"  matters={summary['matters']}")
        print(f"  tasks={summary['tasks']}")
        print(f"  documents={summary['documents']}")
        print(f"  contacts={summary['contacts']}")
        print(f"  knowledge_articles={summary['knowledge_articles']}")
        print("Login credentials:")
        print("  admin@elf-ai-demo.co.za")
        print("  partner@elf-ai-demo.co.za")
        print("  associate@elf-ai-demo.co.za")
        print("  paralegal@elf-ai-demo.co.za")
        print("  staff@elf-ai-demo.co.za")
        print(f"  password={summary['password']}")
    elif args.cmd == "run":
        run_server(app, host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
