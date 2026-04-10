from __future__ import annotations

import argparse

from wa_scheduler.db import SessionLocal, init_db
from wa_scheduler.services.scheduler import enqueue_unique_job, materialize_runs
from wa_scheduler.services.sync import sync_chats, sync_contacts
from wa_scheduler.services.wacli import WacliClient
from wa_scheduler.services.worker import enqueue_default_sync_jobs, run_worker_loop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wa-scheduler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create database tables")
    subparsers.add_parser("sync-now", help="Refresh contacts and chats from wacli")
    subparsers.add_parser("materialize", help="Generate pending runs from schedules")
    subparsers.add_parser("enqueue-sync", help="Enqueue maintenance jobs")

    worker = subparsers.add_parser("worker", help="Run the single-threaded runner")
    worker.add_argument(
        "--once", action="store_true", help="Process a single tick and exit"
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-db":
        init_db()
        print("Database initialized.")
        return

    init_db()

    if args.command == "sync-now":
        client = WacliClient()
        with SessionLocal() as session:
            contacts = sync_contacts(session, client)
            chats = sync_chats(session, client)
        print(f"Contacts synced: {contacts}")
        print(f"Chats synced: {chats}")
        return

    if args.command == "materialize":
        with SessionLocal() as session:
            created = materialize_runs(session)
        print(f"Runs created: {created}")
        return

    if args.command == "enqueue-sync":
        with SessionLocal() as session:
            created = enqueue_default_sync_jobs(session)
            if enqueue_unique_job(session, "sync_once", priority=60):
                created += 1
        print(f"Jobs enqueued: {created}")
        return

    if args.command == "worker":
        processed = run_worker_loop(SessionLocal, once=args.once)
        print(f"Worker finished. Jobs processed: {processed}")
        return
