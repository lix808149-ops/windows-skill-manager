param([string]$Action='menu',[string]$Value,[string]$Candidate,[string]$Origin,[string]$Ref,[string]$Subdir='.',[string]$Skill,[string]$Applications,[string]$Targets,[string]$ExpectedSha256)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$taskRoot=Split-Path -Parent $PSScriptRoot
function Invoke-SkillAction([string]$Operation,[string]$Argument,[string]$SelectedSkill) {
    $arguments=@('-B','-X','utf8',(Join-Path $PSScriptRoot 'skill_manager.py'),$Operation)
    if($Argument){$arguments+=$Argument}
    if($SelectedSkill){$arguments+=@('--skill',$SelectedSkill)}
    $raw=& python @arguments
    $taskExit=$LASTEXITCODE
    try {$result=($raw -join "`n") | ConvertFrom-Json} catch {$raw | Out-Host; return $taskExit}
    if($result.rows){
        Write-Host '以下为文件分发与已纳管开关检查；菜单 15 另列每个应用的实测记录位置。'
        $result.rows | Group-Object target | ForEach-Object {
            $good=@($_.Group | Where-Object {$_.delivery_state -in @('current','disabled')}).Count
            [pscustomobject]@{Target=$_.Name;Checked=$_.Count;Passed=$good}
        } | Format-Table -AutoSize | Out-Host
        if($Operation -eq 'status'){
            Show-DeliveryState @($result.rows | Where-Object {$_.delivery_state -notin @('current','disabled')})
        }
    }
    if($result.ready){Write-Host '可安全补齐的副本';Show-DeliveryState $result.ready}
    if($result.synchronized){Write-Host '已补齐的副本';Show-DeliveryState $result.synchronized}
    if($result.blocked){Write-Host '其余待检查条目';Show-DeliveryState $result.blocked}
    if($result.actions){$result.actions | Format-Table -AutoSize | Out-Host}
    if($result.transactions){$result.transactions | Select-Object id,skill,scope,label,status,utc | Format-Table -AutoSize | Out-Host}
    if($result.restore_entries){
        Write-Host ('恢复范围：'+$result.restore_scope)
        if($result.changed_entries){$result.changed_entries | Out-Host}else{Write-Host '预览中没有文件差异，仍可能恢复启停、来源或配置。'}
    }
    foreach($warning in $result.application_evidence_warnings){Write-Warning $warning}
    foreach($warning in $result.cleanup_warnings){Write-Warning $warning}
    foreach($err in $result.errors){Write-Host $err -ForegroundColor Red}
    foreach($name in @('result','error','transaction','checkpoint','restored','configuration_checkpoint','replaced_content_preserved','recovery_error','recovery_checkpoint','incomplete_transaction')) {
        if($result.$name){Write-Host ($name+': '+($result.$name | ConvertTo-Json -Compress))}
    }
    if($null -ne $result.distribution_exit){Write-Host ('文件分发检查退出码：'+$result.distribution_exit)}
    if($result.incomplete_transaction){Write-Host '用菜单 18 预览并回退中断操作，或运行 recover-plan、recover。'}
    Write-Host ('Exit: '+$taskExit+'; elapsed: '+$result.seconds+'s')
    return $taskExit
}
function Show-DeliveryState($Rows) {
    $labels=@{current='已对齐';stale_copy='副本落后，可检查补齐';conflict='冲突，保留待审查';missing='目标缺失';withdraw_pending='停用待撤出';needs_sync='分发方式待检查'}
    if($Rows){
        $Rows | Select-Object target,skill,@{n='State';e={$labels[$_.delivery_state]}},path | Format-Table -AutoSize -Wrap | Out-Host
    }
}
function Invoke-UpdateAction([string]$Operation,[string]$Name,[string]$Path,[string]$SelectedApps) {
    $command=if($Operation -eq 'edit'){'draft'}else{$Operation}
    $arguments=@('-B','-X','utf8',(Join-Path $PSScriptRoot 'skill_updates.py'),$command)
    if($Name){$arguments+=$Name};if($Path){$arguments+=$Path}
    if($Operation -eq 'import' -and $SelectedApps){$arguments+=@('--applications',$SelectedApps)}
    if($Operation -eq 'import' -and $Targets){$arguments+=@('--targets',$Targets)}
    if($Origin){$arguments+=@('--origin',$Origin)};if($Ref){$arguments+=@('--ref',$Ref)};if($Subdir -ne '.'){$arguments+=@('--subdir',$Subdir)}
    $raw=& python @arguments; $taskExit=$LASTEXITCODE
    try {$r=($raw -join "`n") | ConvertFrom-Json} catch {$raw | Out-Host;return $taskExit}
    if($r.changed_files){Write-Host '候选改动（尚未应用）';$r.changed_files | Out-Host}
    if($r.origin){Write-Host ('来源: '+($r.origin | ConvertTo-Json -Depth 8 -Compress))}
    if($r.diff){Write-Host ('完整差异: '+$r.diff);Get-Content -LiteralPath $r.diff -Encoding UTF8 -TotalCount 160 | Out-Host;Write-Host '以上最多显示160行，完整差异保存在给定路径。'}
    if($r.sources){$r.sources | Select-Object skill,origin | Format-Table -Wrap | Out-Host}
    if($r.security){Write-Host ('安装前扫描 '+$r.security.status+' · '+$r.security.receipt)}
    foreach($err in $r.errors){Write-Host $err -ForegroundColor Red}
    foreach($warning in $r.application_evidence_warnings){Write-Warning $warning}
    foreach($warning in $r.cleanup_warnings){Write-Warning $warning}
    if($r.effective_applications){Write-Host ('可发现该技能的应用范围：'+($r.effective_applications -join ', '))}
    foreach($field in @('result','error','draft','candidate','update','transaction','known_good_checkpoint','recovery_error')){if($r.$field){Write-Host ($field+': '+[string]$r.$field)}}
    if($Operation -eq 'edit' -and $taskExit -eq 0 -and $r.candidate){
        Write-Host '编辑保存后用菜单 16 审查草稿，再用菜单 13 应用更新。'
        Invoke-Item -LiteralPath (Join-Path $r.candidate 'SKILL.md')
    }
    Write-Host ('Exit: '+$taskExit);return $taskExit
}
function Invoke-DistributionVerification {
    & python -B -X utf8 (Join-Path $PSScriptRoot 'verify_management_acceptance.py') | Out-Host
    return $LASTEXITCODE
}
function Invoke-MenuDistribution([string]$Operation) {
    $config=Get-Content -LiteralPath (Join-Path $taskRoot 'skillshare.yaml') -Raw -Encoding UTF8 | ConvertFrom-Json
    $names=@($config.targets.PSObject.Properties | ForEach-Object {
        if ($_.Value.skills) {$_.Value.skills.include} else {$_.Value.include}
    } | Sort-Object -Unique)
    Write-Host ('可选技能：'+($names -join '、'))
    $selection=(Read-Host '输入一个技能目录名，留空处理全部，输入 0 返回').Trim()
    if($selection -eq '0'){return}
    if($selection -and $selection -cnotin $names){Write-Host '没有找到这个目录名，本次操作已取消。';return}
    $null=Invoke-SkillAction $Operation '' $selection
}
if($Action -ne 'menu'){
    if($Action -in @('scan','scan-setup')){
        $arguments=@('-B','-X','utf8',(Join-Path $PSScriptRoot 'install_security.py'),$Action)
        if($Value){$arguments+=$Value}
        if($ExpectedSha256){$arguments+=@('--expected-sha256',$ExpectedSha256)}
        & python @arguments
        $taskExit=$LASTEXITCODE
    }
    elseif($Action -in @('sources','edit','draft','review','prepare','fetch','fetch-manifest','apply','import')){$taskExit=Invoke-UpdateAction $Action $Value $Candidate $Applications}
    elseif($Action -eq 'version'){& python -B -X utf8 (Join-Path $PSScriptRoot 'skill_manager.py') --version; $taskExit=$LASTEXITCODE}
    elseif($Action -eq 'verify'){$taskExit=Invoke-DistributionVerification}
    else {$taskExit=Invoke-SkillAction $Action $Value $Skill}
    exit $taskExit
}
while($true){
    Write-Host "`nAI Skill 管理（唯一入口）" -ForegroundColor Cyan
    Write-Host '1 状态  2 预览（单个或全部）  3 同步（单个或全部）  4 保存恢复点  5 历史与恢复'
    Write-Host '6 停用  7 恢复启用  8 审查并修复纳管漂移  9 查看说明'
    Write-Host '10 编辑  11 导入/新增  12 准备更新并查看差异  13 应用已审查更新  14 来源'
    Write-Host '15 独立文件校验  16 审查编辑草稿  17 补齐落后副本  18 回退中断操作  0 退出'
    $choice=Read-Host '选择'
    switch($choice){
        '1' {$null=Invoke-SkillAction 'status' ''}
        '2' {Invoke-MenuDistribution 'plan'}
        '3' {Invoke-MenuDistribution 'sync'}
        '4' {$null=Invoke-SkillAction 'checkpoint' (Read-Host '恢复点备注（可留空）')}
        '5' {
            $null=Invoke-SkillAction 'history' ''
            $id=Read-Host '要恢复的完整 ID（留空只查看）'
            if($id -and (Invoke-SkillAction 'restore-plan' $id) -eq 0){
                if((Read-Host '已查看恢复范围，输入 restore 执行') -ceq 'restore'){$null=Invoke-SkillAction 'restore' $id}
            }
        }
        '6' {$null=Invoke-SkillAction 'disable' (Read-Host '技能目录名')}
        '7' {$null=Invoke-SkillAction 'enable' (Read-Host '技能目录名')}
        '8' {
            $selected=Read-Host '确认要修复的技能目录名（留空返回）'
            if($selected){
                $null=Invoke-SkillAction 'plan' '' $selected
                if((Read-Host '已审查此技能的差异，输入 repair 保存备份并修复') -ceq 'repair'){$null=Invoke-SkillAction 'repair' '' $selected}
            }
        }
        '9' {Get-Content -LiteralPath (Join-Path $taskRoot 'README.md') -Encoding UTF8}
        '10' {$null=Invoke-UpdateAction 'edit' (Read-Host '技能目录名') '' ''}
        '11' {
            $config=Get-Content -LiteralPath (Join-Path $taskRoot 'skillshare.yaml') -Raw -Encoding UTF8 | ConvertFrom-Json
            $appNames=@($config.'x-application-readers'.PSObject.Properties.Name)
            Write-Host ('可选应用：'+($appNames -join ', '))
            Write-Host '共享目标会供读取该目录的其他应用使用，例如 ZCode 与 Codex。'
            $selectedApps=Read-Host '输入应用名，逗号分隔。输入 ALL 分发全部，留空返回'
            if($selectedApps){
                if($selectedApps -ceq 'ALL'){$selectedApps=''}
                $null=Invoke-UpdateAction 'import' (Read-Host '新技能目录名') (Read-Host '含 SKILL.md 的文件夹完整路径') $selectedApps
            }
        }
        '12' {$null=Invoke-UpdateAction 'prepare' (Read-Host '已安装的技能目录名') (Read-Host '候选新版文件夹完整路径')}
        '13' {$null=Invoke-UpdateAction 'apply' (Read-Host '已审查差异的更新 ID') ''}
        '14' {$null=Invoke-UpdateAction 'sources' '' ''}
        '15' {$null=Invoke-DistributionVerification}
        '16' {$null=Invoke-UpdateAction 'review' (Read-Host '完整草稿 ID') '' ''}
        '17' {
            $code=Invoke-SkillAction 'reconcile-plan' ''
            if($code -in @(0,2) -and (Read-Host '输入 reconcile 补齐可同步的副本') -ceq 'reconcile'){
                $null=Invoke-SkillAction 'reconcile' ''
            }
        }
        '18' {
            if((Invoke-SkillAction 'recover-plan' '') -eq 0 -and (Read-Host '输入 recover 回退中断操作') -ceq 'recover'){
                $null=Invoke-SkillAction 'recover' ''
            }
        }
        '0' {exit 0}
    }
}
