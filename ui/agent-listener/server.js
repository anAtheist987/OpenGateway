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

// Simple in-memory listener for host_agent_test forwarding
// Listens on port 8084 for /chat/call, /chat/function, /chat/final
// Exposes /tree (GET) and /events (SSE) for front-end consumption
const http = require('http');
const url = require('url');

const PORT = process.env.PORT || 8084;

let nodes = {}; // id -> node
let roots = []; // root ids
let clients = [];

function notifyClients() {
  const payload = JSON.stringify({ type: 'update', tree: buildTree() });
  clients.forEach((res) => {
    res.write(`data: ${payload}\n\n`);
  });
}

function buildTree() {
  // return array of root nodes with nested children
  const idToNode = {};
  Object.keys(nodes).forEach((id) => {
    const n = nodes[id];
    idToNode[id] = { ...n, children: [] };
  });

  // attach children
  Object.values(idToNode).forEach((n) => {
    if (n.parent_id && idToNode[n.parent_id]) {
      idToNode[n.parent_id].children.push(n);
    }
  });

  // collect roots (those without parent or parent missing)
  const rootNodes = Object.values(idToNode).filter((n) => !n.parent_id || !idToNode[n.parent_id]);
  return rootNodes;
}

function parseJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      if (!body) return resolve(null);
      try {
        resolve(JSON.parse(body));
      } catch (e) {
        resolve(null);
      }
    });
    req.on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  if (req.method === 'GET' && parsed.pathname === '/tree') {
    const tree = buildTree();
    console.log('GET /tree -> returning tree with', Object.keys(nodes).length, 'nodes');
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    res.end(JSON.stringify(tree));
    return;
  }

  if (req.method === 'GET' && parsed.pathname === '/events') {
    // SSE
    res.writeHead(200, {
      Connection: 'keep-alive',
      'Cache-Control': 'no-cache',
      'Content-Type': 'text/event-stream',
      'Access-Control-Allow-Origin': '*',
    });
    // Send current tree immediately so late-connecting clients get existing state
    const snapshot = JSON.stringify({ type: 'update', tree: buildTree() });
    res.write(`data: ${snapshot}\n\n`);
    clients.push(res);
    console.log('SSE client connected, total clients =', clients.length);
    req.on('close', () => {
      clients = clients.filter((c) => c !== res);
    });
    return;
  }

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    });
    res.end();
    return;
  }

  if (req.method === 'POST' && parsed.pathname === '/reset') {
    nodes = {};
    roots = [];
    notifyClients();
    res.writeHead(200, { 'Access-Control-Allow-Origin': '*' });
    res.end('ok');
    return;
  }

  if (req.method === 'POST' && ['/chat/call', '/chat/function', '/chat/final'].includes(parsed.pathname)) {
    const body = await parseJsonBody(req);
    console.log('POST', parsed.pathname, 'body:', JSON.stringify(body));

    if (parsed.pathname === '/chat/call') {
      // body: { event_id, author, parts }
      const parts = body?.parts || [];
      parts.forEach((part) => {
        if (part.function_call) {
          const fc = part.function_call;
          const id = fc.id || fc.call_id || `${Date.now()}-${Math.random()}`;
          const name = fc.name || (fc.args && fc.args.agent_name) || 'sub-agent';
          const args = fc.args || {};
          const parent_id = args.parent_id || args.parentCallId || fc.parent_id || null;
          nodes[id] = {
            id,
            name,
            args,
            status: 'pending',
            response: null,
            parent_id,
          };
        }
      });
      console.log('Recorded function_call nodes:', Object.keys(nodes).length);
      notifyClients();
      res.writeHead(200);
      res.end('ok');
      return;
    }

    if (parsed.pathname === '/chat/function') {
      // body: { event_id, author, function_response }
      // function_response 结构: { id, name, response: { actual data } }
      const fr = body?.function_response || {};
      const id = fr.id || fr.call_id;
      if (id) {
        // 存 fr.response（实际返回内容），而非整个包装对象
        // 这样 CallTree.tsx 可以直接读 node.response.keyword / .task / .message
        const responseContent = fr.response !== undefined ? fr.response : fr;
        if (!nodes[id]) {
          nodes[id] = { id, name: fr.name || id, args: {}, status: 'done', response: responseContent, parent_id: null };
        } else {
          nodes[id].response = responseContent;
          nodes[id].status = 'done';
        }
      }
      console.log('Recorded function_response for id=', id);
      notifyClients();
      res.writeHead(200);
      res.end('ok');
      return;
    }

    if (parsed.pathname === '/chat/final') {
      // final responses may contain event_id, author, final_text
      const finalText = body?.final_text || body?.text || null;
      // store as a pseudo-root node
      const id = body?.event_id || `final-${Date.now()}`;
      nodes[id] = { id, name: 'final', args: {}, status: 'done', response: { final_text: finalText }, parent_id: null };
      console.log('Recorded final node id=', id);
      notifyClients();
      res.writeHead(200);
      res.end('ok');
      return;
    }
  }

  // Fallback
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('Not Found');
});

server.listen(PORT, () => {
  console.log(`Agent listener running on http://localhost:${PORT}`);
});
