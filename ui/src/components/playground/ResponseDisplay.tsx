// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ChevronLeft, ChevronRight } from "lucide-react";

interface ResponseDisplayProps {
  connectionType: "mcp" | "a2a" | null;
  mcpResponse: any;
  a2aResponse: any;
}

/**
 * 尝试从 MCP / A2A / JSON-RPC 响应中提取「人类可读文本」
 */
function extractText(responseData: any): string | null {
  if (!responseData || typeof responseData !== "object") {
    return null;
  }

  if (responseData.error) {
    return `error: ${responseData.error.message}`;
  }

  // A2A Task format: { artifacts: [{ parts: [{ kind: "text", text: "..." }] }] }
  const artifacts = responseData?.artifacts;
  if (Array.isArray(artifacts) && artifacts.length > 0) {
    const texts: string[] = [];
    for (const artifact of artifacts) {
      const parts = artifact?.parts;
      if (Array.isArray(parts)) {
        for (const part of parts) {
          if (
            part &&
            (part.kind === "text" || part.type === "text") &&
            typeof part.text === "string"
          ) {
            texts.push(part.text);
          }
        }
      }
    }
    if (texts.length > 0) return texts.join("\n");
  }

  // MCP / JSON-RPC format: { result: { parts: [...] } } or { parts: [...] }
  const parts = responseData?.result?.parts ?? responseData?.parts;

  if (!Array.isArray(parts)) {
    return null;
  }

  const texts = parts
    .filter(
      (part) =>
        part && (part.kind === "text" || part.type === "text") && typeof part.text === "string"
    )
    .map((part) => part.text);

  if (texts.length === 0) {
    return null;
  }

  return texts.join("\n");
}

/** 按三页结构拆分：寻找 ### ...第一页 / 第二页 / 第三页 标记 */
function splitIntoPages(text: string): string[] | null {
  const pagePattern = /(?=###[^\n]*第[一二三]页)/g;
  const indices: number[] = [];
  let match;
  while ((match = pagePattern.exec(text)) !== null) {
    indices.push(match.index);
  }

  if (indices.length !== 3) {
    return null;
  }

  return [
    text.slice(indices[0], indices[1]).trim(),
    text.slice(indices[1], indices[2]).trim(),
    text.slice(indices[2]).trim(),
  ];
}

const PAGE_LABELS = ["第一页：方案页", "第二页：流程页", "第三页：检查页"];

export function ResponseDisplay({
  connectionType,
  mcpResponse,
  a2aResponse,
}: ResponseDisplayProps) {
  const [currentPage, setCurrentPage] = useState(0);

  const responseData = connectionType === "a2a" ? a2aResponse : mcpResponse;

  if (!responseData) {
    return null;
  }

  const extractedText = extractText(responseData);
  const pages = extractedText ? splitIntoPages(extractedText) : null;

  if (pages) {
    return (
      <Card className="mt-4">
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Response</CardTitle>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="icon"
                onClick={() => setCurrentPage((p) => Math.max(0, p - 1))}
                disabled={currentPage === 0}
              >
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <span className="text-sm text-muted-foreground min-w-[130px] text-center">
                {PAGE_LABELS[currentPage]}
              </span>
              <Button
                variant="outline"
                size="icon"
                onClick={() => setCurrentPage((p) => Math.min(pages.length - 1, p + 1))}
                disabled={currentPage === pages.length - 1}
              >
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <div className="flex gap-1 mt-2">
            {pages.map((_, i) => (
              <button
                key={i}
                onClick={() => setCurrentPage(i)}
                className={`h-1.5 flex-1 rounded-full transition-colors ${
                  i === currentPage ? "bg-primary" : "bg-muted-foreground/30"
                }`}
              />
            ))}
          </div>
        </CardHeader>
        <CardContent>
          <pre className="bg-muted p-4 rounded-lg overflow-auto max-h-[600px] text-sm whitespace-pre-wrap">
            {pages[currentPage]}
          </pre>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle>Response</CardTitle>
      </CardHeader>
      <CardContent>
        {extractedText ? (
          <pre className="bg-muted p-4 rounded-lg overflow-auto max-h-[500px] text-sm whitespace-pre-wrap">
            {extractedText}
          </pre>
        ) : (
          <pre className="bg-muted p-4 rounded-lg overflow-auto max-h-[500px] text-sm">
            {JSON.stringify(responseData, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
