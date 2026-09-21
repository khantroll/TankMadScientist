"""Small CLI for Tank admin tasks (cancel runs, delete workspaces, resume missions).

Usage:
    python tank_cli.py cancel-run 19
    python tank_cli.py delete-workspace limitednetworkadmin
    python tank_cli.py resume-mission 4 --provider openrouter_agent
    python tank_cli.py resume-mission 4 --from-run 21 --provider openrouter_agent
"""
import sys

import mission
import models
import session_manager


def _usage():
    print(
        "Usage:\n"
        "  python tank_cli.py cancel-run <run_id>\n"
        "  python tank_cli.py delete-workspace <slug>\n"
        "  python tank_cli.py resume-mission <mission_id> [--from-run ID] [--provider ID]\n"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        _usage()
        return 1

    command = argv.pop(0)
    models.init_db()

    if command == "cancel-run":
        if len(argv) != 1:
            _usage()
            return 1
        run_id = int(argv[0])
        if session_manager.cancel_run(run_id):
            print(f"Cancelled run #{run_id}")
            return 0
        print(f"Run #{run_id} could not be cancelled (not found or not active)")
        return 1

    if command == "delete-workspace":
        if len(argv) != 1:
            _usage()
            return 1
        try:
            models.delete_workspace(argv[0])
        except ValueError as exc:
            print(exc)
            return 1
        print(f"Deleted workspace '{argv[0]}'")
        return 0

    if command == "resume-mission":
        if not argv:
            _usage()
            return 1
        mission_id = int(argv.pop(0))
        from_run_id = None
        provider = None
        while argv:
            if argv[0] == "--from-run" and len(argv) >= 2:
                from_run_id = int(argv[1])
                argv = argv[2:]
            elif argv[0] == "--provider" and len(argv) >= 2:
                provider = argv[1]
                argv = argv[2:]
            else:
                _usage()
                return 1
        try:
            run_id = mission.resume_mission(mission_id, from_run_id=from_run_id, provider=provider)
        except ValueError as exc:
            print(exc)
            return 1
        print(f"Resumed mission #{mission_id} — queued run #{run_id}")
        return 0

    _usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
