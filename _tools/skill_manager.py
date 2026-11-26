"""Scoped safety adapter for pinned skillshare. Paths/modes/selection come only from its config.

The only native setting exception is the explicitly approved skill invocation keys.
Plugin, account, MCP and business data remain outside management ownership.
"""
import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import yaml
import native_invocation
import runtime_fingerprints
from management_inventory import entry_type, manifest, digest

ROOT=pathlib.Path(__file__).resolve().parents[1]
EXE=ROOT/"_tools/vendor/skillshare/0.28.4/skillshare.exe"
EXE_SHA="acbcf98fce0d9949fff3944a8ca34feac93da682d8c44532bfeb2a337fdb68f7"
VERSION='1.3.0'
MARKER=".skillshare-manifest.json"

class ConcurrentTargetChange(RuntimeError):
    pass

def write_json(path,data):
    path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+".tmp-"+uuid.uuid4().hex)
    with temp.open("w",encoding="utf-8",newline="\n") as f:
        json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)

def read_json(path,default=None):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig")) if pathlib.Path(path).is_file() else default

def clean_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*",name):raise ValueError("Unsafe or non-flat skill name: "+name)
    return name

def skill_identity(path):
    with (path / 'SKILL.md').open(encoding='utf-8-sig') as stream:
        if stream.readline().strip() != '---':
            raise ValueError('Missing skill frontmatter')
        lines = []
        for line in stream:
            if line.strip() == '---':
                break
            lines.append(line)
        else:
            raise ValueError('Unclosed skill frontmatter')
    metadata = yaml.safe_load(''.join(lines))
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in ('name', 'description')):
        raise ValueError('Name and description must be nonempty strings')
    return metadata['name'].strip()

def full_state(path):
    kind=entry_type(path)
    if kind["type"]=="directory":kind["manifest"]=manifest(path)
    elif kind["type"]=="file":kind["sha256"]=hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    return kind

def equivalent(a,b):
    # resolved is lexical bookkeeping for regular files/directories, not content.
    return {k:v for k,v in a.items() if k!="resolved"}=={k:v for k,v in b.items() if k!="resolved"}

def protected_state(path):
    """Unowned files: metadata only. Never read .env, keys or installer contents."""
    path=pathlib.Path(path);kind=entry_type(path)
    def info(p):
        s=p.lstat()
        return {"size":s.st_size,"mtime_ns":s.st_mtime_ns,"file_id":s.st_ino,"device":s.st_dev}
    if kind["type"]=="directory":
        entries=[]
        def walk(base):
            for p in sorted(base.iterdir(),key=lambda p:p.name):
                k=entry_type(p);row={"path":p.relative_to(path).as_posix(),**k}
                if k["type"]=="directory":
                    # Directory mtime changes when entries are renamed; child file identities matter.
                    walk(p)
                else:row.update(info(p))
                entries.append(row)
        walk(path);kind["metadata_manifest"]=sorted(entries,key=lambda r:r["path"])
    elif kind["type"]=="file":kind.update(info(path))
    return kind

def pid_alive(pid):
    if os.name!="nt":
        try:os.kill(pid,0);return True
        except ProcessLookupError:return False
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle:return ctypes.get_last_error()!=87
    try:return kernel.WaitForSingleObject(ctypes.c_void_p(handle),0)==258
    finally:kernel.CloseHandle(ctypes.c_void_p(handle))

