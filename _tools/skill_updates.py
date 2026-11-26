"""Explicit import/update review. Sources with unknown provenance are never auto-updated."""
import argparse,difflib,hashlib,json,os,pathlib,re,shutil,subprocess,sys,uuid,urllib.parse,urllib.request
from management_inventory import entry_type,manifest,digest
from install_security import inspect_candidate
from skill_manager import ROOT,ConcurrentTargetChange,Manager,clean_name,read_json,skill_identity,write_json

def validate(path):
 if entry_type(path)['type']!='directory':raise ValueError('Candidate must be a real directory')
 if entry_type(path/'.git')['type']!='missing':raise ValueError('Use fetch for a Git repository or select a clean skill folder')
 identity=skill_identity(path)
 for entry in manifest(path):
  if entry['type'] not in ('file','directory'):raise ValueError('Linked candidate content requires separate review')
  if '.git' in pathlib.PurePosixPath(entry['path'].casefold()).parts:raise ValueError('Git metadata is not skill content; use fetch for a repository or select a clean skill folder')
 if (path/'.tabbit-dance-managed').exists():raise ValueError('Tool-owned candidate must remain under its existing owner')
 return identity

def prepare(m,name,candidate,origin,expected_base=None):
 clean_name(name);fm=validate(candidate)
 if (m.source/name/'SKILL.md').is_file() and fm!=validate(m.source/name):raise ValueError('Candidate changes skill identity; use a separately reviewed import')
 ident=uuid.uuid4().hex[:16];dest=m.base/'updates'/ident;dest.mkdir(parents=True)
 shutil.copytree(candidate,dest/'candidate')
 new=manifest(dest/'candidate');old=manifest(m.source/name) if (m.source/name).is_dir() else []
 if expected_base is not None and old!=expected_base:raise ValueError('Installed source changed since this draft was opened; preserve and reconcile your draft first')
 if new!=manifest(candidate):raise RuntimeError('Candidate backup hash mismatch')
 scan_required=expected_base is None
 security=None
 if scan_required:
  security=inspect_candidate(dest/'candidate',m.base/'security-scans',skill=True,expected_sha=digest(new))
  if security['status']!='passed':return {'exit':2,'error':'Candidate failed the install scan','security':security}
 a={f['path']:f for f in old};b={f['path']:f for f in new};changed=[p for p in sorted(set(a)|set(b)) if a.get(p)!=b.get(p)]
 patch=[]
 for p in changed:
  before=m.source/name/p;after=dest/'candidate'/p
  if pathlib.Path(p).suffix in ('.md','.py','.ps1','.js','.ts','.json','.yaml','.yml','.txt') and max(before.stat().st_size if before.is_file() else 0,after.stat().st_size if after.is_file() else 0)<1_000_000:
   patch.extend(difflib.unified_diff(before.read_text(encoding='utf-8',errors='replace').splitlines(True) if before.is_file() else [],after.read_text(encoding='utf-8',errors='replace').splitlines(True) if after.is_file() else [],fromfile='installed/'+p,tofile='candidate/'+p))
 (dest/'review.diff').write_text(''.join(patch),encoding='utf-8')
 record={'id':ident,'skill':name,'origin':origin,'installed_manifest':old,'candidate_manifest':new,'changed_files':changed,'status':'prepared-only','review_diff':str(dest/'review.diff'),'candidate_tree_sha256':digest(new),'scan_required':scan_required}
 write_json(dest/'review.json',record)
 return {'exit':0,'update':ident,'skill':name,'changed_files':changed,'origin':origin,'diff':str(dest/'review.diff'),'security':security,'result':'Prepared for review; installed content unchanged'}

def create_draft(manager, name):
    clean_name(name)
    if name not in manager.names:
        raise ValueError('Unknown managed skill')
    source = manager.source/name
    validate(source)
    baseline = manifest(source)
    ident = uuid.uuid4().hex[:16]
    draft = manager.base/'drafts'/ident
    candidate = draft/'candidate'
    shutil.copytree(source, candidate)
    if manifest(candidate) != baseline or manifest(source) != baseline:
        raise ConcurrentTargetChange('Installed source changed while creating the draft')
    write_json(draft/'draft.json', {'skill':name, 'installed_manifest':baseline})
    return {'exit':0, 'draft':ident, 'candidate':str(candidate), 'result':'Edit only the candidate; review the draft before applying'}


