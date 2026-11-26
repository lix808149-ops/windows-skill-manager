"""Clone the tool-only Git history and exercise its real Windows entry points."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    repository = Path(sys.argv[1]).resolve(strict=True)
    report = repository.parent/'git-install-test.json'
    runs = []
    checks = []
    def check(name, passed):
        checks.append({'name':name,'passed':bool(passed)})
        if not passed:
            raise AssertionError(name)
    def run(cmd, env=None):
        p = subprocess.run(cmd, env=env, capture_output=True, encoding='utf-8', errors='replace', timeout=300)
        runs.append({'command':cmd,'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        return p
    try:
        with tempfile.TemporaryDirectory(prefix='git-install-', dir=repository.parent) as folder:
            lab = Path(folder)
            checkout = lab/'Clean checkout'
            profile = lab/'Different User'
            for name in ('.codex','.agents','.zcode','.gemini/config','.workbuddy','.workbuddy-ai'):
                (profile/name).mkdir(parents=True)
            custom = profile/'Custom Antigravity skills'
            existing = profile/'.workbuddy/skills/existing'
            existing.mkdir(parents=True)
            marker = existing/'SKILL.md'
            original = '---\nname: existing\ndescription: Existing test skill\n---\n'
            marker.write_text(original,encoding='utf-8')
            git = shutil.which('git')
            p = run([git,'clone','--no-local',str(repository),str(checkout)])
            check('clean clone succeeds without prior installer files',p.returncode==0 and
                  not any((checkout/name).exists() for name in ('_python','_tools/vendor','skillshare.yaml','skills')))
            files = run([git,'-C',str(checkout),'ls-files']).stdout.splitlines()
            check('tracked tree consists of public tool files and synthetic example',
                  not any(name.startswith(('skills/','_reports/','_backup/','_tools/vendor/')) or
                          name.endswith('.exe') for name in files)
                  and any(name.startswith('_starter/skill-manager-example/') for name in files))
            env = {**os.environ,'USERPROFILE':str(profile),'PATH':str(Path(os.environ['SystemRoot'])/'System32')}
            def cmd(entry,*args):
                return run([str(checkout/entry),*map(str,args)],env)
            def manage(*args):
                return cmd('Manage-Skills.cmd',*args)
            def field(result,key):
                return re.search(r'^'+key+r': (.+)$',result.stdout,re.M)[1].strip().strip('"')
            setup = ('--install','--with-starters','--target','antigravity='+str(custom))
            p = cmd('setup.cmd',*setup)
            check('official pinned dependencies acquired with no user Python on PATH',p.returncode==0
                  and (checkout/'_python/python.exe').is_file() and (checkout/'_tools/vendor/skillshare/0.28.4/skillshare.exe').is_file())
            install = json.loads(p.stdout)
            check('all five app candidates and confirmed non-default copy root',
                  len(install['applications'])==5 and install['targets']['antigravity']['skills']['path']==str(custom)
                  and custom.exists() and (checkout/'skillshare.yaml').is_file())
            check('one command preserves outside skill and independently verifies ten entries',
                  install['applied'] and marker.read_text(encoding='utf-8')==original
                  and install['verification']['distribution']['independently_passed']==10)
            configuration = (checkout/'skillshare.yaml').read_bytes()
            original_receipt = (checkout/'setup-result.json').read_bytes()
            p = cmd('setup.cmd','--install')
            repeated = json.loads(p.stdout)
            check('run again verifies without reseeding or replacing original receipt',
                  p.returncode==0 and repeated['already_initialized'] and not repeated['applied']
                  and repeated['verification']['distribution']['independently_passed']==10
                  and (checkout/'skillshare.yaml').read_bytes()==configuration
                  and (checkout/'setup-result.json').read_bytes()==original_receipt)
            source = checkout/'skills/skill-manager-example/version.txt'
            copied = custom/'skill-manager-example/version.txt'
            linked = profile/'.codex/skills/skill-manager-example/version.txt'
            original_version = source.read_text(encoding='utf-8')
            p = manage('draft','skill-manager-example')
            draft = field(p,'draft')
            candidate = Path(field(p,'candidate'))
            (candidate/'version.txt').write_text('fresh-reviewed-version\n',encoding='utf-8')
            p = manage('review',draft)
            check('review through shipped entry leaves both target types on prior version',
                  p.returncode==0 and '+fresh-reviewed-version' in p.stdout
                  and linked.read_text(encoding='utf-8')==copied.read_text(encoding='utf-8')==original_version)
            p = manage('apply',field(p,'update'))
            checkpoint = field(p,'known_good_checkpoint')
            check('reviewed version reaches both targets',
                  p.returncode==0 and linked.read_text(encoding='utf-8')==copied.read_text(encoding='utf-8')=='fresh-reviewed-version\n')
            check('restore via shipped entry returns both to previous version',
                  manage('restore-plan',checkpoint).returncode==0 and manage('restore',checkpoint).returncode==0
                  and linked.read_text(encoding='utf-8')==copied.read_text(encoding='utf-8')==original_version)
            check('verify still passes after rollback',manage('verify').returncode==0)
    finally:
        report.write_text(json.dumps({'checks':checks,'commands':runs},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Passed',len(checks),'checks')


if __name__ == '__main__':
    main()
