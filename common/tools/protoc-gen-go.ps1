#!/usr/bin/env pwsh

# Get the directory where this script is located
$WD = Split-Path -Parent $MyInvocation.MyCommand.Path
$WD = Resolve-Path $WD

echo $WD

# Execute the protoc-gen-go tool with the specified module file
& go tool -modfile="$WD/go.mod" "google.golang.org/protobuf/cmd/protoc-gen-go" $args
