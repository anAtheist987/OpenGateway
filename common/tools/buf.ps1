#!/usr/bin/env pwsh

# Get the directory where this script is located
$WD = Split-Path -Parent $MyInvocation.MyCommand.Path
$WD = Resolve-Path $WD

# Execute the buf tool with the specified module file
& go tool -modfile="$WD/go.mod" "github.com/bufbuild/buf/cmd/buf" $args
