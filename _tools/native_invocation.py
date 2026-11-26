"""Narrow adapter for the item-level WorkBuddy override change approved by the user.

Only approved non-secret skill override keys are journaled/restored. Account fields
are never exported; current unowned settings survive every patch and restore.
"""
import hashlib,json,os,pathlib,uuid,yaml

def canonical_hash(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def bindings(manager):
 for target,enabled in manager.cfg.get('x-manage-native-invocation',{}).items():
  if not enabled:continue
  if target not in manager.targets:raise ValueError('Unknown native invocation target')
  permit=manager.base/'native-permissions.json'
  if not permit.is_file():raise ValueError('Native override authorization record missing')
  data=json.loads(permit.read_text(encoding='utf-8'));approval=data.get(target)
  path=manager.targets[target]['path'].parent/'settings.json'
  if not approval or pathlib.Path(approval['settings_path']).absolute()!=path.absolute():raise ValueError('Native override path exceeds recorded item-level scope')
  names=[]
  for name in manager.targets[target]['names']:
   mapped=approval.get('directory_to_name',{}).get(name)
   if mapped in approval['names']:names.append(mapped);continue
   if name in approval['names']:names.append(name);continue
   skill_path=manager.source/name/'SKILL.md'
   if skill_path.is_file():
    fm=yaml.safe_load(skill_path.read_text(encoding='utf-8-sig').split('---',2)[1])
    if fm['name'] in approval['names']:names.append(fm['name'])
  yield target,path,sorted(set(names))

def selected(document,names):
 section=document.get('skillOverrides',{})
 if not isinstance(section,dict):raise ValueError('Unsupported native skillOverrides schema')
 return {name:{'present':name in section,'value':section.get(name)} for name in names}

def validate_entries(entries):
 for name,item in entries.items():
  if item['present'] and item['value'] not in ('user-invocable-only','disabled'):
   raise ValueError('Unsupported native override schema for '+name+'; value retained without exporting it')

def other_hash(document,names):
 clone=json.loads(json.dumps(document));section=clone.get('skillOverrides',{})
 for name in names:section.pop(name,None)
 return canonical_hash(clone)

def snapshot(manager):
 out={}
 for target,path,names in bindings(manager):
  raw=path.read_bytes();document=json.loads(raw.decode('utf-8-sig'))
  entries=selected(document,names);validate_entries(entries)
  out[target]={'path':str(path),'entries':entries,'file_sha256':hashlib.sha256(raw).hexdigest(),'unowned_semantic_sha256':other_hash(document,names)}
 return out

def inspect(manager,strict=True,repair=False):
 errors=[];actions=[];states=snapshot(manager)
 for target,state in states.items():
  present={name:entry['value'] for name,entry in state['entries'].items() if entry['present']}
  unknown={name:value for name,value in present.items() if value not in ('user-invocable-only','disabled')}
  if unknown:errors.append(target+': unsupported native override value; preserve and review '+', '.join(unknown))
  elif present:
   actions.append({'target':target,'action':'normalize-native-invocation','names':sorted(present)})
   if strict or not repair:errors.append(target+': native override prevents configured skill invocation: '+', '.join(sorted(present)))
 return errors,actions,states

def patch(path,desired,approved):
 path=pathlib.Path(path)
 if path.is_symlink() or path.absolute()!=path.resolve():raise ValueError('Native settings path is redirected; review first')
 if set(desired)-set(approved):raise ValueError('Native patch exceeds approved skill names')
 raw=path.read_bytes();doc=json.loads(raw.decode('utf-8-sig'));before=selected(doc,desired);validate_entries(before);validate_entries(desired);untouched=other_hash(doc,desired)
 if before==desired:return {'path':str(path),'result':'unchanged','unowned_semantic_sha256':untouched}
 section=doc.setdefault('skillOverrides',{})
 for name,item in desired.items():
  if item['present']:section[name]=item['value']
  else:section.pop(name,None)
 text=json.dumps(doc,ensure_ascii=False,indent=2)+'\n'
 if b'\r\n' in raw:text=text.replace('\n','\r\n')
 encoded=text.encode('utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8')
 temp=path.with_name('.skill-management-settings-'+uuid.uuid4().hex+'.tmp')
 try:
  with temp.open('xb') as f:f.write(encoded);f.flush();os.fsync(f.fileno())
  if path.read_bytes()!=raw:raise RuntimeError('Native settings changed concurrently; no patch applied')
  os.replace(temp,path)
  result=json.loads(path.read_text(encoding='utf-8-sig'))
  if selected(result,desired)!=desired or other_hash(result,desired)!=untouched:raise RuntimeError('Native settings post-write verification failed')
  return {'path':str(path),'before':before,'after':desired,'unowned_semantic_sha256':untouched,'file_sha256_before':hashlib.sha256(raw).hexdigest(),'file_sha256_after':hashlib.sha256(path.read_bytes()).hexdigest()}
 finally:temp.unlink(missing_ok=True)

def normalize(manager):
 receipts=[]
 for target,path,names in bindings(manager):receipts.append(patch(path,{n:{'present':False,'value':None} for n in names},names))
 return receipts

def restore(manager,states):
 allowed={target:(path,names) for target,path,names in bindings(manager)}
 receipts=[]
 for target,state in states.items():
  if target not in allowed:raise ValueError('Native restore target is not authorized')
  path,names=allowed[target]
  if path.absolute()!=pathlib.Path(state['path']).absolute():raise ValueError('Native restore path changed')
  receipts.append(patch(path,state['entries'],names))
 return receipts