def review_draft(manager, ident):
    if not ident or not re.fullmatch('[a-f0-9]{16}', ident):
        raise ValueError('Exact draft ID required')
    draft = manager.base/'drafts'/ident
    record = read_json(draft/'draft.json')
    if not record or record['skill'] not in manager.names:
        raise ValueError('Draft or managed skill unavailable')
    name = record['skill']
    if manifest(draft/'candidate') == record['installed_manifest']:
        return {'exit':2, 'error':'Draft has no changes'}
    ledger = read_json(manager.config.parent/'skill-sources.json', {})
    origin = {**ledger.get(name, {'type':'local-edit','upstream':'unknown'}), 'local_edit':True}
    return prepare(manager, name, draft/'candidate', origin, expected_base=record['installed_manifest'])


def import_skill(m, args):
 candidate=pathlib.Path(args.candidate).absolute()
 name=clean_name(args.name)
 identity=validate(candidate)
 source=m.source/name
 registering=entry_type(source)['type']=='directory' and candidate.resolve()==source.resolve()
 if name.casefold() in {n.casefold() for n in m.names} or (entry_type(source)['type']!='missing' and not registering):
  raise ValueError('Existing source; prepare an update instead. Never overwrite on import.')
 if name in m.cfg.get('x-preserved-sources',{}):raise ValueError('Preserved source cannot be imported')
 groups=m.application_groups()
 if args.applications:
  apps=[value.strip() for value in args.applications.split(',')]
  if any(app not in groups for app in apps):raise ValueError('Unknown application; choices: '+', '.join(groups))
  keys=list(dict.fromkeys(key for app in apps for key in groups[app]))
 else:
  keys=list(dict.fromkeys(key.strip() for key in args.targets.split(','))) if args.targets else list(m.targets)
 if any(key not in m.targets for key in keys):raise ValueError('Unknown configured target')
 for key in keys:
  if entry_type(m.targets[key]['path']/name)['type']!='missing':raise ValueError('Unowned target collision: '+key+'/'+name)
 check=m.distribution_check(m.inspect(allow_unregistered={name} if registering else ()),name)
 if check['errors']:return {'exit':2,**check}
 desired=json.loads(json.dumps(m.cfg))
 for key in keys:
  target=desired['targets'][key]
  target.get('skills',target)['include'].append(name)
 planned=Manager(m.config,desired)
 source_names={n:skill_identity(m.source/n) for n in m.names}
 source_names[name]=identity
 conflicts=planned.catalog_conflicts(source_names)
 if conflicts:return {'exit':2,'errors':conflicts}
 staged=m.base/'imports'/uuid.uuid4().hex[:16]/'candidate'
 shutil.copytree(candidate,staged)
 candidate_manifest=manifest(staged)
 if manifest(candidate)!=candidate_manifest:raise RuntimeError('Import copy mismatch')
 security=inspect_candidate(staged,m.base/'security-scans',skill=True,expected_sha=digest(candidate_manifest))
 if security['status']!='passed':return {'exit':2,'error':'Candidate failed the install scan','security':security}
 effective=[app for app,app_keys in groups.items() if set(keys).intersection(app_keys)]

 def install(dest,record):
  if manifest(staged)!=candidate_manifest or (registering and manifest(source)!=candidate_manifest):
   raise ConcurrentTargetChange('Import candidate changed after scanning')
  if not registering:os.replace(staged,source)
  write_json(m.config,desired)
  result=planned.distribute(dest,record)
  ledger_path=m.config.parent/'skill-sources.json'
  ledger=read_json(ledger_path,{})
  ledger[name]={'type':'local-import','location':str(candidate),'upstream':args.origin or 'unknown','tree_sha256':digest(candidate_manifest)}
  write_json(ledger_path,ledger)
  return {**result,'security':security,'effective_applications':effective,'application_evidence_warnings':check['application_evidence_warnings']}

 return planned.transaction('before-import-'+name,install,name,m.cfg)


