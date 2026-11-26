"""Initialize the portable manager on a Windows user profile."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from management_inventory import entry_type, manifest
from skill_manager import Manager, ROOT, write_json


ENTRY = 'local-skill-management'
STARTERS = ('aihot', 'blindspot-breaker', 'generate-home-design-voice-scripts')
OFFLINE_EXAMPLE = 'skill-manager-example'


def detect(profile, overrides=None):
    candidates = {
        'codex': (profile / '.codex/skills', 'merge'),
        'agents': (profile / '.agents/skills', 'merge'),
        'workbuddy-old': (profile / '.workbuddy/skills', 'merge'),
        'workbuddy': (profile / '.workbuddy-ai/skills', 'merge'),
        'antigravity': (profile / '.gemini/config/skills', 'copy'),
    }
    overrides = overrides or {}
    if set(overrides)-candidates.keys():
        raise ValueError('Unknown target; choices: '+', '.join(candidates))
    targets = {name: (overrides.get(name,path), mode) for name,(path,mode) in candidates.items()
               if name in overrides or path.parent.is_dir()}
    applications = {
        'Codex': ['codex', 'agents'] if 'codex' in targets else [],
        'ZCode': ['agents'] if 'agents' in targets and (profile / '.zcode').is_dir() else [],
        'WorkBuddy': ['workbuddy-old'],
        'WorkBuddy AI': ['workbuddy'],
        'Antigravity': ['antigravity'],
    }
    applications = {app: [key for key in keys if key in targets] for app, keys in applications.items()}
    return targets, {app: keys for app, keys in applications.items() if keys}


def write_entry(destination):
    destination.mkdir()
    script = ROOT / 'Manage-Skills.cmd'
    (destination/'SKILL.md').write_text(f"""---
name: {ENTRY}
description: 查看这台 Windows 电脑统一管理的 AI 技能、各应用分发范围和来源；用户提出安装、更新、停用、启用、同步或恢复个人技能时使用。
---
# 管理这台电脑上的个人技能

唯一维护入口是 `{script}`。使用 PowerShell 非交互命令，如 `& '{str(script).replace("'", "''")}' status`。
用 `sources` 查来源，`verify` 查文件、链接及已有的应用实测证据。这些记录不能代替各应用的新会话实际验证。
回答已安装哪些技能时，从 `{ROOT / 'skillshare.yaml'}` 读取纳管名称、范围和启停，不要求用户记住名称。使用应用的原生技能清单确认实际发现，并区分应用或项目自己维护的技能。
只有用户明确要求变更才执行。编辑现有技能时用 `draft 名称`，修改返回的候选目录，再用 `review 草稿ID` 查看差异，经审阅后 `apply 更新ID`。单项分发用 `plan -Skill 名称` 预览，再用 `sync -Skill 名称`。导入用 `import 名称 候选目录 -Applications 应用名`。外部更新使用 `prepare 名称 候选目录`。停用与启用使用 `disable`、`enable`，恢复先用 `history` 和 `restore-plan 完整ID`，再用 `restore 完整ID`。
保留冲突副本及所有权不明的技能。只维护已确认的个人技能，不修改插件、账号、权限或业务数据。
检查同步时用 `reconcile-plan` 查看哪些落后副本仍等于上次分发，再用 `reconcile` 安全补齐。汇报补齐项和仍需审查的差异。中断后用 `recover-plan` 查看待回退事务，用 `recover` 回到操作前。
""", encoding='utf-8')


def finish_install(result):
    """Run the same independent file verification as the public management entry."""
    if not result['exit']:
        checker = ROOT/'_tools/verify_management_acceptance.py'
        checked = subprocess.run([sys.executable,'-B','-X','utf8',str(checker)],cwd=ROOT,
                                 capture_output=True,text=True,encoding='utf-8',timeout=360)
        if checked.stdout.strip():
            result['verification'] = json.loads(checked.stdout)
        if checked.stderr.strip():
            result['verification_stderr'] = checked.stderr.strip()
        result['exit'] = checked.returncode
    write_json(ROOT/'install-result.json',result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['exit']


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--install', action='store_true', help='Preflight, install when safe, and verify in one command')
    parser.add_argument('--with-starters', action='store_true')
    parser.add_argument('--profile', type=Path, default=Path.home(), help='Use a separate test profile for acceptance')
    parser.add_argument('--target', action='append', default=[], metavar='KEY=ABSOLUTE_PATH',
                        help='Use a confirmed non-default scan root for a supported target')
    args = parser.parse_args()
    config = ROOT/'skillshare.yaml'
    if config.exists():
        if args.install:
            return finish_install({'exit':0, 'applied':False, 'already_initialized':True})
        print('Manager already initialized. Use Manage-Skills.cmd status, plan, sync, or restore.')
        return 2
    profile = args.profile.resolve(strict=True)
    overrides = {}
    for value in args.target:
        name, separator, path = value.partition('=')
        if not separator or name in overrides or not Path(path).is_absolute():
            raise ValueError('Expected a unique --target KEY=ABSOLUTE_PATH')
        overrides[name] = Path(path)
    targets, applications = detect(profile, overrides)
    if not targets:
        print('No supported profile folders found. Start the installed AI apps once, then run setup again.')
        return 2
    starter_root = ROOT/'_starter'
    bundled = (OFFLINE_EXAMPLE,) if (starter_root/OFFLINE_EXAMPLE/'SKILL.md').is_file() else STARTERS
    names = [ENTRY, *bundled] if args.with_starters else [ENTRY]
    source = ROOT/'skills'
    data = {
        'source': str(source), 'mode': 'merge', 'target_naming': 'flat',
        'ignore': ['!.DS_Store', '!.git/', '!__pycache__/'],
        'targets': {name: {'skills': {'path': str(path), 'mode': mode, 'include': names, 'exclude': []}}
                    for name, (path, mode) in targets.items()},
        'x-application-readers': applications,
    }
    # Keep setup candidates outside the application scan trees.
    with tempfile.TemporaryDirectory(prefix='setup-', dir=ROOT) as temporary:
        staged = Path(temporary)
        write_entry(staged/ENTRY)
        for name in names[1:]:
            shutil.copytree(starter_root/name, staged/name)
        trial = Manager(config, {**data, 'source': str(staged)})
        with trial.lock():
            if config.exists():
                raise RuntimeError('Another setup already created the configuration')
            collisions = [str(t['path']/name) for t in trial.targets.values() for name in names
                          if entry_type(t['path']/name)['type']!='missing']
            state = {'errors':['Existing skill preserved: '+path for path in collisions], 'actions':[]}
            if not collisions:
                state = trial.inspect(strict=False, check_runtimes=False)
            if entry_type(source)['type'] != 'missing':
                resumed = (entry_type(source)['type']=='directory' and {p.name for p in source.iterdir()}==set(names)
                           and all(entry_type(source/name)['type']=='directory'
                                   and manifest(source/name)==manifest(staged/name) for name in names))
                if not resumed:
                    state['errors'].append('Existing library found. Its files were preserved: '+str(source))
            result = {'profile': str(profile), 'applications': applications, 'targets': data['targets'],
                      'errors': state['errors'], 'actions': state['actions'], 'applied': False}
            if (args.apply or args.install) and not state['errors']:
                Manager(config, data)  # Check the final root boundaries before moving the verified source.
                if not source.exists():
                    os.replace(staged, source)
                write_json(config, data)
                result.update(Manager(config).sync(), applied=True)
            write_json(ROOT/'setup-result.json', result)
    result.setdefault('exit',2 if result['errors'] else 0)
    if args.install:
        return finish_install(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['exit']


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(4)
