"""
Law Firm Intranet (ELF demo)

Entrypoint module. App code is organized under the `intranet/` package.
"""

from __future__ import annotations

import argparse
import os

from intranet import create_app
from intranet.cli import create_user, init_db, run_server
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

    args = parser.parse_args()

    if args.cmd == "init-db":
        init_db(app)
        print("DB initialized.")
    elif args.cmd == "create-user":
        uid = create_user(app, args.email, args.password, args.role, args.name)
        print(f"Created user id={uid}")
    elif args.cmd == "run":
        run_server(app, host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
