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

import React from "react";
import { Badge } from "@/components/ui/badge";

interface Node {
  id: string;
  name: string;
  status?: string;
  args?: any;
  response?: any;
  children?: Node[];
}

function summarizeNode(node: Node): string {
  const name = node.name;

  if (name === "final") {
    const txt = (node.response as any)?.final_text || (node.response as any)?.text;
    if (txt) return txt;
  }

  if (name === "search_agents") {
    const r = node.response as any;
    if (r) {
      const parts: string[] = [];
      if (r.keyword) parts.push(`keyword: ${r.keyword}`);
      if (r.task) parts.push(`task: ${r.task}`);
      if (r.message) parts.push(`message: ${r.message}`);
      if (r.total_candidates != null) parts.push(`total: ${r.total_candidates}`);
      if (parts.length) return parts.join(" \n");
    }
  }

  if (name === "send_message" || name.startsWith("send_message [")) {
    const a = node.args as any;
    const r = node.response as any;
    const parts: string[] = [];
    if (a) {
      if (a.agent_name) parts.push(`to: ${a.agent_name}`);
      if (a.node_id) parts.push(`node: ${a.node_id}`);
      if (a.task) parts.push(`task: ${a.task}`);
    }
    if (r) {
      if (r.status) parts.push(`status: ${r.status}`);
      if (r.response) parts.push(`response: ${r.response}`);
      if (r.summary) parts.push(`summary ↓: ${r.summary}`);
    }
    if (parts.length) return parts.join(" \n");
  }

  // fallback to pretty JSON
  return JSON.stringify(node.args || node.response || {}, null, 2);
}

function NodeView({ node }: { node: Node }) {
  const summary = summarizeNode(node);
  return (
    <div className="ml-2 my-2">
      <div className="flex items-center gap-2">
        <span className="font-medium">{node.name}</span>
        <Badge variant={node.status === "done" ? "secondary" : "outline"}>
          {node.status || "pending"}
        </Badge>
      </div>
      {summary && (
        <pre className="text-xs mt-1 bg-muted/20 p-2 rounded max-w-full overflow-auto">
          {summary}
        </pre>
      )}
      {node.children && node.children.length > 0 && (
        <div className="border-l pl-4 mt-2">
          {node.children.map((c) => (
            <NodeView key={c.id} node={c} />
          ))}
        </div>
      )}
    </div>
  );
}

export function CallTree({ tree }: { tree: Node[] }) {
  if (!tree || tree.length === 0) {
    return <div className="text-muted-foreground">No agent calls yet.</div>;
  }

  return (
    <div className="space-y-2 overflow-auto">
      {tree.map((n) => (
        <div key={n.id} className="p-2 bg-card rounded">
          <NodeView node={n} />
        </div>
      ))}
    </div>
  );
}

export default CallTree;
