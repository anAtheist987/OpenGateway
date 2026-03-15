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

use hyper::{Request, Response};
use tower::{Service, MakeService};

use super::connect::Connect;
use super::pool;

pub struct Client<M> {
    // Hi there. So, let's take a 0.14.x hyper::Client, and build up its layers
    // here. We don't need to fully expose the layers to start with, but that
    // is the end goal.
    //
    // Client = MakeSvcAsService<
    //   SetHost<
    //     Http1RequestTarget<
    //       DelayedRelease<
    //         ConnectingPool<C, P>
    //       >
    //     >
    //   >
    // >
    make_svc: M,
}

// We might change this... :shrug:
type PoolKey = hyper::Uri;

/// A marker to identify what version a pooled connection is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[allow(dead_code)]
pub enum Ver {
    Auto,
    Http2,
}

// ===== impl Client =====

impl<M, /*ReqBody, ResBody,*/ E> Client<M>
where
    M: MakeService<
        hyper::Uri,
        Request<()>,
        Response = Response<()>,
        Error = E,
        MakeError = E,
    >,
    //M: Service<hyper::Uri, Error = E>,
    //M::Response: Service<Request<ReqBody>, Response = Response<ResBody>>,
{
    pub async fn request(&mut self, req: Request<()>) -> Result<Response<()>, E> {
        let mut svc = self.make_svc.make_service(req.uri().clone()).await?;
        svc.call(req).await
    }
}

impl<M, /*ReqBody, ResBody,*/ E> Client<M>
where
    M: MakeService<
        hyper::Uri,
        Request<()>,
        Response = Response<()>,
        Error = E,
        MakeError = E,
    >,
    //M: Service<hyper::Uri, Error = E>,
    //M::Response: Service<Request<ReqBody>, Response = Response<ResBody>>,
{
    
}
