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

python -m vllm.entrypoints.openai.api_server \
  --model /mnt/ssd2/dh/Agent/airbnb_planner_multiagent/Qwen3.5-27B-FP8 \
  --served-model-name Qwen3.5-27B-FP8 \
  --dtype auto \
  --gpu-memory-utilization 0.40 \
  --tensor-parallel-size 8 \
  --trust-remote-code \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml