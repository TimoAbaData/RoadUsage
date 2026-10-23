"""Unattended numbered workflow with a frozen config and safe completed-step resume."""
import argparse
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import sys
from RoadUsage_Workflow import DEFAULT_CONFIG, config_hash, load_config, write_json
import json

STEP_MARKERS = {
    "RoadUsage_Step1.py": "Step1_Complete.json",
    "RoadUsage_Step2_Routing.py": "Step2_Complete.json",
    "RoadUsage_Step3_Reporting.py": "Step3_Complete.json",
    "RoadUsage_Step4_CompleteRoute.py": "Step4_Complete.json",
    "RoadUsage_Step5_ExportGDB.py": "Step5_Complete.json",
}


def prepare_run(config_path, requested_folder=None, resume=False):
    """Freeze one effective configuration; resume only from verified saved settings."""
    if resume:
        if requested_folder is None:
            raise ValueError("--resume requires --run-dir for the existing run")
        folder = Path(requested_folder).resolve()
        first = json.loads((folder / STEP_MARKERS["RoadUsage_Step1.py"]).read_text(encoding="utf-8-sig"))
        if first.get("Status") != "COMPLETE" or not isinstance(first.get("Config"), dict):
            raise ValueError("Resume requires a completed Step 1 with its saved configuration")
        config = first["Config"]
        if config_hash(config) != first["ConfigSHA256"]:
            raise ValueError("The saved Step 1 configuration does not match its fingerprint")
        if not Path(first["PreparedGDB"]).is_dir():
            raise FileNotFoundError(f"Prepared Step 1 geodatabase is missing: {first['PreparedGDB']}")
    else:
        config = load_config(config_path)
        folder = (Path(requested_folder) if requested_folder is not None else
                  Path(config["output_root"]) / datetime.now().strftime("%Y%m%d_%H%M%S")).resolve()
        folder.mkdir(parents=True, exist_ok=False)
    snapshot = folder / "Run_Config.json"
    if snapshot.exists():
        if config_hash(load_config(snapshot)) != config_hash(config):
            raise ValueError("Run_Config.json differs from the saved run; existing results are preserved")
    else:
        write_json(snapshot, config)
    return folder, config, snapshot


def completed_steps(folder, scripts, config):
    """Require a contiguous completed sequence and reject changed configuration."""
    completed = []
    missing = False
    for script in scripts:
        marker = folder / STEP_MARKERS[script]
        if not marker.exists():
            missing = True
            continue
        if missing:
            raise ValueError(f"Cannot resume: {marker.name} exists after an incomplete earlier step")
        state = json.loads(marker.read_text(encoding="utf-8-sig"))
        if state.get("Status") != "COMPLETE":
            raise ValueError(f"Cannot resume an unverified step: {marker}")
        if "ConfigSHA256" in state and state["ConfigSHA256"] != config_hash(config):
            raise ValueError(f"Saved configuration mismatch in {marker.name}")
        completed.append(script)
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path, help="Output directory; existing completed steps require --resume")
    parser.add_argument("--with-report", action="store_true", help="Run disposition report and CompleteRoute after routes")
    parser.add_argument("--resume", action="store_true", help="Continue an existing run using its saved Step 1 settings")
    args = parser.parse_args()
    folder, config, snapshot = prepare_run(args.config, args.run_dir, args.resume)
    scripts = ["RoadUsage_Step1.py", "RoadUsage_Step2_Routing.py"]
    if args.with_report:
        scripts.extend(["RoadUsage_Step3_Reporting.py", "RoadUsage_Step4_CompleteRoute.py"])
    scripts.append("RoadUsage_Step5_ExportGDB.py")
    completed = completed_steps(folder, scripts, config) if args.resume else []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.resume and (folder / "Run_Status.json").exists():
        shutil.copy2(folder / "Run_Status.json", folder / f"Run_Status_before_resume_{stamp}.json")
    state = {"Status": "RUNNING", "RunFolder": str(folder), "CompletedSteps": completed,
             "FrozenConfig": str(snapshot), "ConfigSHA256": config_hash(config)}
    write_json(folder / "Run_Status.json", state)
    if args.resume:
        print(f"Resuming with saved configuration: {snapshot}; completed steps: {len(completed)}", flush=True)
    process = None
    try:
        for script in scripts:
            if script in completed:
                continue
            print(f"Running {script}; outputs: {folder}", flush=True)
            suffix = f".resume_{stamp}" if args.resume else ""
            log_path = folder / (Path(script).stem + suffix + ".log")
            with log_path.open("x", encoding="utf-8", buffering=1) as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-u", str(Path(__file__).with_name(script)),
                     "--config", str(snapshot), "--run-dir", str(folder)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                return_code = process.wait()
            process = None
            if return_code:
                raise subprocess.CalledProcessError(return_code, script)
            state["CompletedSteps"].append(script)
            write_json(folder / "Run_Status.json", state)
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        state.update(Status="INTERRUPTED", InterruptedAt=datetime.now().isoformat(timespec="seconds"))
        write_json(folder / "Run_Status.json", state)
        raise
    except Exception as exc:
        state.update(Status="FAILED", Error=str(exc))
        write_json(folder / "Run_Status.json", state)
        raise
    state["Status"] = "COMPLETE"
    write_json(folder / "Run_Status.json", state)
    print(f"COMPLETE: {folder}", flush=True)


if __name__ == "__main__":
    main()
