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


echo "|Field|Description|"
echo "|-|-|"
jq -r -f "$( dirname -- "${BASH_SOURCE[0]}" )"/schema_paths.jq "$1"| sed 's|.\[\].|\[\].|g'
