"""Gate ECR BASIC scan findings for the exact pushed digest; never assume API errors mean safe."""
import json
import os
from pathlib import Path
import subprocess
import time


def ecr(*args):
    return subprocess.run(
        ["aws", "ecr", *args, "--output", "json", "--no-cli-pager"],
        text=True, capture_output=True, check=False,
    )


def check_findings(data):
    if data.get("imageScanStatus", {}).get("status") != "COMPLETE":
        raise ValueError("ECR scan has not completed successfully")
    findings = data.get("imageScanFindings")
    if not isinstance(findings, dict):
        raise ValueError("ECR scan response is missing findings")
    counts = findings.get("findingSeverityCounts")
    if not isinstance(counts, dict):
        raise ValueError("ECR scan response is missing severity counts")
    for severity in ("HIGH", "CRITICAL"):
        count = counts.get(severity, 0)
        if type(count) is not int or count < 0:
            raise ValueError(f"Invalid {severity} count: {count!r}")
        print(f"{severity}: {count}", flush=True)
        if count:
            raise ValueError(f"Security gate failed: {count} {severity} vulnerabilities")


def main():
    image_args = ("--repository-name", os.environ["ECR_REPOSITORY"],
                  "--image-id", "imageDigest=" + os.environ["IMAGE_DIGEST"])
    started = ecr("start-image-scan", *image_args)
    # A scan-on-push race or an already scanned digest can hit the daily scan limit.
    # Only this documented error is tolerated; a COMPLETE result is still mandatory.
    if started.returncode and "LimitExceededException" not in started.stderr:
        raise RuntimeError(started.stderr)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = ecr("describe-image-scan-findings", *image_args)
        if result.returncode:
            if "ScanNotFoundException" not in result.stderr:
                raise RuntimeError(result.stderr)
        else:
            data = json.loads(result.stdout)
            Path("reports").mkdir(exist_ok=True)
            Path("reports/ecr-scan.json").write_text(json.dumps(data, indent=2))
            status = data.get("imageScanStatus", {}).get("status")
            print(f"ECR scan status: {status}", flush=True)
            if status == "COMPLETE":
                check_findings(data)
                summary = os.environ.get("GITHUB_STEP_SUMMARY")
                if summary:
                    with open(summary, "a") as report:
                        report.write(f"ECR gate passed: `{os.environ['IMAGE_DIGEST']}`; HIGH=0, CRITICAL=0.\n")
                return
            if status not in ("PENDING", "IN_PROGRESS"):
                raise RuntimeError(f"ECR scan cannot pass: {data.get('imageScanStatus')}")
        time.sleep(10)
    raise TimeoutError("ECR scan did not complete within 600 seconds")


if __name__ == "__main__":
    main()
