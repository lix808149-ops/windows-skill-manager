"""Test reviewed drafts and restoration failures on Windows directory links and copies."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

from skill_manager import Manager, ROOT, read_json, write_json


def main():
    report = ROOT/'_reports/skill-management/handoff-fixes'
    report.mkdir(parents=True, exist_ok=True)
    tests = []
    commands = []

    def check(name, condition):
        tests.append({'name':name, 'passed':bool(condition)})
        if not condition:
            raise AssertionError(name)

    try:
        with tempfile.TemporaryDirectory(dir=report, prefix='regression-') as directory:
            root = Path(directory)
            source = root/'skills/demo'
            source.mkdir(parents=True)
            (source/'SKILL.md').write_text('---\nname: demo\ndescription: Synthetic fixture\n---\n',encoding='utf-8')
            (source/'version.txt').write_text('old',encoding='utf-8')
            targets = {key:{'skills':{'path':str(root/key), 'mode':mode, 'include':['demo']}}
                       for key,mode in [('linked','merge'),('copied','copy')]}
            config = root/'skillshare.yaml'
            write_json(config, {'source':str(source.parent), 'targets':targets})

            def run(script, *args):
                cmd = [sys.executable,'-B','-X','utf8',str(ROOT/'_tools'/script),'--config',str(config),*args]
                result = subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',timeout=90)
                commands.append({'command':cmd,'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
                return json.loads(result.stdout)

            def versions():
                return [(root/key/'demo/version.txt').read_text(encoding='utf-8') for key in ['skills','linked','copied']]

            check('initialize linked and copied targets',run('skill_manager.py','sync')['exit']==0)
            draft = run('skill_updates.py','draft','demo')
            stale = run('skill_updates.py','draft','demo')
            candidate = Path(draft['candidate'])
            (candidate/'version.txt').write_text('new',encoding='utf-8')
            (Path(stale['candidate'])/'version.txt').write_text('later',encoding='utf-8')
            check('all active paths still read old while editing',versions()==['old']*3)
            review = run('skill_updates.py','review',draft['draft'])
            check('review includes changed file and version',review['changed_files']==['version.txt'] and
                  '+new' in Path(review['diff']).read_text(encoding='utf-8') and versions()==['old']*3)
            (candidate/'version.txt').write_text('unreviewed',encoding='utf-8')
            new = run('skill_updates.py','apply',review['update'])
            checkpoint = new.get('known_good_checkpoint')
            check('only frozen reviewed version is applied',new['exit']==0 and versions()==['new']*3)
            check('second draft cannot overwrite a newer published base',
                  run('skill_updates.py','review',stale['draft'])['exit']!=0 and versions()==['new']*3)

            manager = Manager(config)
            original_copy = shutil.copytree
            def fail_stage(before, after, *args, **kwargs):
                if Path(after).name=='new' and Path(before).parent.name=='copied':
                    Path(after).mkdir()
                    (Path(after)/'incomplete.txt').write_text('partial',encoding='utf-8')
                    raise OSError(errno.ENOSPC,'Synthetic destination staging failure')
                return original_copy(before,after,*args,**kwargs)
            with manager.lock(), patch('skill_manager.shutil.copytree',side_effect=fail_stage):
                failure = manager.restore(checkpoint)
            check('partial staging never reaches source, links, or existing copies',
                  failure['exit']==3 and versions()==['new']*3 and
                  read_json(manager.base/'active.json')['id']==failure['recovery_checkpoint'])
            check('recover staged failure successfully',
                  run('skill_manager.py','restore',failure['recovery_checkpoint'])['exit']==0)

            manager = Manager(config)
            replace = os.replace
            def fail_later_switch(before, after):
                if Path(before).name=='new' and Path(after)==root/'copied/demo':
                    raise PermissionError('Synthetic rename failure on the later copy')
                return replace(before,after)
            with manager.lock(), patch('skill_manager.os.replace',side_effect=fail_later_switch):
                failure = manager.restore(checkpoint)
            check('late rename failure puts all previously switched entries back',
                  failure['exit']==3 and versions()==['new']*3)
            check('retry and restore old version',run('skill_manager.py','restore',failure['recovery_checkpoint'])['exit']==0
                  and run('skill_manager.py','restore',checkpoint)['exit']==0 and versions()==['old']*3)
            (root/'linked/demo').rmdir()
    finally:
        write_json(report/'handoff-regression.json',{'checks':tests, 'commands':commands})
    print('Passed',len(tests),'checks')


if __name__=='__main__':
    main()
