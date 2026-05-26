# AgentsHive init bootstrap for Windows PowerShell.
#
# Usage:
#   iex "& { $(iwr -useb https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.ps1) } poker-online"
#
# Or just:
#   iex (iwr -useb https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.ps1).Content
#   (then run `agentshive-init poker-online` -- the function is defined in current shell)
#
# Why this exists: PowerShell's `iwr ... | python -` pipeline line-wraps
# multi-line script content to console width before piping, breaking
# Python's triple-quoted string literals. Downloading to a temp file
# preserves bytes verbatim.

function agentshive-init {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $tmp = [System.IO.Path]::GetTempFileName() + ".py"
    try {
        Invoke-WebRequest -UseBasicParsing `
            -Uri "https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/init_project.py" `
            -OutFile $tmp
        & python $tmp @Args
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $tmp
    }
}

# When invoked via iex, also run immediately with the args after the script.
# This handles the `iex "& { $(iwr ...) } slug"` invocation pattern.
if ($args.Count -gt 0) {
    agentshive-init @args
}
