# Keep the default build responsive on a shared development machine. Explicit
# -Jobs remains available for dedicated builders with a known memory budget.

function Get-FastHostMemory {
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        try {
            # Query the kernel directly: WMI/CIM can stall when the machine is
            # already under memory pressure. This works in Windows PowerShell 5.1.
            if (-not ('PrusaSlicer.FastBuildMemory' -as [type])) {
                Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace PrusaSlicer {
    public static class FastBuildMemory {
        [StructLayout(LayoutKind.Sequential)]
        public struct Status {
            public uint Length;
            public uint MemoryLoad;
            public ulong TotalPhysical;
            public ulong AvailablePhysical;
            public ulong TotalPageFile;
            public ulong AvailablePageFile;
            public ulong TotalVirtual;
            public ulong AvailableVirtual;
            public ulong AvailableExtendedVirtual;
        }
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GlobalMemoryStatusEx(ref Status status);
        public static Status Read() {
            var status = new Status();
            status.Length = (uint)Marshal.SizeOf(typeof(Status));
            if (!GlobalMemoryStatusEx(ref status))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return status;
        }
    }
}
'@
            }
            $memory = [PrusaSlicer.FastBuildMemory]::Read()
            return [pscustomobject]@{
                TotalBytes = [long] $memory.TotalPhysical
                AvailableBytes = [long] $memory.AvailablePhysical
            }
        }
        catch {
            Write-Verbose "Physical memory query failed: $($_.Exception.Message)"
        }
    }

    # Unknown memory budgets (including non-Windows hosts) use a single worker.
    # Developers can opt in to more parallelism with -Jobs on those hosts.
    return [pscustomobject]@{ TotalBytes = 0L; AvailableBytes = 0L }
}

function Resolve-FastBuildJobs {
    param(
        [ValidateRange(0, 512)] [int] $Jobs = 0,
        [ValidateRange(1, 2147483647)] [int] $ProcessorCount = [Environment]::ProcessorCount,
        [ValidateRange(0, [long]::MaxValue)] [long] $TotalMemoryBytes = 0,
        [ValidateRange(0, [long]::MaxValue)] [long] $AvailableMemoryBytes = 0,
        [ValidateRange(1, [long]::MaxValue)] [long] $MinimumFreeMemoryBytes = 6GB,
        [ValidateRange(1, [long]::MaxValue)] [long] $BuildMemoryBytes = 6GB
    )

    # Fail closed even with explicit -Jobs: worker selection is not permission
    # to start work when the desktop reserve cannot be maintained. Runtime
    # sampling in run_guarded.py also checks free RAM and Windows commit.
    if ($TotalMemoryBytes -le 0 -or $AvailableMemoryBytes -le 0 -or
        $AvailableMemoryBytes -gt $TotalMemoryBytes) {
        throw 'Cannot establish a trustworthy physical-memory budget; refusing to start a build.'
    }
    $workerBytes = [Math]::Min(2GB, $BuildMemoryBytes)
    if ($AvailableMemoryBytes -lt $MinimumFreeMemoryBytes + $workerBytes) {
        throw 'Insufficient available RAM for the desktop reserve plus one compiler worker; refusing to start a build.'
    }
    if ($Jobs -gt 0) {
        return $Jobs
    }

    # Share the runtime guard's budget; also leave a quarter of installed RAM
    # free on large workstations. Reserve one worker's worth for build/link tools.
    $reservedBytes = [Math]::Max($MinimumFreeMemoryBytes, $TotalMemoryBytes / 4.0)
    $usableBytes = [Math]::Min($BuildMemoryBytes, $AvailableMemoryBytes - $reservedBytes)
    $memoryJobs = [Math]::Max(1, [Math]::Floor(($usableBytes - $workerBytes) / $workerBytes))
    $cpuJobs = [Math]::Max(1, [Math]::Floor($ProcessorCount / 2.0))
    return [int] [Math]::Min(4, [Math]::Min($cpuJobs, $memoryJobs))
}

function New-FastWindowsCommandFile {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $VsDevCmd,
        [Parameter(Mandatory)] [string] $Executable,
        [Parameter(Mandatory)] [string[]] $ArgumentList
    )
    # Python's Windows argv quoting is for CommandLineToArgvW, not cmd.exe.
    # A durable command file avoids embedded-quote corruption at that boundary
    # and records the actual build command beside its telemetry. Escape percent
    # expansion in batch files; quotes and line breaks are rejected, not guessed.
    $values = @($VsDevCmd, $Executable) + $ArgumentList
    foreach ($value in $values) {
        if ($value -match '["\r\n]') { throw 'Unsupported quote or line break in a Windows build argument.' }
    }
    $quoted = @($values | ForEach-Object { '"' + $_.Replace('%', '%%') + '"' })
    $lines = @('@echo off', 'setlocal DisableDelayedExpansion', 'chcp 65001 >nul',
        ('call ' + $quoted[0] + ' -no_logo -arch=x64 -host_arch=x64 >nul'),
        'if errorlevel 1 exit /b %errorlevel%',
        ($quoted[1..($quoted.Count-1)] -join ' '), 'exit /b %errorlevel%')
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(($lines -join "`r`n") + "`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
    }
    finally { $stream.Dispose() }
}
