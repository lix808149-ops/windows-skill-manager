param([string]$Config=(Join-Path (Split-Path -Parent $PSScriptRoot) 'skillshare.yaml'),[string]$Out)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$timer=[Diagnostics.Stopwatch]::StartNew()
$errors=[Collections.Generic.List[string]]::new()
$rows=[Collections.Generic.List[object]]::new()
$hashCmdlet=Get-Command Get-FileHash -ErrorAction SilentlyContinue
$hashBackend=if($hashCmdlet){'Get-FileHash SHA256'}else{'.NET SHA256 FileStream (cmdlet unavailable)'}
function Get-IndependentSha([string]$Path) {
    if($hashCmdlet){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()}
    $stream=[IO.File]::OpenRead($Path);$algorithm=[Security.Cryptography.SHA256]::Create()
    try {return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}
    finally {$algorithm.Dispose();$stream.Dispose()}
}
function Normalize-Path([string]$Path) {
    if($Path.StartsWith('\\?\')){$Path=$Path.Substring(4)}
    return [IO.Path]::GetFullPath($Path).TrimEnd('\').ToLowerInvariant()
}
function Read-Manifest([string]$Path) {
    $base=[IO.Path]::GetFullPath($Path).TrimEnd('\')+'\'
    $queue=[Collections.Generic.Queue[string]]::new();$queue.Enqueue($Path)
    $items=[Collections.Generic.List[object]]::new()
    while($queue.Count -gt 0){
        $directory=$queue.Dequeue()
        foreach($item in (Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)){
            if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw ('Nested reparse point: '+$item.FullName)}
            if(-not $item.FullName.StartsWith($base,[StringComparison]::OrdinalIgnoreCase)){throw ('Enumeration escaped the lexical root: '+$item.FullName)}
            $rel=$item.FullName.Substring($base.Length).Replace('\','/')
            if($item.PSIsContainer){
                $items.Add([pscustomobject]@{path=$rel;type='directory';bytes=$null;sha256=$null});$queue.Enqueue($item.FullName)
            }else{
                $hash=Get-IndependentSha $item.FullName
                $items.Add([pscustomobject]@{path=$rel;type='file';bytes=$item.Length;sha256=$hash})
            }
        }
    }
    return @($items | Sort-Object path)
}
try {
    # The manager intentionally emits the JSON subset of YAML, allowing this independent parser.
    $cfg=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    if(-not $cfg.source -or -not $cfg.targets){throw 'Invalid canonical configuration'}
    foreach($targetProperty in $cfg.targets.PSObject.Properties){
        $key=$targetProperty.Name;$target=$targetProperty.Value
        $sc=if($target.skills){$target.skills}else{$target}
        $mode=if($sc.mode){$sc.mode}else{$cfg.mode}
        $root=Get-Item -LiteralPath $sc.path -Force -ErrorAction Stop
        if(-not $root.PSIsContainer -or (($root.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)){throw ('Invalid real target root: '+$key)}
        foreach($name in $sc.include){
            $src=Join-Path $cfg.source $name;$dst=Join-Path $sc.path $name
            $row=[ordered]@{target=$key;skill=$name;mode=$mode;path=$dst;enabled=($name -notin $sc.exclude);type=$null;link_target=$null;passed=$false;source_manifest=$null;target_manifest=$null;error=$null}
            try {
                $source=Get-Item -LiteralPath $src -Force -ErrorAction Stop
                if(-not $source.PSIsContainer -or (($source.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)){throw 'Source is not a real directory'}
                if(-not (Test-Path -LiteralPath (Join-Path $src 'SKILL.md') -PathType Leaf)){throw 'Missing source SKILL.md'}
                $row.source_manifest=@(Read-Manifest $src)
                $entry=Get-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
                if(-not $row.enabled){
                    if($entry){throw 'Disabled target still exists'}
                    $row.type='missing';$row.passed=$true
                }else{
                    if(-not $entry){throw 'Missing target skill'}
                    $reparse=(($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
                    $row.type=if($reparse){[string]$entry.LinkType}elseif($entry.PSIsContainer){'directory'}else{'file'}
                    if($mode -eq 'merge'){
                        if(-not $reparse -or $entry.LinkType -notin @('Junction','SymbolicLink')){throw 'Expected a directory link'}
                        $destination=[string]@($entry.Target)[0]
                        if(-not [IO.Path]::IsPathRooted($destination)){$destination=Join-Path $entry.Parent.FullName $destination}
                        $row.link_target=$destination
                        if((Normalize-Path $destination) -ne (Normalize-Path $src)){throw 'Wrong link target'}
                    }elseif($mode -eq 'copy'){
                        if($reparse -or -not $entry.PSIsContainer){throw 'Expected a real copy directory'}
                    }else{throw ('Unsupported per-entry mode: '+$mode)}
                    $row.target_manifest=@(Read-Manifest $dst)
                    $a=$row.source_manifest | ConvertTo-Json -Depth 6 -Compress
                    $b=$row.target_manifest | ConvertTo-Json -Depth 6 -Compress
                    if($a -cne $b){throw 'Complete file/directory list or SHA-256 differs'}
                    $row.passed=$true
                }
            }catch{$row.error=$_.Exception.Message;$errors.Add($key+'/'+$name+': '+$row.error)}
            $rows.Add([pscustomobject]$row)
        }
    }
}catch{$errors.Add($_.Exception.Message)}
$timer.Stop();$code=if($errors.Count){1}else{0}
$result=[pscustomobject]@{utc=[DateTime]::UtcNow.ToString('o');config=[IO.Path]::GetFullPath($Config);method='Independent Windows PowerShell/.NET directory types, link targets and SHA256; complete lists including directories; no Python manifest reuse';hash_backend=$hashBackend;powershell=$PSVersionTable.PSVersion.ToString();seconds=$timer.Elapsed.TotalSeconds;exit=$code;errors=@($errors);rows=@($rows.ToArray())}
if($Out){$result | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $Out -Encoding UTF8}
[pscustomobject]@{Exit=$code;Checked=$rows.Count;Passed=@($rows | Where-Object passed).Count;Seconds=$result.seconds;Errors=@($errors);Evidence=$Out} | ConvertTo-Json -Depth 5
exit $code
