param([ValidateSet('all', 'text', 'filehelper', 'articles', 'miniprograms')][string]$Target = 'all')

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue) -or
    -not (Get-Command lib.exe -ErrorAction SilentlyContinue)) {
    throw '请先进入 MSVC 的 x64 开发者命令环境，再运行此脚本。运行预编译 DLL 不需要开发环境。'
}
if ($env:VSCMD_ARG_TGT_ARCH -and $env:VSCMD_ARG_TGT_ARCH -ne 'x64') {
    throw '需要 MSVC x64 目标环境。'
}

$taskBuild = Join-Path $PSScriptRoot '.build'
New-Item -ItemType Directory -Path $taskBuild -Force | Out-Null
Push-Location $taskBuild
try {
    # Relative source names keep local installation paths out of compiler strings.
    & cl.exe /nologo /c /TC /O2 /MD /DNDEBUG /W3 /utf-8 `
        /I..\vendor\minhook-1.3.4\include /I..\vendor\minhook-1.3.4\src `
        /I..\vendor\minhook-1.3.4\src\hde `
        ..\vendor\minhook-1.3.4\src\buffer.c ..\vendor\minhook-1.3.4\src\hook.c `
        ..\vendor\minhook-1.3.4\src\trampoline.c ..\vendor\minhook-1.3.4\src\hde\hde64.c
    if ($LASTEXITCODE -ne 0) { throw 'MinHook 编译失败。' }
    & lib.exe /nologo /out:minhook.lib buffer.obj hook.obj trampoline.obj hde64.obj
    if ($LASTEXITCODE -ne 0) { throw 'MinHook 建库失败。' }
    if ($Target -in @('all', 'text')) {
        New-Item -ItemType Directory -Path '..\build_text' -Force | Out-Null
        & cl.exe /nologo /std:c++17 /EHsc /W4 /utf-8 /LD /MD /O2 /DNDEBUG `
            ..\text_bridge.cpp /Fe:..\build_text\text_bridge.dll /Fo:text_bridge.obj `
            /link /MACHINE:X64 bcrypt.lib minhook.lib
        if ($LASTEXITCODE -ne 0) { throw '文本桥接 DLL 编译失败。' }
        # Run the production notice-layout policy with assertions enabled.
        & cl.exe /nologo /std:c++17 /EHsc /W4 /utf-8 /MD /O2 `
            ..\..\tests\native_local_recall_policy.cpp /Fe:local_recall_policy_test.exe `
            /Fo:local_recall_policy_test.obj
        if ($LASTEXITCODE -ne 0) { throw '本机防撤回边界测试编译失败。' }
        & '.\local_recall_policy_test.exe'
        if ($LASTEXITCODE -ne 0) { throw '本机防撤回边界测试失败。' }
    }
    if ($Target -in @('all', 'filehelper')) {
        New-Item -ItemType Directory -Path '..\build' -Force | Out-Null
        & cl.exe /nologo /std:c++17 /EHsc /W4 /utf-8 /LD /MD /O2 /DNDEBUG `
            ..\selftest_bridge.cpp /Fe:..\build\selftest_bridge.dll /Fo:selftest_bridge.obj `
            /link /MACHINE:X64 bcrypt.lib minhook.lib
        if ($LASTEXITCODE -ne 0) { throw '文件传输助手桥接 DLL 编译失败。' }
    }
    if ($Target -in @('all', 'articles')) {
        New-Item -ItemType Directory -Path '..\build_article' -Force | Out-Null
        & cl.exe /nologo /std:c++17 /EHsc /W4 /utf-8 /LD /MD /O2 /DNDEBUG `
            ..\article_bridge.cpp /Fe:..\build_article\article_bridge.dll /Fo:article_bridge.obj `
            /link /MACHINE:X64 bcrypt.lib minhook.lib
        if ($LASTEXITCODE -ne 0) { throw '公众号读取桥接 DLL 编译失败。' }
    }
    if ($Target -in @('all', 'miniprograms')) {
        New-Item -ItemType Directory -Path '..\build_miniprogram' -Force | Out-Null
        & cl.exe /nologo /std:c++17 /EHsc /W4 /utf-8 /LD /MD /O2 /DNDEBUG `
            ..\miniprogram_bridge.cpp /Fe:..\build_miniprogram\miniprogram_bridge.dll /Fo:miniprogram_bridge.obj `
            /link /MACHINE:X64 bcrypt.lib minhook.lib
        if ($LASTEXITCODE -ne 0) { throw '小程序原生 IPC 桥接 DLL 编译失败。' }
    }
} finally {
    Pop-Location
}
