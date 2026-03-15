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

import { useState, useEffect, useRef } from "react";
import { A2AClient } from "@a2a-js/sdk/client";
import { DagView } from "@/components/playground/DagView";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Loader2, Send, Bot, Users, RefreshCw } from "lucide-react";
import { v4 as uuidv4 } from "uuid";

const HOST_AGENT_URL = "http://localhost:3001";
const LISTENER_TREE_URL = "http://localhost:8084/tree";
const LISTENER_SSE_URL = "http://localhost:8084/events";

interface ChatMessage {
  role: "user" | "assistant" | "error";
  content: string;
  timestamp: string;
}

export default function TravelAgentPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [callTree, setCallTree] = useState<any[]>([]);
  const [listenerConnected, setListenerConnected] = useState(false);
  const clientRef = useRef<A2AClient | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  // Initialize A2A client once
  useEffect(() => {
    clientRef.current = new A2AClient(HOST_AGENT_URL);
  }, []);

  // Connect to agent-listener SSE for real-time call tree
  useEffect(() => {
    let es: EventSource | null = null;

    async function fetchTree() {
      try {
        const res = await fetch(LISTENER_TREE_URL);
        if (res.ok) {
          const data = await res.json();
          setCallTree(data || []);
          setListenerConnected(true);
        }
      } catch {
        setListenerConnected(false);
      }
    }

    fetchTree();

    try {
      es = new EventSource(LISTENER_SSE_URL);
      es.onopen = () => setListenerConnected(true);
      es.onerror = () => setListenerConnected(false);
      es.onmessage = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          if (payload?.tree) {
            setCallTree(payload.tree);
          }
        } catch {
          // ignore parse errors
        }
      };
    } catch {
      // SSE not available
    }

    return () => {
      if (es) es.close();
    };
  }, []);

  // Auto-scroll to latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  const extractResponseText = (result: any): string => {
    if (!result) return "";
    // A2A Task format: status.message.parts[].text
    if (result.status?.message?.parts) {
      return result.status.message.parts
        .map((p: any) => p.text || "")
        .filter(Boolean)
        .join("\n");
    }
    // Artifact format
    if (result.artifacts?.length > 0) {
      const art = result.artifacts[0];
      return art?.parts?.[0]?.root?.text || art?.parts?.[0]?.text || JSON.stringify(art);
    }
    // Direct result field
    if (result.result) {
      return typeof result.result === "string"
        ? result.result
        : JSON.stringify(result.result, null, 2);
    }
    return JSON.stringify(result, null, 2);
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || isLoading) return;

    setInput("");
    setCallTree([]); // reset tree for this new request
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text, timestamp: new Date().toISOString() },
    ]);
    setIsLoading(true);

    try {
      const client = clientRef.current ?? new A2AClient(HOST_AGENT_URL);
      const result = await client.sendMessage({
        message: {
          role: "user",
          parts: [{ kind: "text", text }],
          kind: "message",
          messageId: uuidv4(),
        },
      });

      const responseText = extractResponseText(result) || "Agent returned no text.";
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: responseText, timestamp: new Date().toISOString() },
      ]);
    } catch (err: any) {
      setMessages((prev) => [
        ...prev,
        {
          role: "error",
          content: `Error: ${err?.message ?? "Failed to reach host agent at " + HOST_AGENT_URL}`,
          timestamp: new Date().toISOString(),
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const clearChat = () => {
    setMessages([]);
    setCallTree([]);
  };

  return (
    <div className="container mx-auto py-8 px-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Travel Agent</h1>
          <p className="text-muted-foreground mt-1">
            Multi-agent travel planning — semantic routing to specialized agents
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Badge variant={listenerConnected ? "default" : "secondary"} className="text-xs">
            {listenerConnected ? "● Live" : "○ Listener offline"}
          </Badge>
          <Button variant="outline" size="sm" onClick={clearChat} disabled={isLoading}>
            <RefreshCw className="h-4 w-4 mr-1" />
            Clear
          </Button>
        </div>
      </div>

      {/* Main layout: Chat (left) + Call Tree (right) */}
      <div
        className="grid grid-cols-1 lg:grid-cols-2 gap-6"
        style={{ height: "calc(100vh - 220px)", minHeight: "500px" }}
      >
        {/* Chat Panel */}
        <Card className="flex flex-col overflow-hidden">
          <CardHeader className="pb-2 flex-shrink-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <Bot className="h-4 w-4" />
              Chat
              {isLoading && (
                <Badge variant="outline" className="ml-auto text-xs">
                  <Loader2 className="h-3 w-3 animate-spin mr-1" />
                  Agents working...
                </Badge>
              )}
            </CardTitle>
          </CardHeader>

          <CardContent className="flex flex-col flex-1 overflow-hidden p-4 pt-0">
            {/* Message list */}
            <div className="flex-1 overflow-y-auto space-y-3 mb-3 pr-1">
              {messages.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full text-muted-foreground text-sm gap-3">
                  <Bot className="h-14 w-14 opacity-20" />
                  <p className="font-medium">Send a travel query to get started</p>
                  <div className="space-y-1 text-xs text-center opacity-70">
                    <p>帮我规划一个从北京到上海的3天旅行</p>
                    <p>查询下周巴黎的天气和航班</p>
                    <p>推荐东京的住宿和景点</p>
                  </div>
                </div>
              ) : (
                messages.map((msg, i) => (
                  <div
                    key={i}
                    className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
                  >
                    <div
                      className={`max-w-[85%] rounded-lg px-4 py-2.5 text-sm whitespace-pre-wrap break-words ${
                        msg.role === "user"
                          ? "bg-primary text-primary-foreground"
                          : msg.role === "error"
                            ? "bg-destructive/10 text-destructive border border-destructive/20"
                            : "bg-muted"
                      }`}
                    >
                      {msg.content}
                    </div>
                  </div>
                ))
              )}

              {isLoading && (
                <div className="flex justify-start">
                  <div className="bg-muted rounded-lg px-4 py-2.5 text-sm text-muted-foreground flex items-center gap-2">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Agents processing your request...
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Input area */}
            <div className="flex gap-2 flex-shrink-0">
              <Textarea
                placeholder="Enter travel query… (Enter to send, Shift+Enter for new line)"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isLoading}
                className="flex-1 resize-none min-h-[56px] max-h-[120px]"
                rows={2}
              />
              <Button
                onClick={sendMessage}
                disabled={isLoading || !input.trim()}
                className="self-end h-[56px] px-4"
              >
                {isLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Send className="h-4 w-4" />
                )}
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* DAG Execution View */}
        <Card className="flex flex-col overflow-hidden">
          <CardHeader className="pb-2 flex-shrink-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <Users className="h-4 w-4" />
              DAG Execution
              {isLoading && listenerConnected && (
                <Badge variant="outline" className="ml-auto text-xs">
                  <span className="h-2 w-2 rounded-full bg-green-500 inline-block mr-1 animate-pulse" />
                  Live
                </Badge>
              )}
            </CardTitle>
            <CardDescription className="text-xs">
              Parallel execution layers — nodes in the same batch run concurrently
            </CardDescription>
          </CardHeader>

          <CardContent className="flex-1 overflow-auto p-4 pt-0">
            {callTree.length === 0 && !listenerConnected ? (
              <div className="flex flex-col items-center justify-center h-full text-muted-foreground text-sm gap-2">
                <Users className="h-10 w-10 opacity-20" />
                <div className="text-center text-xs space-y-1">
                  <p className="font-medium">Agent Listener offline</p>
                  <p className="opacity-70">
                    Start the listener:{" "}
                    <code className="bg-muted px-1 rounded">node ui/agent-listener/server.js</code>
                  </p>
                </div>
              </div>
            ) : (
              <DagView tree={callTree} />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
