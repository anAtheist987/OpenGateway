// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

#![allow(missing_docs)]

pub(crate) mod exec;
#[cfg(feature = "client")]
mod lazy;
#[cfg(feature = "client")]
mod sync;
pub(crate) mod timer;

#[cfg(feature = "client")]
pub(crate) use exec::Exec;
#[cfg(feature = "client")]
pub(crate) use lazy::{lazy, Started as Lazy};
#[cfg(feature = "client")]
pub(crate) use sync::SyncWrapper;

pub(crate) mod future;
