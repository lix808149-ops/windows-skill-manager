param([string]$Root=(Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem

if($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64'){
    throw 'This bootstrap requires Windows x64.'
}
$runtime=Join-Path $Root '_python'
$vendor=Join-Path $Root '_tools/vendor/skillshare/0.28.4'
$requiredExe='acbcf98fce0d9949fff3944a8ca34feac93da682d8c44532bfeb2a337fdb68f7'

function Confirm-Hash([string]$Path,[string]$Expected) {
    $stream=[IO.File]::OpenRead($Path)
    $hash=[Security.Cryptography.SHA256]::Create()
    try { $observed=[BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-','') }
    finally { $hash.Dispose(); $stream.Dispose() }
    if($observed -ine $Expected){
        throw ('Unexpected SHA-256 for '+$Path)
    }
}
function Receive-Archive([string]$Url,[string]$Expected,[string]$File,[string]$Destination) {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $Url -OutFile $File
    Confirm-Hash $File $Expected
    [IO.Compression.ZipFile]::ExtractToDirectory($File,$Destination)
}

$working=Join-Path $Root '.bootstrap'
[void][IO.Directory]::CreateDirectory($working)
$guard=[IO.File]::Open((Join-Path $working 'operation.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
$temp=Join-Path $working ([Guid]::NewGuid().ToString('N'))
try {
    [void][IO.Directory]::CreateDirectory($temp)
    if(-not (Test-Path -LiteralPath $runtime)){
        $pythonStage=Join-Path $temp 'runtime'
        $yamlStage=Join-Path $temp 'pyyaml'
        $pythonUrl='https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip'
        $yamlUrl='https://files.pythonhosted.org/packages/97/c9/39d5b874e8b28845e4ec2202b5da735d0199dbe5b8fb85f91398814a9a46/pyyaml-6.0.3-cp313-cp313-win_amd64.whl'
        Receive-Archive $pythonUrl '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38' (Join-Path $temp 'python.zip') $pythonStage
        Receive-Archive $yamlUrl '79005a0d97d5ddabfeeea4cf676af11e647e41d81c9a7722a193022accdb6b7c' (Join-Path $temp 'pyyaml.zip') $yamlStage
        Move-Item -LiteralPath (Join-Path $yamlStage 'yaml') -Destination $pythonStage
        Get-ChildItem -LiteralPath $yamlStage -Directory -Filter '*.dist-info' | Move-Item -Destination $pythonStage
        [IO.File]::WriteAllLines((Join-Path $pythonStage 'python313._pth'),@('python313.zip','.','..\_tools'),[Text.Encoding]::ASCII)
        Move-Item -LiteralPath $pythonStage -Destination $runtime
    }
    $python=Join-Path $runtime 'python.exe'
    & $python -B -I -c "import sys,yaml; assert sys.version_info[:3] == (3,13,17) and yaml.__version__ == '6.0.3'"
    if($LASTEXITCODE -ne 0){throw 'Bundled Python or PyYAML failed verification.'}
    if(-not (Test-Path -LiteralPath $vendor)){
        $shareStage=Join-Path $temp 'skillshare'
        $shareUrl='https://github.com/runkids/skillshare/releases/download/v0.28.4/skillshare_0.28.4_windows_amd64.zip'
        Receive-Archive $shareUrl '02dc167b57b75ae41dff6a00a5d212e9809d952f3352cea93ddca46e84c23313' (Join-Path $temp 'skillshare.zip') $shareStage
        Confirm-Hash (Join-Path $shareStage 'skillshare.exe') $requiredExe
        [void][IO.Directory]::CreateDirectory((Split-Path -Parent $vendor))
        Move-Item -LiteralPath $shareStage -Destination $vendor
    }
    Confirm-Hash (Join-Path $vendor 'skillshare.exe') $requiredExe
} finally {
    $guard.Dispose()
    if((Split-Path -Parent ([IO.Path]::GetFullPath($temp))) -eq [IO.Path]::GetFullPath($working) -and
       (Test-Path -LiteralPath $temp) -and -not ((Get-Item -LiteralPath $temp -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){
        Remove-Item -LiteralPath $temp -Recurse -Force
    }
}
