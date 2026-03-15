// Copyright 2026 Tsinghua University
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// This file was created by Tsinghua University and is not part of
// the original agentgateway project by Solo.io.

"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface ResponseDisplayProps {
  connectionType: "mcp" | "a2a" | null;
  mcpResponse: any;
  a2aResponse: any;
}

/**
 * 尝试从 MCP / A2A 响应中提取「人类可读文本」
 * 支持结构：
 * - response.result.parts[].text
 * - response.parts[].text
 */
function extractText(responseData: any): string | null {
  if (!responseData || typeof responseData !== "object") {
    return null;
  }

  const parts = responseData?.result?.parts ?? responseData?.parts;

  if (!Array.isArray(parts)) {
    return null;
  }

  const texts = parts
    .filter((part) => part && part.kind === "text" && typeof part.text === "string")
    .map((part) => part.text);

  if (texts.length === 0) {
    return null;
  }

  return texts.join("\n");
}

export function ResponseDisplay({
  connectionType,
  mcpResponse,
  a2aResponse,
}: ResponseDisplayProps) {
  const responseData = connectionType === "a2a" ? a2aResponse : mcpResponse;

  if (!responseData) {
    return null;
  }

  // 尝试提取 text
  const extractedText = extractText(responseData);

  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle>Response</CardTitle>
      </CardHeader>
      <CardContent>
        {extractedText ? (
          // ✅ 人类可读模式
          <pre className="bg-muted p-4 rounded-lg overflow-auto max-h-[500px] text-sm">
            {extractedText}
          </pre>
        ) : (
          // 🔁 回退：原始 JSON
          <pre className="bg-muted p-4 rounded-lg overflow-auto max-h-[500px] text-sm">
            {JSON.stringify(responseData, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
