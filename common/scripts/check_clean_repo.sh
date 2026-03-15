#!/bin/bash
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


if [[ -n $(git status --porcelain) ]]; then
  git status
  git diff
  echo "ERROR: Some files need to be updated, please run 'make gen' and include any changed files in your PR"
  exit 1
fi