def main():
 p=argparse.ArgumentParser();p.add_argument('--config',default=str(ROOT/'skillshare.yaml'));p.add_argument('command',choices=['sources','draft','review','prepare','fetch','fetch-manifest','apply','import']);p.add_argument('name',nargs='?');p.add_argument('candidate',nargs='?');p.add_argument('--origin');p.add_argument('--ref');p.add_argument('--subdir',default='.');selection=p.add_mutually_exclusive_group();selection.add_argument('--targets');selection.add_argument('--applications');a=p.parse_args()
 m=Manager(a.config)
 with m.lock():
  m=Manager(a.config)
  if read_json(m.base/'active.json'):raise RuntimeError('Recover the incomplete transaction before updating')
  if a.command=='sources':
   known=read_json(m.config.parent/'skill-sources.json',{})
   result={'exit':0,'sources':[{'skill':n,'origin':known.get(n,{'type':'local-adopted','upstream':'unknown; not safe to infer','tree_sha256':digest(manifest(m.source/n))})} for n in m.names]}
  elif a.command=='draft':result=create_draft(m,a.name or '')
  elif a.command=='review':result=review_draft(m,a.name)
  elif a.command=='prepare':result=prepare(m,a.name,pathlib.Path(a.candidate).absolute(),{'type':'user-supplied-candidate','location':str(pathlib.Path(a.candidate).absolute()),'upstream':a.origin or 'unknown'})
  elif a.command=='fetch-manifest':
   clean_name(a.name);url=urllib.parse.urlsplit(a.candidate or '')
   if url.scheme!='https' or url.username or url.password or url.query or url.fragment:raise ValueError('Clean HTTPS manifest URL required')
   checkout=m.base/'updates'/('manifest-'+uuid.uuid4().hex[:12]);checkout.mkdir(parents=True);candidate=checkout/'candidate';candidate.mkdir()
   receipts=[]
   def download(address,limit):
    with urllib.request.urlopen(urllib.request.Request(address,headers={'User-Agent':'skill-management/1.0'}),timeout=25) as response:
     if urllib.parse.urlsplit(response.url).hostname!=url.hostname:raise ValueError('Unexpected publisher change during download')
     data=response.read(limit+1)
     if len(data)>limit:raise ValueError('Published file exceeds size limit')
     receipts.append({'url':address,'final_url':response.url,'status':response.status,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()});return data
   raw=download(a.candidate,128000);entries=[]
   for line in raw.decode('utf-8-sig').splitlines():
    if not line.strip() or line.lstrip().startswith('#'):continue
    found=re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?(.+)',line.strip())
    if not found:raise ValueError('Unsupported manifest line')
    sha,name=found.groups();relative=pathlib.PurePosixPath(urllib.parse.unquote(name))
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:raise ValueError('Unsafe published path')
    entries.append((sha.lower(),name))
   if not 1<=len(entries)<=100 or len({n for _,n in entries})!=len(entries):raise ValueError('Invalid published file set')
   for sha,name in entries:
    data=download(urllib.parse.urljoin(a.candidate.rsplit('/',1)[0]+'/',name),5_000_000)
    if hashlib.sha256(data).hexdigest()!=sha:raise ValueError('Published SHA-256 mismatch: '+name)
    path=candidate/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
   write_json(checkout/'download-receipts.json',receipts)
   result=prepare(m,a.name,candidate,{'type':'https-sha256-manifest','manifest_url':a.candidate,'manifest_sha256':hashlib.sha256(raw).hexdigest(),'files':[{'path':n,'sha256':h} for h,n in entries]})
   write_json(m.base/'updates'/result['update']/'download-receipts.json',receipts)
  elif a.command=='fetch':
   if not a.ref:raise ValueError('An explicit immutable commit or release tag is required')
   if not a.candidate or not (a.candidate.startswith('https://') or pathlib.Path(a.candidate).is_dir()):raise ValueError('Use an HTTPS repository URL or an existing local Git repository')
   if a.candidate.startswith('https://'):
    url=urllib.parse.urlsplit(a.candidate)
    if url.username or url.password or url.query or url.fragment:raise ValueError('Use a clean repository URL without credentials or access-bearing query parameters')
   clean_name(a.name);checkout=m.base/'updates'/('checkout-'+uuid.uuid4().hex[:12]);checkout.mkdir(parents=True)
   env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}
   empty_hooks=checkout/'no-hooks';empty_hooks.mkdir()
   commands=[['git','-c','core.hooksPath='+str(empty_hooks),'clone','--no-checkout','--filter=blob:none',a.candidate,str(checkout/'repo')],['git','-C',str(checkout/'repo'),'-c','core.hooksPath='+str(empty_hooks),'checkout','--detach',a.ref]]
   receipts=[]
   for command in commands:
    r=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,timeout=120);receipts.append({'command':command,'exit':r.returncode,'stderr':r.stderr})
    if r.returncode:write_json(checkout/'commands.json',receipts);raise RuntimeError('Fetch/checkout failed; existing skill unchanged')
   commit=subprocess.check_output(['git','-C',str(checkout/'repo'),'rev-parse','HEAD'],text=True,env=env).strip();repo=(checkout/'repo').resolve();candidate=(repo/a.subdir).resolve()
   if not candidate.is_relative_to(repo):raise ValueError('Candidate subdir escapes repository')
   if candidate==repo:(repo/'.git').rename(checkout/'git-metadata')
   result=prepare(m,a.name,candidate,{'type':'git','repository':a.candidate,'requested_ref':a.ref,'commit':commit,'subdir':a.subdir})
   write_json(m.base/'updates'/result['update']/'fetch-commands.json',receipts)
  elif a.command=='import':result=import_skill(m,a)
  else:
   if not a.name or not re.fullmatch('[a-f0-9]{16}',a.name):raise ValueError('Exact prepared update ID required')
   dest=m.base/'updates'/a.name;review=read_json(dest/'review.json')
   if not review:raise ValueError('No prepared candidate')
   name=review['skill']
   if name not in m.names:raise ValueError('Import new skills explicitly instead')
   if manifest(dest/'candidate')!=review['candidate_manifest']:raise ValueError('Candidate changed after review')
   if manifest(m.source/name)!=review['installed_manifest']:raise ValueError('Installed source changed after diff; prepare a fresh review')
   check=m.distribution_check(m.inspect(),name)
   if check['errors']:return output({'exit':2,**check})
   staged=dest/('verified-source-'+uuid.uuid4().hex[:8])
   shutil.copytree(dest/'candidate',staged)
   if manifest(staged)!=review['candidate_manifest']:raise RuntimeError('Staged source differs from reviewed candidate')
   security=None
   if review.get('scan_required',True):
    security=inspect_candidate(staged,m.base/'security-scans',skill=True,expected_sha=review['candidate_tree_sha256'])
    if security['status']!='passed':return output({'exit':2,'error':'Candidate failed the install scan','security':security})

   def replace_source(checkpoint,record):
    if manifest(m.source/name)!=review['installed_manifest']:
     raise ConcurrentTargetChange('Installed source changed during backup; review before updating')
    if manifest(staged)!=review['candidate_manifest']:
     raise ConcurrentTargetChange('Update candidate changed after scanning')
    os.replace(m.source/name,checkpoint/'previous-source')
    os.replace(staged,m.source/name)
    distributed=m.distribute(checkpoint,record)
    ledger_path=m.config.parent/'skill-sources.json'
    ledger=read_json(ledger_path,{})
    ledger[name]={**review['origin'],'tree_sha256':review['candidate_tree_sha256'],'known_good_checkpoint':record['id']}
    write_json(ledger_path,ledger)
    return {**distributed,'security':security,'known_good_checkpoint':record['id'],'application_evidence_warnings':check['application_evidence_warnings']}

   result=m.transaction('known-good-before-update-'+name,replace_source,name)
   if result['exit']==0:
    review.update(status='applied',known_good_checkpoint=result['known_good_checkpoint'])
    try:write_json(dest/'review.json',review)
    except OSError as exc:result['cleanup_warnings']=['Update committed; candidate receipt could not be annotated: '+str(exc)]
 return output(result)


def output(result):
 print(json.dumps(result,ensure_ascii=False,indent=2));return result['exit']

if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as e:print(json.dumps({'exit':4,'error':str(e)},ensure_ascii=False));raise SystemExit(4)
