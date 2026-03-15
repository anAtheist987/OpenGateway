#!/usr/bin/env bash
# Copyright 2026 Tsinghua University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# This file was created by Tsinghua University and is not part of
# the original agentgateway project by Solo.io.

set -e
if BUILD_GIT_REVISION=$(git rev-parse HEAD 2> /dev/null); then
  if [[ -z "${IGNORE_DIRTY_TREE}" ]] && [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    BUILD_GIT_REVISION=${BUILD_GIT_REVISION}"-dirty"
  fi
else
  BUILD_GIT_REVISION=unknown
fi

# used by common/scripts/gobuild.sh
echo "agentgateway.dev.buildVersion=${VERSION:-$BUILD_GIT_REVISION}"
echo "agentgateway.dev.buildGitRevision=${GIT_REVISION:-$BUILD_GIT_REVISION}"
echo "agentgateway.dev.buildOS=$(uname -s)"
echo "agentgateway.dev.buildArch=$(uname -m)"