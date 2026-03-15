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

import React, { useMemo } from "react";
import { Badge } from "@/components/ui/badge";

// ── Types ─────────────────────────────────────────────────────────────────────

interface RawNode {
  id: string;
  name: string;
  status?: string;
  args?: any;
  response?: any;
  children?: RawNode[];
}

interface DagNodeInfo {
  id: string; // call_id from listener
  nodeId: string; // dag node_id (t1, t2, …)
  agentName: string;
  task: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  response?: string;
  summary?: string;
  batchId?: string; // shared parent_id for parallel siblings
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function parseNodes(tree: RawNode[]): DagNodeInfo[] {
  const result: DagNodeInfo[] = [];
  for (const node of tree) {
    if (!node.name.startsWith("send_message")) continue;
    const a = node.args as any;
    const r = node.response as any;
    result.push({
      id: node.id,
      nodeId: a?.node_id ?? node.id,
      agentName: a?.agent_name ?? node.name,
      task: a?.task ?? "",
      status:
        node.status === "done"
          ? r?.status === "failed"
            ? "failed"
            : r?.status === "skipped"
              ? "skipped"
              : "done"
          : "running",
      response: r?.response,
      summary: r?.summary,
      batchId: a?.parent_id ?? undefined,
    });
  }
  return result;
}

/** Group nodes into layers: nodes sharing the same batchId are in the same layer.
 *  Nodes without a batchId each get their own layer (sequential). */
function groupLayers(nodes: DagNodeInfo[]): DagNodeInfo[][] {
  if (nodes.length === 0) return [];
  const layers: DagNodeInfo[][] = [];
  const batchMap = new Map<string, DagNodeInfo[]>();

  for (const n of nodes) {
    if (n.batchId) {
      if (!batchMap.has(n.batchId)) {
        batchMap.set(n.batchId, []);
        layers.push(batchMap.get(n.batchId)!);
      }
      batchMap.get(n.batchId)!.push(n);
    } else {
      layers.push([n]);
    }
  }
  return layers;
}

// ── Sub-components ────────────────────────────────────────────────────────────

const STATUS_STYLES: Record<string, string> = {
  pending: "border-muted-foreground/30 bg-muted/30 text-muted-foreground",
  running:
    "border-blue-400 bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 animate-pulse",
  done: "border-green-400 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-300",
  failed: "border-red-400 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300",
  skipped:
    "border-yellow-400 bg-yellow-50 dark:bg-yellow-950/40 text-yellow-700 dark:text-yellow-300",
};

const STATUS_DOT: Record<string, string> = {
  pending: "bg-muted-foreground/40",
  running: "bg-blue-400 animate-ping",
  done: "bg-green-400",
  failed: "bg-red-400",
  skipped: "bg-yellow-400",
};

function NodeCard({ node }: { node: DagNodeInfo }) {
  const style = STATUS_STYLES[node.status] ?? STATUS_STYLES.pending;
  const dot = STATUS_DOT[node.status] ?? STATUS_DOT.pending;

  return (
    <div className={`rounded-lg border-2 p-3 text-xs space-y-1.5 w-full ${style}`}>
      {/* Header */}
      <div className="flex items-center gap-1.5 font-semibold text-sm">
        <span className={`inline-block h-2 w-2 rounded-full flex-shrink-0 ${dot}`} />
        <span className="truncate">{node.agentName}</span>
        <span className="ml-auto font-mono text-[10px] opacity-60">{node.nodeId}</span>
      </div>

      {/* Task */}
      {node.task && <p className="leading-snug opacity-80 line-clamp-3">{node.task}</p>}

      {/* Response (done only) */}
      {node.status === "done" && node.response && (
        <div className="mt-1 pt-1 border-t border-current/20">
          <p className="font-medium opacity-60 mb-0.5">Response</p>
          <p className="leading-snug line-clamp-4 opacity-90">{node.response}</p>
        </div>
      )}

      {/* Summary passed downstream */}
      {node.summary && (
        <div className="mt-1 pt-1 border-t border-current/20 rounded bg-current/5 px-2 py-1">
          <p className="font-semibold opacity-70 mb-0.5">↓ Summary to downstream</p>
          <p className="leading-snug line-clamp-3 italic opacity-80">{node.summary}</p>
        </div>
      )}
    </div>
  );
}

/** Arrow connector between layers */
function LayerConnector({ count }: { count: number }) {
  return (
    <div className="flex items-center justify-center py-1 gap-2">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="flex flex-col items-center opacity-40">
          <div className="w-px h-4 bg-muted-foreground" />
          <svg
            width="10"
            height="6"
            viewBox="0 0 10 6"
            className="text-muted-foreground fill-current"
          >
            <path d="M5 6L0 0h10z" />
          </svg>
        </div>
      ))}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function DagView({ tree }: { tree: RawNode[] }) {
  const nodes = useMemo(() => parseNodes(tree), [tree]);
  const layers = useMemo(() => groupLayers(nodes), [nodes]);

  // Decomposition node: "final" whose text starts with "[Gateway Decomposition"
  const decompNode = tree.find(
    (n) =>
      n.name === "final" &&
      (n.response as any)?.final_text?.trimStart().startsWith("[Gateway Decomposition")
  );

  // Summary node: "final" with the longest text (LLM report), excluding the decomp node
  const summaryNode = tree
    .filter((n) => n.name === "final" && (n.response as any)?.final_text && n.id !== decompNode?.id)
    .sort(
      (a, b) =>
        ((b.response as any).final_text?.length ?? 0) -
        ((a.response as any).final_text?.length ?? 0)
    )[0];

  if (nodes.length === 0 && !decompNode && !summaryNode) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        Waiting for DAG execution…
      </div>
    );
  }

  return (
    <div className="space-y-0 overflow-auto h-full">
      {/* ① Decomposition plan */}
      {decompNode && (
        <>
          <div className="rounded-lg border-2 border-violet-400/50 bg-violet-50 dark:bg-violet-950/30 p-3 text-xs">
            <p className="font-semibold text-violet-700 dark:text-violet-300 mb-1">
              ⬡ Task Decomposition
            </p>
            <p className="text-muted-foreground leading-snug whitespace-pre-wrap">
              {(decompNode.response as any)?.final_text ?? ""}
            </p>
          </div>
          {layers.length > 0 && <LayerConnector count={Math.min(layers[0].length, 3)} />}
        </>
      )}

      {/* ② DAG execution layers */}
      {layers.map((layer, li) => (
        <div key={li}>
          <div className="flex items-center gap-2 mb-2">
            <span className="text-[10px] font-mono text-muted-foreground uppercase tracking-wider">
              {layer.length > 1 ? `Batch ${li + 1} — ${layer.length} parallel` : `Step ${li + 1}`}
            </span>
            {layer.length > 1 && (
              <Badge variant="outline" className="text-[10px] px-1.5 py-0">
                ⚡ parallel
              </Badge>
            )}
          </div>

          <div
            className="grid gap-3"
            style={{ gridTemplateColumns: `repeat(${Math.min(layer.length, 3)}, 1fr)` }}
          >
            {layer.map((n) => (
              <NodeCard key={n.id} node={n} />
            ))}
          </div>

          {li < layers.length - 1 && <LayerConnector count={Math.min(layers[li + 1].length, 3)} />}
        </div>
      ))}

      {/* ③ Final LLM summary */}
      {summaryNode && (
        <>
          <LayerConnector count={1} />
          <div className="rounded-lg border-2 border-primary/40 bg-primary/5 p-3 text-xs">
            <p className="font-semibold text-primary mb-1">✓ DAG Complete</p>
            <p className="text-muted-foreground leading-snug whitespace-pre-wrap">
              {(summaryNode.response as any)?.final_text ?? ""}
            </p>
          </div>
        </>
      )}
    </div>
  );
}

export default DagView;