class Manager:
    def __init__(self,config,config_data=None):
        self.config=pathlib.Path(config).absolute()
        self.base=self.config.parent/".skill-management"
        self.cfg=config_data if config_data is not None else yaml.safe_load(self.config.read_text(encoding="utf-8-sig"))
        self.source=pathlib.Path(self.cfg["source"])
        if not self.source.is_absolute():raise ValueError("Source must be absolute")
        self.targets={}
        for key,item in self.cfg["targets"].items():
            clean_name(key)
            if key.casefold() in ('source','management'):raise ValueError('Reserved target name: '+key)
            sc=item.get("skills",item)
            path=pathlib.Path(sc["path"]).absolute()
            if not pathlib.Path(sc["path"]).is_absolute():raise ValueError("Target must be absolute")
            if sc.get("mode",self.cfg.get("mode")) not in ("merge","copy"):raise ValueError("Only per-skill merge/copy is permitted")
            for part in path.parts:
                if part.lower() in ("plugins","cache",".system"):raise ValueError("Protected target: "+str(path))
            names=[clean_name(n) for n in sc.get("include",[])]
            if not names:raise ValueError("Explicit include ownership list required")
            if len({n.casefold() for n in names}) != len(names):raise ValueError('Duplicate skill directories in '+key)
            if set(sc.get("exclude",[]))-set(names):raise ValueError("exclude must select registered skills")
            self.targets[key]={"path":path,"mode":sc.get("mode",self.cfg.get("mode","merge")),"names":names,"exclude":sc.get("exclude",[])}
        roots=[self.source,self.base,*[t["path"] for t in self.targets.values()]]
        for index,root in enumerate(roots):
            if root.resolve() != root or '..' in root.parts:
                raise ValueError('Redirected management or scan root: '+str(root))
            if any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots[:index]):
                raise ValueError("Overlapping managed/scanning/backup roots")
        self.base.mkdir(parents=True,exist_ok=True)
        self.names=sorted({n for t in self.targets.values() for n in t["names"]})
        self.state=read_json(self.base/"ownership.json",{"entries":{},"config_targets":{}})
        self.env={**os.environ,"SKILLSHARE_CONFIG":str(self.config),"XDG_DATA_HOME":str(self.base/"native-data"),"XDG_STATE_HOME":str(self.base/"native-state"),"XDG_CACHE_HOME":str(self.base/"native-cache"),"NO_COLOR":"1"}
        if hashlib.sha256(EXE.read_bytes()).hexdigest()!=EXE_SHA:raise ValueError("Pinned upstream binary hash mismatch")
    @contextlib.contextmanager
    def lock(self):
        # Keep this inode in place. The OS releases its lock even on a hard process exit.
        with (self.base/'operation.guard').open('a+b') as guard:
            guard.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(guard.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another management operation is active") from exc
            with self.legacy_lock():
                yield

    @contextlib.contextmanager
    def legacy_lock(self):
        path=self.base/"operation.lock"
        if path.exists():
            old=read_json(path)
            if pid_alive(old["pid"]):raise RuntimeError("Another management operation is active")
            path.unlink()
        # Windows rename publishes a complete marker and refuses an existing destination.
        if os.name=='nt':
            marker=self.base/('pid-'+uuid.uuid4().hex+'.tmp')
            try:
                marker.write_text(json.dumps({'pid':os.getpid()}),encoding='utf-8')
                marker.rename(path)
            finally:marker.unlink(missing_ok=True)
        else:
            with path.open('x') as stream:json.dump({'pid':os.getpid()},stream)
        try:yield
        finally:path.unlink(missing_ok=True)
    def save_state(self):write_json(self.base/"ownership.json",self.state)
    def owned_paths(self):
        yield 'management/provenance',self.config.parent/'skill-sources.json'
        for n in self.names:yield "source/"+n,self.source/n
        for key,t in self.targets.items():
            for n in t["names"]:yield key+"/"+n,t["path"]/n
    def native(self,*args,config_override=None):
        start=time.perf_counter()
        env={**self.env,"SKILLSHARE_CONFIG":str(config_override or self.config)}
        p=subprocess.run([str(EXE),*args,"-g"],cwd=self.config.parent,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=120)
        row={"utc":dt.datetime.now(dt.timezone.utc).isoformat(),"config":env["SKILLSHARE_CONFIG"],"command":[str(EXE),*args,"-g"],"exit":p.returncode,"seconds":time.perf_counter()-start,"stdout":p.stdout,"stderr":p.stderr}
        log=self.base/"commands"/(dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")+".json")
        write_json(log,row)
        if p.returncode:raise RuntimeError("skillshare failed; raw evidence: "+str(log))
        try:
            data=json.loads(p.stdout)
            if data.get("failed") or data.get("error"):raise RuntimeError("skillshare reported errors: "+str(log))
        except json.JSONDecodeError:pass
        return row
    def expected(self,key,name):
        t=self.targets[key]
        return name not in t["exclude"]
    def application_groups(self):
        readers = dict(self.cfg.get('x-application-readers', {}))
        if any(key not in self.targets for keys in readers.values() for key in keys):
            raise ValueError('Application readers must use configured targets')
        covered = {key for keys in readers.values() for key in keys}
        readers.update({key: [key] for key in self.targets.keys() - covered})
        return readers

    def catalog_conflicts(self, identities):
        errors = []
        unowned = {}
        for app, keys in self.application_groups().items():
            seen = {}
            for key in keys:
                for name in self.targets[key]['names']:
                    if not self.expected(key, name) or name not in identities:
                        continue
                    identity = identities[name].casefold()
                    previous = seen.setdefault(identity, name)
                    if previous != name:
                        message = f'{app}: duplicate skill name {identities[name]} in {previous} and {name}'
                        if message not in errors:
                            errors.append(message)
            for key in keys:
                if key not in unowned:
                    target=self.targets[key]
                    unowned[key]=[]
                    managed={name.casefold() for name in target['names']}
                    for path in target['path'].iterdir() if entry_type(target['path'])['type']=='directory' else ():
                        if path.name.startswith('.') or path.name.casefold() in managed:continue
                        try:other=skill_identity(path)
                        except (OSError,ValueError,yaml.YAMLError):continue
                        unowned[key].append((other,path.name))
                for other,folder in unowned[key]:
                    if other.casefold() in seen:
                        errors.append(f'{key}/{seen[other.casefold()]}: {app} also sees skill name {other} in unowned {key}/{folder}')
        return errors

    def inspect(self,strict=True,repair=False,allow_new=False,allow_unregistered=(),check_runtimes=True):
        errors=[];actions=[];rows=[]
        preserved=self.cfg.get('x-preserved-sources',{})
        if set(preserved)&set(self.names):errors.append('A preserved source is also configured for ownership')
        unregistered=[p.name for p in self.source.iterdir() if p.is_dir() and (p/'SKILL.md').is_file() and p.name not in self.names and p.name not in preserved and p.name not in allow_unregistered]
        if unregistered:errors.append('Unregistered sources retained; use import to register explicitly: '+', '.join(sorted(unregistered)))
        identities={}
        for name in self.names:
            p=self.source/name
            try:
                if entry_type(p)["type"]!="directory":raise ValueError("source must be a real directory")
                identities[name]=skill_identity(p)
                files=manifest(p)
                if any(f["type"] not in ("file","directory") for f in files):raise ValueError("nested reparse point requires explicit review")
            except Exception as e:errors.append("source/"+name+": "+str(e))
        errors.extend(self.catalog_conflicts(identities))
        for key,t in self.targets.items():
            oldpath=self.state["config_targets"].get(key)
            if oldpath and os.path.normcase(oldpath)!=os.path.normcase(str(t["path"])):errors.append(key+": target path changed; explicit re-adoption required")
            rootkind=entry_type(t["path"])["type"]
            if rootkind not in ("directory","missing"):errors.append(key+": root must be real directory")
            if rootkind=="missing":
                if strict:errors.append(key+": missing target root")
                actions.append({"target":key,"action":"create-root"})
            marker=t["path"]/MARKER
            if marker.exists():errors.append(key+": another manager manifest is present; ownership must be resolved first")
            for name in t["names"]:
                p=t["path"]/name;src=self.source/name
                try:
                    current=full_state(p);exists=current["type"]!="missing"
                    previous=self.state["entries"].get(key+"/"+name)
                    unowned=exists and previous is None and not allow_new
                    if unowned:errors.append(key+"/"+name+": unowned collision; adopt only after comparison")
                    if current["type"] in ("junction","symlink"):
                        good=os.path.normcase(os.path.realpath(p))==os.path.normcase(os.path.realpath(src)) and t["mode"]=="merge"
                    elif current["type"]=="directory":good=t["mode"]=="copy" and current["manifest"]==manifest(src)
                    else:good=False
                    wanted=self.expected(key,name)
                    changed=not good if wanted else exists
                    drifted=changed and exists and previous is not None and not equivalent(current,previous)
                    if unowned or drifted:
                        delivery_state='conflict'
                    elif not changed:
                        delivery_state='current' if wanted else 'disabled'
                    elif not wanted:
                        delivery_state='withdraw_pending'
                    elif not exists:
                        delivery_state='missing'
                    elif t["mode"]=="copy" and current["type"]=="directory" and previous is not None:
                        delivery_state='stale_copy'
                    else:
                        delivery_state='needs_sync'
                    if changed:actions.append({"target":key,"skill":name,"action":"distribute" if wanted else "withdraw","current_type":current["type"],"observed_sha256":digest(current),"delivery_state":delivery_state})
                    if strict and changed:errors.append(key+"/"+name+": "+("missing/mismatched target" if wanted else "disabled entry still distributed"))
                    # Updating a pristine old managed copy is fine. Unknown edits require repair.
                    if not strict and drifted and not repair:
                        errors.append(key+"/"+name+": drift since last accepted version; review then repair")
                    rows.append({"target":key,"skill":name,"path":str(p),"delivery_state":delivery_state,"enabled":wanted,"type":current["type"],"matches":good,"tree_sha256":digest(current.get("manifest",[])) if current["type"]=="directory" else None})
                except Exception as e:errors.append(key+"/"+name+": "+str(e))
        try:
            native_errors,native_actions,native_state=native_invocation.inspect(self,strict,repair)
            errors.extend(native_errors);actions.extend(native_actions)
        except Exception as e:errors.append('native invocation: '+str(e));native_state={}
        runtime_errors,runtime_rows=runtime_fingerprints.inspect(self.cfg) if check_runtimes else ([],[])
        return {"errors":errors,"actions":actions,"rows":rows,"unregistered_sources":unregistered,"preserved_sources":preserved,'native_invocation':native_state,'application_runtime_fingerprints':runtime_rows,'application_evidence_warnings':runtime_errors}
    def unmanaged(self):
        out={}
        for key,t in self.targets.items():
            out[key]={}
            if t["path"].is_dir():
                out[key]={p.name:protected_state(p) for p in t["path"].iterdir() if p.name not in t["names"]}
        return out
    def capture(self,label,scope=None,previous_config=None):
        ident=dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-"+uuid.uuid4().hex[:6]
        dest=self.base/"transactions"/ident;dest.mkdir(parents=True)
        record={"id":ident,"label":label,"status":"preparing","utc":dt.datetime.now(dt.timezone.utc).isoformat(),"entries":[],"unmanaged":self.unmanaged(),"roots":{k:entry_type(t['path']) for k,t in self.targets.items()},"config":json.loads(json.dumps(self.cfg)),"ownership":json.loads(json.dumps(self.state))}
        record['skill_scope']=scope
        if previous_config is not None:
            record['working_config']=record['config']
            record['config']=previous_config
        if scope is None:
            record['native_invocation']=native_invocation.snapshot(self)
        write_json(dest/"journal.json",record)
        for key,p in self.owned_paths():
            if scope and key != 'management/provenance' and key.rsplit('/',1)[-1] != scope:
                continue
            state=full_state(p);row={"key":key,"path":str(p),"state":state}
            backup=dest/"files"/key
            if state["type"]=="directory":
                shutil.copytree(p,backup)
                if manifest(backup)!=state["manifest"]:raise RuntimeError("Backup full manifest mismatch")
            elif state["type"]=="file":
                backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,backup)
                if hashlib.sha256(backup.read_bytes()).hexdigest()!=state["sha256"]:raise RuntimeError("Backup file mismatch")
            elif state["type"] not in ("missing","junction","symlink"):raise ValueError("Unsupported reparse type")
            record["entries"].append(row)
            write_json(dest/"journal.json",record)
        record["status"]="prepared";write_json(dest/"journal.json",record)
        return dest,record
    def make_junction(self,p,target):
        quote=lambda s:"'"+str(s).replace("'","''")+"'"
        r=subprocess.run(["powershell","-NoProfile","-NonInteractive","-Command","New-Item -ItemType Junction -Path "+quote(p)+" -Target "+quote(target)+" | Out-Null"],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if r.returncode:raise RuntimeError("Junction creation failed: "+r.stderr.decode(errors="replace"))
    def stage_and_commit(self,dest,record,actions):
        # Upstream owns only this disposable staging tree. It never receives the live paths.
        selections={key:[n for n in t['names'] if not record.get('skill_scope') or n==record['skill_scope']]
                    for key,t in self.targets.items()}
        cfg={k:json.loads(json.dumps(v)) for k,v in self.cfg.items() if k in ("source","mode","target_naming","ignore")}
        cfg["targets"]={key:{"skills":{"path":str(dest/"native-stage"/key),"mode":t["mode"],"include":selections[key],"exclude":[n for n in t['exclude'] if n in selections[key]]}} for key,t in self.targets.items() if selections[key]}
        staged_config=dest/"staging-config.yaml";write_json(staged_config,cfg)
        native=self.native("sync","--json",config_override=staged_config)
        todo=[a for a in actions if "skill" in a]
        prepared=[]
        for key,t in self.targets.items():
            for name in selections[key]:
                staged=dest/"native-stage"/key/name
                if self.expected(key,name):
                    kind=entry_type(staged)["type"]
                    if t["mode"]=="copy" and (kind!="directory" or manifest(staged)!=manifest(self.source/name)):raise RuntimeError("Staged copy does not match complete source: "+key+"/"+name)
                    if t["mode"]=="merge" and (kind not in ("junction","symlink") or os.path.realpath(staged)!=os.path.realpath(self.source/name)):raise RuntimeError("Staged link wrong: "+key+"/"+name)
                elif entry_type(staged)["type"]!="missing":raise RuntimeError("Disabled skill was staged")
        for action in todo:
            key=action["target"];name=action["skill"];t=self.targets[key]
            local=t["path"].parent/".skill-management-stage"/record["id"]/key
            local.mkdir(parents=True,exist_ok=True)
            new=local/(name+".new");old=local/(name+".old");target=t["path"]/name
            if self.expected(key,name):
                if t["mode"]=="merge":self.make_junction(new,self.source/name)
                else:
                    shutil.copytree(dest/"native-stage"/key/name,new)
                    if manifest(new)!=manifest(self.source/name):raise RuntimeError("Local staged copy mismatch")
            prepared.append((target,new,old,self.expected(key,name)))
        record["local_staging"]=[str(self.targets[a["target"]]["path"].parent/".skill-management-stage"/record["id"]/a["target"]) for a in todo]
        write_json(dest/"journal.json",record)
        before={pathlib.Path(row['path']):row['state'] for row in record['entries']}
        for action,(target,_,_,_) in zip(todo,prepared):
            if target not in before or digest(before[target])!=action["observed_sha256"]:
                raise ConcurrentTargetChange('Target changed between inspection and backup: '+str(target))
        # Recheck each target immediately before its rename, including later targets in a batch.
        for target,new,old,wanted in prepared:
            if not equivalent(full_state(target),before[target]):
                raise ConcurrentTargetChange('Target changed after backup; review before syncing: '+str(target))
            target.parent.mkdir(parents=True,exist_ok=True)
            if entry_type(target)["type"]!="missing":os.replace(target,old)
            if wanted:os.replace(new,target)
        return native
    def clean_staging(self,record):
        for key,t in self.targets.items():
            path=t["path"].parent/".skill-management-stage"/record["id"]
            expected_parent=t["path"].parent/".skill-management-stage"
            if path.absolute().parent!=expected_parent.absolute():raise ValueError("Unsafe staging cleanup target")
            if entry_type(path)["type"]=="directory":shutil.rmtree(path)
    @staticmethod
    def restore_scope(record):
        if 'skill_scope' in record:
            return clean_name(record['skill_scope']) if record['skill_scope'] else None
        # Earlier releases used full snapshots for these single-skill operations.
        match = re.fullmatch(r'(?:known-good-before-update|before-edit)-([A-Za-z0-9][A-Za-z0-9_-]*)', record['label'])
        return match.group(1) if match else None

    def restore_entries(self,dest,entries):
        allowed = {key: path for key, path in self.owned_paths()}
        for row in entries:
            path = allowed.get(row['key'])
            if path is None or path != pathlib.Path(row['path']).absolute():
                raise ValueError('Restore refers to entries outside current ownership')
            if entry_type(path.parent)['type'] not in ('directory', 'missing'):
                raise ValueError('Restore parent is not a real directory: ' + str(path.parent))
        for row in entries:
            backup=dest/'files'/row['key'];state=row['state']
            if state['type']=='directory' and manifest(backup)!=state['manifest']:
                raise RuntimeError('Corrupt backup: '+row['key'])
            if state['type']=='file' and hashlib.sha256(backup.read_bytes()).hexdigest()!=state['sha256']:
                raise RuntimeError('Corrupt backup: '+row['key'])
        # Verify each replacement on the destination volume before changing any live entry.
        with contextlib.ExitStack() as scratch:
            prepared = []
            for row in entries:
                path = allowed[row['key']]
                state = row['state']
                original = full_state(path)
                if equivalent(original, state):
                    continue
                backup = dest/'files'/row['key']
                stage_root = self.base if row['key']=='management/provenance' else path.parent.parent/'.skill-management-stage'
                if stage_root.resolve()!=stage_root:
                    raise ValueError('Redirected restoration staging root: '+str(stage_root))
                stage_root.mkdir(parents=True,exist_ok=True)
                staging = pathlib.Path(scratch.enter_context(tempfile.TemporaryDirectory(prefix='restore-', dir=stage_root)))
                new, old = staging/'new', staging/'old'
                if state['type']=='directory':
                    shutil.copytree(backup,new)
                elif state['type']=='file':
                    shutil.copy2(backup,new)
                elif state['type']=='junction':
                    self.make_junction(new,os.path.normpath(state['target'].removeprefix('\\\\?\\')))
                elif state['type']=='symlink':
                    os.symlink(state['target'],new,target_is_directory=True)
                if not equivalent(full_state(new),state):
                    raise RuntimeError('Staged restore verification failed: '+row['key'])
                prepared.append((path, new, old, original, state))

            switched = []
            try:
                for path, new, old, original, state in prepared:
                    if not equivalent(full_state(path),original):
                        raise ConcurrentTargetChange('Entry changed during restore preparation: '+str(path))
                    path.parent.mkdir(parents=True,exist_ok=True)
                    if original['type']!='missing':
                        os.replace(path,old)
                    try:
                        if state['type']!='missing':
                            os.replace(new,path)
                    except OSError:
                        if original['type']!='missing':
                            os.replace(old,path)
                        raise
                    switched.append((path, new, old, original, state))
                    if not equivalent(full_state(path),state):
                        raise RuntimeError('Restore verification failed: '+str(path))
            except Exception as exc:
                rollback_errors = []
                for path, new, old, original, state in reversed(switched):
                    try:
                        if not equivalent(full_state(path),state):
                            raise ConcurrentTargetChange('Entry changed after switch: '+str(path))
                        if state['type']!='missing':
                            os.replace(path,new)
                        if original['type']!='missing':
                            os.replace(old,path)
                    except (OSError, ConcurrentTargetChange) as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    raise RuntimeError(f'{exc}; restore rollback needs review: ' + '; '.join(rollback_errors)) from exc
                raise

    def scoped_restore(self, dest, record, scope):
        current = yaml.safe_load(self.config.read_text(encoding='utf-8-sig'))
        saved_targets=self.check_scoped_topology(record,scope)
        wanted_keys = {'source/'+scope, *(key+'/'+scope for key in saved_targets)}
        self.restore_scoped_files(dest,record,scope,wanted_keys)
        for key in saved_targets:
            after = current['targets'][key].get('skills',current['targets'][key])
            old = record['config']['targets'][key].get('skills',record['config']['targets'][key])
            if scope not in old['include']:
                after['include'] = [name for name in after['include'] if name != scope]
            excludes = [name for name in after.get('exclude',[]) if name != scope]
            if scope in old.get('exclude',[]):excludes.append(scope)
            if excludes or 'exclude' in old or 'exclude' in after:after['exclude']=sorted(excludes)
            if record['roots'][key]['type'] == 'missing' and entry_type(self.targets[key]['path'])['type'] == 'directory':
                path = self.targets[key]['path']
                if not any(path.iterdir()):path.rmdir()
        if current != yaml.safe_load(self.config.read_text(encoding='utf-8-sig')):write_json(self.config,current)
        for key in wanted_keys - {'source/'+scope}:
            prior = record['ownership']['entries'].get(key)
            if prior is None:self.state['entries'].pop(key,None)
            else:self.state['entries'][key]=prior
        self.save_state()

    def check_scoped_topology(self,record,scope):
        saved = record.get('working_config', record['config'])
        saved_targets = {key for key, target in saved['targets'].items()
                         if scope in target.get('skills', target).get('include', [])}
        current_targets = {key for key, target in self.targets.items() if scope in target['names']}
        if saved_targets != current_targets:
            raise ValueError('Skill target membership changed since this recovery point')
        for key in saved_targets:
            target = saved['targets'][key].get('skills', saved['targets'][key])
            if pathlib.Path(target['path']).absolute() != self.targets[key]['path'] or target.get('mode', saved.get('mode','merge')) != self.targets[key]['mode']:
                raise ValueError('Skill target path or mode changed since this recovery point')
        if pathlib.Path(saved['source']).absolute() != self.source:
            raise ValueError('Skill source path changed since this recovery point')
        return saved_targets

    def restore_scoped_files(self,dest,record,scope,wanted_keys):
        entries = [row for row in record['entries'] if row['key'] in wanted_keys]
        if {row['key'] for row in entries} != wanted_keys or len(entries) != len(wanted_keys):
            raise ValueError('Recovery point lacks a complete snapshot for '+scope)
        provenance = next((row for row in record['entries'] if row['key']=='management/provenance'), None)
        if provenance is None:
            raise ValueError('Recovery point lacks its provenance snapshot')
        backup = dest/'files'/'management/provenance'
        old_source = None
        if provenance['state']['type'] == 'file':
            if hashlib.sha256(backup.read_bytes()).hexdigest() != provenance['state']['sha256']:
                raise ValueError('Provenance backup hash mismatch')
            old_source = read_json(backup, {}).get(scope)
        elif provenance['state']['type'] != 'missing':
            raise ValueError('Unsupported provenance snapshot type')
        ledger_path = self.config.parent/'skill-sources.json'
        ledger = read_json(ledger_path, {})
        self.restore_entries(dest,entries)
        if old_source is None:ledger.pop(scope,None)
        else:ledger[scope]=old_source
        if ledger != read_json(ledger_path, {}):write_json(ledger_path,ledger)

    def check_full_restore_topology(self,record):
        historical_names={n for t in record['config']['targets'].values() for n in t.get('skills',t).get('include',[])}
        later_names=set(self.names)-historical_names
        old_targets=record['config']['targets']
        if set(old_targets)!=set(self.targets) or pathlib.Path(record['config']['source'])!=self.source:
            raise ValueError('Scope/topology changed; review a scoped migration before restoring this checkpoint')
        for key,t in self.targets.items():
            old=old_targets[key].get('skills',old_targets[key])
            if pathlib.Path(old['path'])!=t['path'] or old.get('mode',record['config'].get('mode','merge'))!=t['mode']:
                raise ValueError('Target path or mode changed; review before restoring this checkpoint')
            if set(old['include']) != set(t['names'])-later_names:
                raise ValueError('Existing skill target membership changed; review before restoring this checkpoint')
        if later_names:
            if any(not any(k.endswith('/'+n) for k in self.state['entries']) for n in later_names):raise ValueError('Later configured sources have unresolved ownership; preserve before restoring')
        return later_names

    def restore_record(self,dest,record,preserve_unmanaged=None):
        scope = self.restore_scope(record)
        if scope:
            self.scoped_restore(dest,record,scope)
            if self.unmanaged() != (record['unmanaged'] if preserve_unmanaged is None else preserve_unmanaged):
                raise RuntimeError('Unmanaged entries changed; retained evidence, manual review required')
            return
        later_names=self.check_full_restore_topology(record)
        previous_state=json.loads(json.dumps(self.state))
        later_origins={n:v for n,v in read_json(self.config.parent/'skill-sources.json',{}).items() if n in later_names}
        self.restore_entries(dest,record['entries'])
        restored_config=json.loads(json.dumps(record["config"]))
        for key,t in self.targets.items():
            if key not in restored_config['targets']:continue
            sc=restored_config['targets'][key].get('skills',restored_config['targets'][key])
            extra=[n for n in t['names'] if n in later_names]
            sc['include']=list(dict.fromkeys(sc.get('include',[])+extra))
            sc['exclude']=list(dict.fromkeys(sc.get('exclude',[])+[n for n in extra if n in t['exclude']]))
        if later_origins:
            origins=read_json(self.config.parent/'skill-sources.json',{});origins.update(later_origins);write_json(self.config.parent/'skill-sources.json',origins)
        if record.get('native_invocation'):native_invocation.restore(self,record['native_invocation'])
        elif self.cfg.get('x-manage-native-invocation'):restored_config['x-manage-native-invocation']=self.cfg['x-manage-native-invocation']
        if self.cfg.get('x-tested-application-runtimes'):restored_config['x-tested-application-runtimes']=self.cfg['x-tested-application-runtimes']
        protected=self.cfg.get('x-preserved-sources',{})
        restored_names={n for t in restored_config['targets'].values() for n in t.get('skills',t).get('include',[])}
        if protected:restored_config['x-preserved-sources']={**restored_config.get('x-preserved-sources',{}),**{n:v for n,v in protected.items() if n not in restored_names}}
        write_json(self.config,restored_config)
        self.state=json.loads(json.dumps(record['ownership']))
        self.state['entries'].update({k:v for k,v in previous_state['entries'].items() if k.rsplit('/',1)[-1] in later_names})
        self.save_state()
        if self.unmanaged()!=(record["unmanaged"] if preserve_unmanaged is None else preserve_unmanaged):raise RuntimeError("Unmanaged entries changed; retained evidence, manual review required")
        for key,kind in record.get('roots',{}).items():
            path=self.targets[key]['path']
            if kind['type']=='missing' and entry_type(path)['type']=='directory':
                if any(path.iterdir()):raise RuntimeError('New root acquired unowned data; retained for review')
                path.rmdir()
    def update_state(self):
        self.state["entries"]={key:full_state(p) for key,p in self.owned_paths() if not key.startswith(("source/","management/"))}
        self.state["config_targets"]={k:str(t["path"]) for k,t in self.targets.items()};self.save_state()
    def recovery_point(self,ident):
        if not ident or not re.fullmatch(r"[0-9-]+[a-f0-9]*",ident):raise ValueError("Exact transaction ID required")
        dest=self.base/"transactions"/ident;record=read_json(dest/"journal.json")
        if not record or record["status"]=="preparing":raise ValueError("No complete restorable snapshot")
        pending=read_json(self.base/'active.json')
        if pending and pending['id']!=ident:
            raise ValueError('Recover the currently pending transaction first: '+pending['id'])
        if not self.restore_scope(record):self.check_full_restore_topology(record)
        return dest, record

    def restore_preview(self, ident):
        _, record = self.recovery_point(ident)
        scope = self.restore_scope(record)
        self.manager_for_restore(record)
        entries = [row for row in record['entries'] if not scope or row['key'].rsplit('/',1)[-1] == scope]
        return {'exit':0,'restore_scope':scope or 'full-library','checkpoint':ident,
                'restore_entries':[row['key'] for row in entries],
                'changed_entries':[row['key'] for row in entries if not equivalent(full_state(pathlib.Path(row['path'])),row['state'])],
                'pending_recovery':read_json(self.base/'active.json')}

    def manager_for_restore(self,record):
        scope=self.restore_scope(record)
        pending=read_json(self.base/'active.json')
        manager=self
        if scope and scope not in self.names and record.get('working_config'):
            # During an interrupted import the source may exist before its registration does.
            working=json.loads(json.dumps(self.cfg))
            for key,target in record['working_config']['targets'].items():
                if scope in target.get('skills',target).get('include',[]):
                    entry=working['targets'][key]
                    entry.get('skills',entry)['include'].append(scope)
            if not pending or pending['id']!=record['id']:
                raise ValueError('This skill is no longer registered; only the active import can be recovered')
            manager=Manager(self.config,working)
        if scope and scope not in manager.names:raise ValueError('Recovery skill no longer registered')
        if scope:manager.check_scoped_topology(record,scope)
        return manager

    def restore(self,ident):
        dest,record=self.recovery_point(ident)
        scope=self.restore_scope(record)
        pending=read_json(self.base/'active.json')
        manager=self.manager_for_restore(record)
        preservation,current=manager.capture('before-explicit-restore',scope)
        current['restore_requested']=ident
        write_json(preservation/'journal.json',current)
        if pending is None:
            write_json(self.base/'active.json',{'id':current['id'],'operation':'restore','requested':ident})
        try:
            manager.restore_record(dest,record,preserve_unmanaged=current['unmanaged'])
            current['status']='restore-completed';write_json(preservation/'journal.json',current)
            if pending:
                record.update(status='rolled-back', rollback_receipt=current['id'])
                write_json(dest/'journal.json',record)
            (self.base/'active.json').unlink(missing_ok=True)
            return {'exit':0,'restored':ident,'restore_scope':scope or 'full-library',
                    'replaced_content_preserved':str(preservation),**manager.cleanup_after_commit(record)}
        except Exception as exc:
            current.update(status='recovery-required',error=str(exc))
            write_json(preservation/'journal.json',current)
            return {'exit':3,'error':str(exc),'recovery_checkpoint':pending['id'] if pending else current['id'],'requested_restore':ident}

    def recover(self, preview=False):
        pending = read_json(self.base/'active.json')
        if pending is None:
            return {'exit':0,'result':'No interrupted operation'}
        if preview:
            return {**self.restore_preview(pending['id']), 'result':'Preview of rollback for the interrupted operation'}
        return self.restore(pending['id'])

    def cleanup_after_commit(self,record):
        try:self.clean_staging(record)
        except OSError as exc:return {'cleanup_warnings':[str(exc)]}
        return {}

    def transaction(self,label,work,scope=None,previous_config=None):
        pending=read_json(self.base/"active.json")
        if pending:raise RuntimeError("Incomplete transaction: run restore "+pending["id"])
        dest,record=self.capture(label,scope,previous_config)
        write_json(self.base/'active.json',{'id':record['id'],'operation':label,'skill':scope})
        try:
            result=work(dest,record)
            if self.unmanaged()!=record["unmanaged"]:raise RuntimeError("Unmanaged entry changed")
            record["status"]="committed"
            write_json(dest/"journal.json",record);(self.base/"active.json").unlink()
        except Exception as e:
            record["error"]=str(e)
            # Sync and repair never edit canonical sources. A failed run must not undo an editor's work.
            changed_sources=[]
            if label in ('sync','repair'):
                for row in record['entries']:
                    if row['key'].startswith('source/'):
                        try:unchanged=equivalent(full_state(row['path']),row['state'])
                        except OSError:unchanged=False
                        if not unchanged:changed_sources.append(row['key'])
            if isinstance(e,ConcurrentTargetChange) or changed_sources:
                if changed_sources:record['error']+='; concurrent source changes preserved: '+', '.join(changed_sources)
                record['status']='recovery-required'
                write_json(dest/'journal.json',record)
                return {'exit':3,'transaction':record['id'],'result':record['status'],'error':record['error']}
            try:
                recovery,_=self.capture('failed-state-preserved',scope)
                record["failure_capture"]=str(recovery)
                self.restore_record(dest,record);record["status"]="rolled-back"
                write_json(dest/'journal.json',record)
                (self.base/"active.json").unlink(missing_ok=True)
            except Exception as rexc:record["status"]="recovery-required";record["recovery_error"]=str(rexc)
            write_json(dest/"journal.json",record)
            return {"exit":3,"transaction":record["id"],"result":record["status"],"error":str(e),"recovery_error":record.get("recovery_error")}
        return {'exit':0,'transaction':record['id'],**result,**self.cleanup_after_commit(record)}

    def distribution_check(self, plan, scope):
        if not scope:
            return plan
        skipped={row['target']+'/'+row['skill'] for row in plan['rows'] if row['skill']!=scope}
        return {**plan,'errors':[e for e in plan['errors'] if e.partition(':')[0] not in skipped],
                'actions':[a for a in plan['actions'] if a.get('skill')==scope],
                'rows':[row for row in plan['rows'] if row['skill']==scope]}

    def distribute(self,dest,record,plan=None):
        scope=record.get('skill_scope')
        if plan is None:plan=self.distribution_check(self.inspect(strict=False,check_runtimes=False),scope)
        if plan['errors']:raise RuntimeError('Cannot distribute: '+str(plan['errors']))
        if plan['actions']:
            record['native_command']=self.stage_and_commit(dest,record,plan['actions'])
        if not scope and any(a['action']=='normalize-native-invocation' for a in plan['actions']):
            record['native_invocation_patch']=native_invocation.normalize(self)
        check=self.distribution_check(self.inspect(allow_new=True,check_runtimes=False),scope)
        if check['errors']:raise RuntimeError('Post-sync verification: '+str(check['errors']))
        if scope:
            for key,path in self.owned_paths():
                if not key.startswith(('source/','management/')) and key.rsplit('/',1)[-1]==scope:
                    self.state['entries'][key]=full_state(path)
            self.state['config_targets']={k:str(t['path']) for k,t in self.targets.items()}
            self.save_state()
        else:self.update_state()
        return {'result':'synchronized and verified',**check,
                'application_evidence_warnings':plan['application_evidence_warnings']}

    def sync(self,repair=False,scope=None):
        pending=read_json(self.base/'active.json')
        if pending:raise RuntimeError('Incomplete transaction: run restore '+pending['id'])
        if scope and scope not in self.names:raise ValueError('Unknown managed skill')
        plan=self.distribution_check(self.inspect(strict=False,repair=repair),scope)
        if plan['errors']:return {'exit':2,**plan}
        if not plan['actions']:return {'exit':0,'result':'already synchronized',**plan}
        return self.transaction('repair' if repair else 'sync',lambda d,r:self.distribute(d,r,plan),scope)

    def reconcile(self, preview=False):
        """Catch up unchanged managed copies, leaving other work for explicit review."""
        pending = read_json(self.base/'active.json')
        if pending:
            return {'exit':3,'incomplete_transaction':pending,'error':'Recover the interrupted operation first'}
        plan = self.inspect(strict=False)
        eligible = {}
        for name in sorted({row['skill'] for row in plan['rows'] if row['delivery_state']=='stale_copy'}):
            scoped = self.distribution_check(plan,name)
            if not scoped['errors'] and all(action['delivery_state']=='stale_copy' for action in scoped['actions']):
                eligible[name] = scoped
        attention = [row for row in plan['rows'] if row['delivery_state'] not in ('current','disabled')]
        ready = [row for row in attention if row['skill'] in eligible]
        blocked = [row for row in attention if row['skill'] not in eligible]
        result = {'exit':2 if blocked or plan['errors'] else 0, 'ready':ready, 'blocked':blocked,
                  'errors':plan['errors'], 'application_evidence_warnings':plan['application_evidence_warnings'],
                  'transactions':[], 'synchronized':[], 'cleanup_warnings':[]}
        if preview:
            return result
        for name, scoped in eligible.items():
            applied = self.transaction('sync',lambda dest,record:self.distribute(dest,record,scoped),name)
            if applied.get('transaction'):
                result['transactions'].append({'skill':name,'id':applied['transaction'],'status':applied['result']})
            if applied['exit']:
                return {**result, **{key:applied[key] for key in ('exit','error','recovery_error') if key in applied},
                        'incomplete_transaction':read_json(self.base/'active.json')}
            updated_keys = {(action['target'], action['skill']) for action in scoped['actions']}
            result['synchronized'].extend(row for row in applied['rows'] if (row['target'],row['skill']) in updated_keys)
            result['ready'] = [row for row in result['ready'] if row['skill']!=name]
            result['cleanup_warnings'].extend(applied.get('cleanup_warnings',[]))
        return result

    def toggle(self,name,enabled):
        clean_name(name)
        if name not in self.names:raise ValueError("Unknown managed skill")
        check=self.distribution_check(self.inspect(),name)
        if check["errors"]:return {"exit":2,**check}
        desired=json.loads(json.dumps(self.cfg))
        for item in desired["targets"].values():
            sc=item.get("skills",item)
            if name in sc.get("include",[]):
                values=set(sc.get("exclude",[]))
                if enabled:values.discard(name)
                else:values.add(name)
                sc["exclude"]=sorted(values)
        if desired==self.cfg:return {'exit':0,'result':'already in requested state','skill':name,'enabled':enabled}
        updated=Manager(self.config,desired)
        def toggle_distribution(dest,record):
            write_json(self.config,desired)
            return {**updated.distribute(dest,record),'configuration_checkpoint':record['id'],'skill':name,'enabled':enabled,
                    'application_evidence_warnings':check['application_evidence_warnings']}
        return updated.transaction('before-enable' if enabled else 'before-disable',toggle_distribution,name,self.cfg)

def main():
    p=argparse.ArgumentParser(description="Unified scoped Skill management")
    p.add_argument('--version',action='version',version='skill-manager '+VERSION+'; skillshare 0.28.4')
    p.add_argument("--config",default=str(ROOT/"skillshare.yaml"))
    p.add_argument("command",choices=["status","plan","sync","reconcile-plan","reconcile","repair","adopt","checkpoint","restore-plan","restore","recover-plan","recover","history","enable","disable"])
    p.add_argument("value",nargs="?")
    p.add_argument('--skill',help='Scope plan, sync, repair or checkpoint to one registered skill')
    args=p.parse_args();start=time.perf_counter();m=Manager(args.config)
    with m.lock():
        m=Manager(args.config)
        if args.skill and (args.command not in ('plan','sync','repair','checkpoint') or args.skill not in m.names):
            raise ValueError('Invalid command or skill scope')
        if args.command in ("status","plan"):
            result=m.distribution_check(m.inspect(strict=args.command=="status"),args.skill)
            result['distribution_exit']=2 if result['errors'] else 0
            result['exit']=result['distribution_exit']
            if args.command=='status' and result['application_evidence_warnings']:result['exit']=2
            pending=read_json(m.base/'active.json')
            if pending:result.update(exit=3,distribution_exit=3,incomplete_transaction=pending)
        elif args.command=="adopt":
            # Only identical sources can be adopted. Non-identical contents remain unowned.
            if read_json(m.base/'active.json'):raise ValueError('Recover the incomplete transaction before adopting')
            result=m.inspect(strict=True)
            errors=[e for e in result["errors"] if "unowned collision; adopt" not in e]
            if errors:result={"exit":2,"errors":errors}
            else:m.update_state();result={"exit":0,"result":"identical configured entries adopted; no target writes"}
        elif args.command in ("sync","repair"):result=m.sync(args.command=="repair",args.skill)
        elif args.command in ('reconcile-plan','reconcile'):result=m.reconcile(preview=args.command=='reconcile-plan')
        elif args.command in ("enable","disable"):result=m.toggle(args.value or "",args.command=="enable")
        elif args.command=="checkpoint":
            if read_json(m.base/'active.json'):raise ValueError('Recover the incomplete transaction before creating a new checkpoint')
            check=m.distribution_check(m.inspect(),args.skill)
            if check["errors"]:result={"exit":2,**check}
            else:
                dest,rec=m.capture(args.value or "checkpoint",args.skill);rec["status"]="checkpoint";write_json(dest/"journal.json",rec)
                result={"exit":0,"checkpoint":rec["id"],'application_evidence_warnings':check['application_evidence_warnings']}
        elif args.command=="history":result={"exit":0,"transactions":[{**{k:v for k,v in record.items() if k in ('id','label','status','error','utc')},'scope':m.restore_scope(record) or 'full-library'} for f in sorted((m.base/'transactions').glob('*/journal.json')) if (record:=read_json(f))]}
        elif args.command=='restore-plan':result=m.restore_preview(args.value)
        elif args.command in ('recover-plan','recover'):result=m.recover(preview=args.command=='recover-plan')
        else:
            result=m.restore(args.value)
    result["seconds"]=round(time.perf_counter()-start,6)
    write_json(m.base/"operations"/(dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")+".json"),{"command":sys.argv,"result":result,"python":sys.version,"yaml_version":yaml.__version__,'manager_version':VERSION,'skillshare_version':'0.28.4','skillshare_exe_sha256':EXE_SHA})
    print(json.dumps(result,ensure_ascii=False,indent=2));return result["exit"]

if __name__=="__main__":
    try:sys.exit(main())
    except Exception as e:print(json.dumps({"exit":4,"error":str(e)},ensure_ascii=False));sys.exit(4)
