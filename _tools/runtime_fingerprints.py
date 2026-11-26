"""Read-only invalidation of integration evidence when the tested app/runtime changes."""
import hashlib,pathlib,subprocess

def codex_desktop_backends():
 """Return only executable paths of app servers directly launched by this desktop host."""
 query=r"""
$ErrorActionPreference='Stop'
$rows=@(Get-CimInstance Win32_Process -Filter "Name='codex.exe' OR Name='ChatGPT.exe'")
$parents=@($rows | Where-Object Name -eq 'ChatGPT.exe' | Select-Object -ExpandProperty ProcessId)
$rows | Where-Object { $_.Name -eq 'codex.exe' -and $_.ParentProcessId -in $parents -and $_.CommandLine -match '\bapp-server\b' } | Select-Object -ExpandProperty ExecutablePath -Unique
"""
 result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                        '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);'+query],
                       capture_output=True,encoding='utf-8',check=True,timeout=12)
 return {pathlib.Path(line.strip()) for line in result.stdout.splitlines() if line.strip()}
def sha256(path):
 digest=hashlib.sha256()
 with pathlib.Path(path).open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
 return digest.hexdigest()
def inspect(config):
 errors=[];rows=[]
 for app,entry in config.get('x-tested-application-runtimes',{}).items():
  for file in entry['files']:
   path=pathlib.Path(file['path'])
   if path.suffix.lower() not in ('.exe','.asar','.cjs','.mjs'):errors.append(app+': unsupported program fingerprint path');continue
   try:
    current=sha256(path);matches=current==file['sha256']
    if not matches:errors.append(app+': application/runtime changed; prior discovery/use evidence must be refreshed')
    row={'application':app,'path':str(path),'sha256':current,'matches_tested_runtime':matches,'tested_version':entry['version']}
    if file.get('verify_running_codex_desktop'):
     try:
      active=codex_desktop_backends()
      row['running_desktop_paths']=sorted(str(p) for p in active)
      if active!={path}:
       row['matches_tested_runtime']=False
       errors.append(app+': currently running desktop backend is missing, different or ambiguous; check the active desktop version')
     except (OSError,subprocess.SubprocessError):
      row['matches_tested_runtime']=False
      errors.append(app+': unable to identify the active desktop backend')
    rows.append(row)
   except OSError as e:errors.append(app+': tested runtime unavailable: '+str(e))
 return errors,rows
