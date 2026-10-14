"""Unattended sequence: Steps 1 and 2, with optional Steps 3 and 4."""
import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys
from RoadUsage_Workflow import DEFAULT_CONFIG, load_config, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir",type=Path,help="New output directory; defaults to a timestamp under output_root")
    parser.add_argument("--with-report",action="store_true",help="Run disposition report and CompleteRoute after routes")
    args = parser.parse_args()
    config = load_config(args.config)
    folder = (args.run_dir or Path(config["output_root"]) / datetime.now().strftime("%Y%m%d_%H%M%S")).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    scripts = ["RoadUsage_Step1.py","RoadUsage_Step2_Routing.py"]
    if args.with_report:
        scripts.extend(["RoadUsage_Step3_Reporting.py", "RoadUsage_Step4_CompleteRoute.py"])
    state = {"Status":"RUNNING","RunFolder":str(folder),"CompletedSteps":[]}
    write_json(folder / "Run_Status.json",state)
    process = None
    try:
        for script in scripts:
            print(f"Running {script}; outputs: {folder}",flush=True)
            log_path = folder / (Path(script).stem + ".log")
            with log_path.open("w",encoding="utf-8",buffering=1) as log:
                process = subprocess.Popen(
                    [sys.executable,"-B","-u",str(Path(__file__).with_name(script)),
                     "--config",str(args.config.resolve()),"--run-dir",str(folder)],
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,errors="replace")
                for line in process.stdout:
                    print(line,end="",flush=True)
                    log.write(line)
                return_code = process.wait()
            process = None
            if return_code:
                raise subprocess.CalledProcessError(return_code,script)
            state["CompletedSteps"].append(script)
            write_json(folder / "Run_Status.json",state)
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        state.update(Status="INTERRUPTED",InterruptedAt=datetime.now().isoformat(timespec="seconds"))
        write_json(folder / "Run_Status.json",state)
        raise
    except Exception as exc:
        state.update(Status="FAILED",Error=str(exc))
        write_json(folder / "Run_Status.json",state)
        raise
    state["Status"]="COMPLETE"
    write_json(folder / "Run_Status.json",state)
    print(f"COMPLETE: {folder}",flush=True)


if __name__ == "__main__":
    main()
