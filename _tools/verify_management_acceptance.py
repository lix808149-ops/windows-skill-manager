"""Fresh distribution verification with separate application-evidence status."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from skill_manager import Manager, ROOT, read_json, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=ROOT / 'skillshare.yaml', type=Path)
    parser.add_argument('--out', default=ROOT / '_reports/skill-management/current-acceptance.json', type=Path)
    args = parser.parse_args()
    manager = Manager(args.config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw = args.out.with_name(f'{args.out.stem}-independent-{uuid.uuid4().hex[:8]}.json')
    powershell = shutil.which('powershell.exe') or shutil.which('pwsh')
    command = [powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               str(ROOT / '_tools/Verify-Skills.ps1'), '-Config', str(manager.config), '-Out', str(raw.absolute())]
    with manager.lock():
        manager = Manager(args.config)
        state = manager.inspect()
        pending = read_json(manager.base / 'active.json')
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        independent = read_json(raw, {})
    expected = {(key, name) for key, target in manager.targets.items() for name in target['names']}
    seen = [(row['target'], row['skill']) for row in independent.get('rows', [])]
    complete = len(seen) == len(expected) and set(seen) == expected
    file_pass = not state['errors'] and not pending and process.returncode == 0 and independent.get('exit') == 0 and complete
    runtimes = manager.cfg.get('x-tested-application-runtimes', {})
    apps = []
    for app, targets in manager.application_groups().items():
        baseline = runtimes.get(app, {})
        rows = [row for row in state['application_runtime_fingerprints'] if row['application'] == app]
        if not baseline:
            fingerprint = 'no baseline configured'
        elif len(rows) != len(baseline['files']) or any(not row['matches_tested_runtime'] for row in rows):
            fingerprint = 'refresh required'
        else:
            fingerprint = 'matches stored baseline'
        apps.append({'application':app,'targets':targets,'tested_baseline_version':baseline.get('version'),
                     'stored_usage_evidence':baseline.get('evidence'),'stored_test_coverage':baseline.get('coverage'),
                     'program_fingerprint':fingerprint,'current_discovery_and_use':'not tested in this run'})
    result = {'utc':datetime.now(timezone.utc).isoformat(),'exit':0 if file_pass else 2,
              'distribution':{'passed':file_pass,'configured_entries':len(expected),'independently_checked':len(seen),
                              'independently_passed':sum(bool(row.get('passed')) for row in independent.get('rows', [])),
                              'errors':state['errors']+independent.get('errors',[]),'pending_recovery':pending},
              'application_evidence_warnings':state['application_evidence_warnings'],
              'applications':apps,'independent_command':command,'independent_exit':process.returncode,
              'independent_receipt':str(raw),'independent_stderr':process.stderr,
              'historical_results':str(ROOT/'_reports/skill-management/FINAL-REPORT.md')}
    write_json(args.out,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result['exit']


if __name__ == '__main__':
    sys.exit(main())
