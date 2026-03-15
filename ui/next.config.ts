// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  basePath: "/ui",
  trailingSlash: true,
};

// 禁用 telemetry，避免构建时卡在向 telemetry.nextjs.org 发送请求
process.env.NEXT_TELEMETRY_DISABLED = "1";

export default nextConfig;
