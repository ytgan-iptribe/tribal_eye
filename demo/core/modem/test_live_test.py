import sys
from schemas import TestStatus
from workflow_modem import modem_execution_worker


def run_live_modem_suite():
    print("==================================================")
    print("[*] STARTING LIVE MODEM EXECUTION WORKER SUITE")
    print("==================================================\n")

    results = modem_execution()
    
    print("\n==================================================")
    print("      SUMMARY OF EXECUTION RESULTS               ")
    print("==================================================")

    all_passed = True

    for idx, result in enumerate(results, start=1):
        decision = result.get("decision")

        if decision == TestStatus.PASS.value or decision == TestStatus.PASS:
            status_symbol = "[✓] PASS"
        else:
            status_symbol = "[✗] FAIL"
            all_passed = False

        print(f"Step {idx}: -> {status_symbol}")

        if result.get("error_message"):
            print(f"        Error Detail: {result.get('error_message')}")

    print("--------------------------------------------------")
    if all_passed and len(results) == 5:
        print("[RESULT] Overall Suite Execution: SUCCESS (All 5 steps passed)")
        sys.exit(0)
    else:
        print(f"[RESULT] Overall Suite Execution: FAILED (Executed {len(results)}/5 steps)")
        sys.exit(1)


if __name__ == "__main__":
    run_live_modem_suite()