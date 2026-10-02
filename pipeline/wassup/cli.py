"""wassup command line.

  wassup init-db              create or upgrade the database schema
  wassup run                  run the pipeline forever (collect, cluster, triage, link)
  wassup once                 one full pass, then exit
  wassup collect NAME         run one collector once (gdelt, rss, congress, federal_register)
  wassup process|triage|links|breaking
  wassup retriage             mark every story for triage again (after editing desks or interests)
  wassup rebuild-stories      re-cluster every item (after changing EMBED_BACKEND or EMBED_MODEL)
  wassup api                  serve the API and the built UI on http://localhost:8000
  wassup dev                  pipeline and API together in one process
"""
from __future__ import annotations

import argparse
import logging
import threading

from . import db


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _api(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run("wassup.api:app", host=host, port=port, log_level="info")


def main() -> None:
    p = argparse.ArgumentParser(prog="wassup", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command")
    p.add_argument("name", nargs="?")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    _setup_logging(a.verbose)

    from . import scheduler
    from .cluster import process_new
    from .signals import update_breaking, update_links
    from .triage import run_triage

    if a.command == "init-db":
        db.init_schema()
        print("schema ready")
    elif a.command == "run":
        db.init_schema()
        scheduler.run_forever()
    elif a.command == "dev":
        db.init_schema()
        threading.Thread(target=scheduler.run_forever, daemon=True).start()
        _api(a.host, a.port)
    elif a.command == "once":
        db.init_schema()
        scheduler.run_once()
    elif a.command == "collect":
        cs = {c.key: c for c in scheduler.all_collectors()}
        if a.name not in cs:
            p.error(f"collector must be one of: {', '.join(cs)}")
        with db.connect() as conn:
            print(f"{cs[a.name].run(conn)} new items")
    elif a.command in ("process", "triage", "links", "breaking"):
        fn = {"process": process_new, "triage": run_triage, "links": update_links, "breaking": update_breaking}[a.command]
        with db.connect() as conn:
            print(fn(conn))
    elif a.command == "retriage":
        with db.connect() as conn:
            conn.execute("UPDATE stories SET triaged_item_count = 0")
            conn.commit()
        print("all stories queued for triage")
    elif a.command == "rebuild-stories":
        from .cluster import rebuild_stories

        with db.connect() as conn:
            print(f"{rebuild_stories(conn)} items queued; `wassup run` will re-cluster them")
    elif a.command == "api":
        _api(a.host, a.port)
    else:
        p.error(f"unknown command {a.command}")


if __name__ == "__main__":
    main()
