<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

## **MCP Authentication API in agentgateway**

### **Scenario 1: agentgateway adapts traffic for Authorization Servers**

Most Authorization Servers don’t implement the standards as is, and the traffic from MCP Clients has to be adapted before it is forwarded to them.
A quick example is Keycloak, which doesn’t expose the certs at the well-known endpoint, and that needs to be redirected to */protocol/openid-connect/certs*

In this scenario agentgateway:

1. Exposes protected resource metadata (on behalf of the mcp server)
2. Proxies to the Authorization Server Metadata endpoint
3. Proxies and adapts traffic to the Authorization Server for client registration
4. Validates token against the AS jwks
5. Reject traffic without an access token and set the WWW-Authentication header to the resource metadata endpoint.

Config for this scenario:

```yaml
mcpAuthentication:
 issuer: [http://localhost:7080/realms/mcp](http://localhost:7080/realms/mcp)
 jwksUrl: http://localhost:7080/protocol/openid-connect/certs
 provider:
   # this is an indicator to adapt traffic for keycloak
   # It sets the jwksUrl to the keycloak certs endpoint (and uses it for validation) an
   # sets the issuer to the agentgateway URL, for it to adapt traffic to this provider
   keycloak: {}
 resourceMetadata:
   resource: http://localhost:3000/mcp
   scopesSupported:
   - read:all
   bearerMethodsSupported:
   - header
   - body
   - query
   # these two fields are optional
   resourceDocumentation: http://localhost:3000/stdio/docs
   resourcePolicyUri: http://localhost:3000/stdio/policies
```

### **Scenario 2: agentgateway acts solely as a resource server on behalf of MCP Servers**

In this scenario agentgateway:

1. Exposes protected resource metadata (on behalf of the mcp server)
2. Validates token against the AS jwks
3. Reject traffic without access token and set the WWW-Authentication header to the resource metadata endpoint.

NOTE: The route for oauth-authorization-server is not configured.

Config for this scenario:

```
mcpAuthentication:
 # points to the external Authorization Server
 # which handles the oauth server metadata, and everything else up to the point of using the token
 issuer: http://localhost:9000
 jwksUrl: http://localhost:9000/.well-known/jwks.json
 resourceMetadata:
   resource: http://localhost:3000/mcp
   scopesSupported:
   - read:all
   bearerMethodsSupported:
   - header
   - body
   - query
   resourceDocumentation: http://localhost:3000/stdio/docs
   resourcePolicyUri: http://localhost:3000/stdio/policies
```

## Key information:

Audience and resource can be used interchangeably.
The resource returned by the .well-known/oauth-protected-resource has to be the same as the domain the result is returned from. Meaning:

If we query:

```
curl -s http://localhost:3003/.well-known/oauth-protected-resource/mcp | jq .resource
```

The value below should be returned which shows that [http://localhost:3003/mcp](http://localhost:3003/mcp) matches:
```
"http://localhost:3003/mcp"
```

The well known resource metadata endpoint is formed like this:

If the server is accessible at [http://www.example.com](http://www.example.com), then the endpoint is:

```
[http://www.example.com](http://www.example.com)/.well-known/oauth-protected-resource
```

If the endpoint has some path e.g.

```
[http://www.example.com](http://www.example.com)/path
```

Then it is postfixed:

```
[http://www.example.com](http://www.example.com)/.well-known/oauth-protected-resource/path
```

The key audience is equal to the key resource, and it has to match the endpoint receiving traffic. Which is the location of the proxy.

WWW-Authentication has to return the following information:
```
WWW-Authenticate: Bearer resource\_metadata="[http://localhost:3003/.well-known/oauth-protected-resource/mcp](http://localhost:3003/.well-known/oauth-protected-resource//mcp)"
```

This has the same approach of being configured postfixed with the path of the request.

### **Scenario 3: agentgateway passes traffic as is to an MCP Server that already implements MCP Authorization**

On this scenario, we don't need the mcpAuthentication property in those cases.
