"""One-file Windows updater entry point, launched outside the installation folder."""
import argparse
import sys
from pathlib import Path

from interview_agent.update_install import install, recover, validate_plan, write_status, wait_parent, launch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--recover-parent", type=int, default=0)
    args = parser.parse_args()
    plan = validate_plan(args.plan)
    data = Path(plan["data_dir"])
    # The updater must never run from inside the folder it will rename.
    if getattr(sys, "frozen", False) and Path(sys.executable).resolve().is_relative_to(Path(plan["install_dir"])):
        raise RuntimeError("Updater must be copied outside the installation folder.")
    with (data / "logs/update-install.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            if args.recover:
                wait_parent(args.recover_parent)
                recover(args.plan)
                launch(plan, updated=False)
            else:
                return 0 if install(args.plan) else 1
        except Exception:
            import traceback
            traceback.print_exc()
            write_status(data, "repair_required", "更新或恢复尚未完成，请查看 logs/update-install.log；备份已保留。")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
